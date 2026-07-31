"""Stats: a host PC's vitals on the badge, paged with UP and DOWN.

UP/DOWN     page through what the host is configured to show
A B C       whatever the host has bound them to, if anything
HOME        hold to leave

Where a screen takes A/B/C for its own input rather than passing them to the host, they
are used in the order they sit in: A back, B select, C next.

The host decides what the pages are; this fetches them and draws them. Polling is a
generator advanced from the draw loop, so a slow reply costs latency and never a
frame.
"""

import os
import sys
import time

APP_DIR = "/system/apps/stats"
try:
    os.chdir(APP_DIR)
except OSError:
    # Not installed: running from a mounted checkout. Locate the app by this file
    # rather than by cwd, which under `mpremote mount` is the mount root and not the
    # app directory - so `pages/` would be looked for in the wrong place.
    here = globals().get("__file__")
    APP_DIR = here.rsplit("/", 1)[0] if here and "/" in here else os.getcwd()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import look  # noqa: E402

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None


def _splash():
    """The mark, before anything expensive happens.

    Compiling draw, pages and net is about 500ms from flash and font.load another 107ms,
    so this is shapes only - no font, no icon file. It redraws what the launcher icon
    draws, a gauge sweep over three bars, at 320x240 and on the app's own dial angles so
    the splash and the first page agree.
    """
    theme = look.get(look.DEFAULT)
    screen.pen = color.rgb(*theme.bg)
    screen.clear()

    centre = vec2(look.W // 2, look.H // 2)
    outer, inner = 62, 45
    screen.pen = color.rgb(*theme.grid)
    screen.shape(shape.arc(centre, inner, outer, look.DIAL_FROM, look.DIAL_TO))
    screen.pen = color.rgb(*theme.accent)
    sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * 0.7
    screen.shape(shape.arc(centre, inner, outer, look.DIAL_FROM, sweep))

    screen.pen = color.rgb(*theme.ink)
    bar_w, gap = 11, 7
    span = 3 * bar_w + 2 * gap
    left = look.W // 2 - span // 2
    base = look.H // 2 + 14
    for i, height in enumerate((17, 30, 23)):
        screen.rectangle(rect(left + i * (bar_w + gap), base - height, bar_w, height))
    display.update()


_splash()

import draw  # noqa: E402
import net  # noqa: E402
import pages as pages_module  # noqa: E402
import wifi  # noqa: E402


def pairing_ui():
    """Import the pairing screens on demand.

    The badge compiles from source at every launch, and setup.py is 740ms of that for
    something an already-paired badge never shows. Deferring it is the single biggest
    saving available without precompiling to .mpy.
    """
    import setup
    return setup


def load_extensions():
    """Import any badge-side modules an extension had pushed into ext/.

    Each registers its own page kind in `pages.EXTRA` on import. A broken extension
    must not take the app down with it, so each is imported inside a try.

    The directory is `ext`, not `pages`: a directory named `pages` on sys.path shadows
    the app's own `pages.py`, and an extension importing `pages` would get the empty
    directory instead of the module it wanted.
    """
    directory = APP_DIR + "/ext"
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    if directory not in sys.path:
        sys.path.insert(0, directory)
    loaded = []
    for name in sorted(names):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        try:
            __import__(name[:-3])
            loaded.append(name[:-3])
        except Exception as exc:  # noqa: BLE001
            print(f"extension {name} failed: {exc}")
    return loaded

# The launcher puts an exit IRQ on HOME. Taking it lets HOME be held for exit, and
# leaves the button usable, but a way out has to stay - see `_check_exit`.
BUTTON_HOME.irq(None)
HOLD_TO_EXIT_MS = 700

STATE_APP = "stats"


class App:
    def __init__(self):
        self.config = net.Config()
        self.client = net.Client(self.config)
        self.theme = look.get(look.DEFAULT)
        self.layout = None
        self.layout_rev = -1
        self.frame = {}
        self.history = {}
        self.page_index = 0
        self.status = "starting"
        self.detail = None
        self.toast_until = 0
        self.toast_text = None
        self.home_since = None
        self.dirty = True

        # Poll state: one request in flight at a time, cycling stats, then layout or
        # history when they are due.
        self._next_poll = 0
        self._pending = None
        self._history_due = 0
        self._last_ok = 0
        self._was_stale = False
        self._next_hunt = 0
        self.rejected = False

        # State.load merges into the dict it is given and returns whether a file was
        # there, so the defaults are what to read afterwards.
        saved = {"page": 0}
        State.load(STATE_APP, saved)
        self.page_index = int(saved.get("page", 0) or 0)

    # -- pages --------------------------------------------------------------

    @property
    def page_list(self):
        return (self.layout or {}).get("pages") or []

    def current_page(self):
        pages = self.page_list
        if not pages:
            return None
        if self.page_index >= len(pages):
            self.page_index = 0
        return pages[self.page_index]

    def turn(self, delta):
        pages = self.page_list
        if not pages:
            return
        self.page_index = (self.page_index + delta) % len(pages)
        State.save(STATE_APP, {"page": self.page_index})
        self.dirty = True

    # -- polling ------------------------------------------------------------

    def poll(self):
        """Advance whatever request is in flight, or start the next one due."""
        if not self.config.paired:
            return

        if self._pending is not None:
            if not self.client.step():
                return
            self._finish(self._pending)
            self._pending = None
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._next_poll) < 0:
            return

        interval = (self.layout or {}).get("interval_ms", 1000)
        # Back off when the host is not answering, so a sleeping PC does not keep the
        # radio busy.
        if self.client.failures:
            interval = min(15000, interval * (1 << min(self.client.failures, 4)))
        self._next_poll = time.ticks_add(now, interval)

        if self.client.failures >= 3:
            self.hunt()

        if self.layout is None or self.layout_rev != (
                self.frame.get("layout_rev", self.layout_rev)):
            self._start("layout", "/v1/layout")
        elif time.ticks_diff(now, self._history_due) >= 0 and self._graph_keys():
            keys = ",".join(self._graph_keys())
            points = (self.layout or {}).get("graph_points", 48)
            self._history_due = time.ticks_add(now, 5000)
            self._start("history", f"/v1/history?keys={keys}&points={points}")
        else:
            self._start("stats", "/v1/stats")

    def hunt(self):
        """Look for a paired host on the network after the current one went quiet.

        Covers the two ways an address goes stale: the host got a new DHCP lease, or
        the badge moved to a desk with a different machine on it. Credentials are keyed
        on the server's id, so a beacon is enough to recognise a host we already know
        and follow it to its new address without re-pairing.
        """
        now = time.ticks_ms()
        if time.ticks_diff(now, self._next_hunt) < 0:
            return
        # Listening costs a frame's worth of time, so not on every failed poll.
        self._next_hunt = time.ticks_add(now, 20000)

        for beacon in net.discover(timeout_ms=1200):
            server_id = beacon.get("id")
            if not server_id:
                continue
            if server_id == self.config.active:
                if self.config.note_address(server_id, beacon["host"], beacon["port"],
                                            beacon.get("name")):
                    self.client.close()
                    self.note(f"moved to {beacon['host']}")
                    self.dirty = True
                return
            if server_id in self.config.hosts:
                self.config.note_address(server_id, beacon["host"], beacon["port"],
                                         beacon.get("name"))
                if self.config.switch(server_id):
                    self.client.close()
                    self.layout = None
                    self.history = {}
                    draw.clear_cache()
                    self.note(self.config.name or "switched host")
                    self.dirty = True
                return
            # An unpaired host we can see but cannot talk to. Adopt the id if this is
            # a flat install that has never learned it.
            if self.config.adopt_id(server_id, beacon.get("name")):
                return

    def _start(self, what, path):
        self._pending = what
        self.client.get(path)

    def _finish(self, what):
        if self.client.status != net.DONE:
            self.status = "offline"
            self.detail = self.client.error
            # 403 is the host saying it does not know this badge. Nothing the badge can
            # do about that on its own - it has to be paired again - so say so rather
            # than sitting on "Connecting" forever.
            if self.client.http_status == 403:
                self.rejected = True
            self.dirty = True
            return
        payload = self.client.json()
        if payload is None:
            self.status = "bad reply"
            self.dirty = True
            return

        self._last_ok = time.ticks_ms()
        self.status = "ok"
        self.detail = None
        self.rejected = False
        if what == "stats":
            self.frame = payload
            rev = payload.get("layout_rev")
            if rev is not None and rev != self.layout_rev and self.layout is not None:
                # The host's config changed; pick it up on the next poll.
                self.layout = None
        elif what == "layout":
            self.layout = payload
            self.layout_rev = payload.get("rev", 0)
            self.frame["layout_rev"] = self.layout_rev
            self.apply_layout()
        elif what == "history":
            self.history = payload
        self.dirty = True

    def _graph_keys(self):
        keys = []
        for page in self.page_list:
            if page.get("kind") == "graph":
                for ref in page.get("fields", []):
                    if ref not in keys:
                        keys.append(ref)
        return keys[:6]

    def apply_layout(self):
        theme_name = (self.layout or {}).get("theme", look.DEFAULT)
        theme = look.get(theme_name)
        if theme is not self.theme:
            self.theme = theme
            draw.clear_cache()
        display.backlight(float((self.layout or {}).get("brightness", 0.8)))
        badge.caselights(
            self.theme.case if (self.layout or {}).get("caselights", True) else 0.0)
        if self.page_index >= len(self.page_list):
            self.page_index = 0

    # -- input --------------------------------------------------------------

    def buttons(self):
        if badge.pressed(BUTTON_UP):
            self.turn(-1)
        if badge.pressed(BUTTON_DOWN):
            self.turn(1)
        for name, button in (("a", BUTTON_A), ("b", BUTTON_B), ("c", BUTTON_C)):
            if badge.pressed(button):
                self.send_command(name)

    def send_command(self, which):
        command = ((self.layout or {}).get("buttons") or {}).get(which)
        if not command:
            return
        if self._pending is not None:
            # One request at a time; a button press while polling is dropped rather
            # than queued, because a stale command is worse than a missed one.
            self.note("busy")
            return
        self._pending = "command"
        self.client.post("/v1/command", {"cmd": command})
        self.note(command.replace("_", " "))

    def needs_setup(self):
        """Whether to offer the pairing screens.

        Not just when unpaired: a badge holding credentials a host rejects, or one that
        has never managed a poll, is otherwise stuck with no way to reach setup at all.
        """
        if not self.config.paired or self.rejected:
            return True
        return self.layout is None and self.client.failures >= 3

    def note(self, text):
        self.toast_text = text
        self.toast_until = time.ticks_add(time.ticks_ms(), 1200)
        self.dirty = True

    def tick(self):
        """Notice the things that change with time rather than with an event.

        Without this a page would keep a toast forever and never admit the host had
        gone away, because nothing would mark it for redraw.
        """
        now = time.ticks_ms()
        if self.toast_text and time.ticks_diff(self.toast_until, now) <= 0:
            self.toast_text = None
            self.dirty = True
        stale = time.ticks_diff(now, self._last_ok) > 5000
        if stale != self._was_stale:
            self._was_stale = stale
            self.dirty = True
        page = self.current_page()
        if page is not None and page.get("kind") in pages_module.ANIMATED:
            # This page moves on its own, so it gets a frame regardless of polling.
            self.dirty = True

    # -- drawing ------------------------------------------------------------

    def render(self):
        theme = self.theme
        if not self.config.paired:
            draw.banner(theme, "Not paired", "B to set up",
                        "or run: statsbadge install")
            return
        if self.rejected:
            draw.banner(theme, "Not recognised", self.config.name or self.config.host,
                        "B to pair again")
            return
        if not wifi.is_connected():
            draw.banner(theme, "No WiFi", wifi.status()[1])
            return
        page = self.current_page()
        if page is None:
            hint = "B to set up" if self.needs_setup() else self.detail
            draw.banner(theme, "Connecting",
                        f"{self.config.name or self.config.host}:{self.config.port}",
                        hint)
            return

        subtitle = self.frame.get("sys", {}).get("host") or self.config.name
        if self._was_stale:
            subtitle = self.detail or "offline"
        pages_module.render(page, self.frame, self.history, theme,
                            self.page_index, len(self.page_list), subtitle)
        if self.toast_text:
            draw.toast(theme, self.toast_text)

    # -- exit ---------------------------------------------------------------

    def check_exit(self):
        """HOME held leaves the app. A press alone does nothing, so HOME stays free
        for a menu without stranding anyone."""
        if badge.held(BUTTON_HOME):
            if self.home_since is None:
                self.home_since = badge.ticks
            elif badge.ticks - self.home_since > HOLD_TO_EXIT_MS:
                return True
        else:
            self.home_since = None
        return False


def main():
    draw.prepare()
    load_extensions()
    app = App()

    # Say something before the first fetch lands: a blank screen for a second reads
    # as a hang.
    draw.banner(app.theme, "Stats", "connecting")
    badge.update()

    while not wifi.connect():
        draw.banner(app.theme, "WiFi", wifi.status()[1])
        badge.update()
        if badge.pressed(BUTTON_HOME):
            return

    if not app.config.paired:
        if not pairing_ui().run(app):
            return
    app.apply_layout()

    while True:
        if app.check_exit():
            return
        app.buttons()
        # B selects, as it does on every screen that takes A/B/C. Offered whenever the
        # badge cannot get a usable connection, not only when unpaired, or credentials a
        # host has stopped accepting would be a dead end. A command bound to B is only
        # reachable once a layout has arrived, so the two cannot collide.
        if app.needs_setup() and badge.pressed(BUTTON_B):
            if not pairing_ui().run(app):
                return
            app.rejected = False
            app.layout = None
            app.apply_layout()
        app.poll()
        app.tick()

        # A stats page changes when a poll lands, once a second. Everything between
        # is a frame that would redraw the same picture, so it does not draw at all:
        # `badge.default_clear = None` leaves the framebuffer standing.
        if app.dirty:
            app.render()
            app.dirty = False
        badge.update()


main()
