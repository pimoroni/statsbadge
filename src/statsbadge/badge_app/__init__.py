"""Stats: a host PC's vitals on the badge, paged with UP and DOWN.

UP/DOWN     page through what the host is configured to show
A B C       whatever the host has bound them to, if anything
HOME        open the hosts menu; hold to leave

Where a screen takes A/B/C for its own input rather than passing them to the host, they
are used in the order they sit in: A back, B select, C next.

The host decides what the pages are; this fetches them and draws them. Polling is a
generator advanced from the draw loop, so a slow reply costs latency and never a
frame.
"""

import builtins
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


import splash  # noqa: E402  a tiny module, imported before the expensive ones

splash.show()

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

# HOME opens the hosts menu, so the launcher's exit irq is taken off it and it is polled
# instead - the idiom BADGEWARE.md describes. Holding it still leaves, and the way out has
# to stay: a press alone must not strand anyone.
BUTTON_HOME.irq(None)
HOLD_TO_EXIT_MS = 700

# State writes /state/<app>.json, the same file net.Config keeps the pairing in. Both read
# and write it, so page saves go through State.modify, which merges.
STATE_APP = "stats"

# The lowest display.backlight value that lights the panel. The driver raises its input
# to the power of 2.8 to get the PWM duty, so 0.5 is 14% duty, 0.25 is 2% and 0.1 is
# under a fifth of one percent: the bottom half of the range is the difference between
# off and nearly off. A configured brightness is spread over the half that does
# something instead. Drop this once display.backlight covers its own range.
BACKLIGHT_FLOOR = 0.5

# The share of the theme's case light level a reading of zero still gets.
CASELIGHT_FLOOR = 0.15


# How much of a step to take towards a new ambient reading each poll, and how often to take
# one. The sensor is a phototransistor a hand can shadow, so following it directly makes the
# panel flicker at every passing movement: a fifth of the gap every 250ms is about a second
# and a half from a curtain being opened.
LIGHT_FOLLOW = 0.2
LIGHT_EVERY_MS = 250

# Bindings this badge answers itself, and the shares of the configured brightness its button
# steps through. Paging and the panel are the badge's own business and a round trip to the host
# would be slower than the press; the host never sees these.
LOCAL_PREFIX = "badge."
BRIGHTNESS_STEPS = (1.0, 0.6, 0.3)


# How long a page takes to slide on, when the layout asks for that. Short: it is a quarter
# of a second between pressing for the next page and being able to read it.
SLIDE_MS = 220

# How long the panel takes to reach a new brightness. Short enough to answer a button
# press, long enough that the step is a change of light rather than a click.
BACKLIGHT_MS = 300
_backlight_at = 1.0
_backlight_to = None


def backlight(fraction):
    """Set the display brightness, over the range the panel responds to.

    Clamped as well as scaled: the binding casts to uint8_t, so anything over 1.0 wraps
    and blanks the screen over a framebuffer that still dumps perfectly.
    """
    fraction = max(0.0, min(1.0, fraction))
    display.backlight(BACKLIGHT_FLOOR + (1.0 - BACKLIGHT_FLOOR) * fraction)


def backlight_to(fraction, ease=True):
    """Head for a brightness, easing there over BACKLIGHT_MS unless told not to.

    A step is the one thing on this badge that is unmissable however small, because the
    whole panel moves at once: cycling the button or a curtain opening both read as a click
    where a ramp reads as the light changing. The sensor is already smoothed by
    LIGHT_FOLLOW, which stops the panel chasing a passing hand; this is about the step
    itself.
    """
    global _backlight_at, _backlight_to
    fraction = max(0.0, min(1.0, fraction))
    if not ease or abs(fraction - _backlight_at) < 0.005:
        _backlight_to = None
        _backlight_at = fraction
        backlight(fraction)
        return
    _backlight_to = tween(_backlight_at, fraction, BACKLIGHT_MS, tween.QUAD_INOUT).start()


def backlight_step():
    """Move the panel along its ramp, if it is on one. Called every frame."""
    global _backlight_at, _backlight_to
    if _backlight_to is None:
        return
    _backlight_at = _backlight_to.now
    backlight(_backlight_at)
    if _backlight_to.done:
        _backlight_to = None


class App:
    def __init__(self):
        self.config = net.Config()
        self.client = net.Client(self.config)
        self.theme = look.get(look.DEFAULT)
        self.layout = None
        # Where the ambient follower has got to, and the brightest the sensor has read.
        self.ambient = None
        self.light_ceiling = look.LIGHT_BRIGHT
        # A local override of the configured brightness, for the button that cycles it.
        self.dimmed = None
        self.dim_step = 0
        self._light_at = 0
        # When a button was last touched, and when the badge last turned a page by itself.
        self._pressed_at = time.ticks_ms()
        self._advanced_at = 0
        self.layout_rev = -1
        self.frame = {}
        self.history = {}
        self.page_index = 0
        self.status = "starting"
        self.detail = None
        # Whether the panel has been set once, so the first level is taken and not ramped to.
        self._lit = False
        # The page turn in progress, if the layout asks for one: the two cards, which way it
        # is going, and the images behind them. 307KB each, allocated on the first turn that
        # needs one and written over after that - a deck needs both, a slide-over only the
        # page arriving.
        self.sliding = None
        self.slide_back = False
        self.arriving = None
        self.leaving = None
        self._arriving = None
        self._kept = None
        self.toast_until = 0
        self.toast_text = None
        self.dirty = True
        self._home_at = None

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
        self._saved_page = self.page_index

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
        pages_module.sweep_reset()
        style = (self.layout or {}).get("slide") or "off"
        if style != "off" and len(pages) > 1:
            self.start_slide(style, delta < 0)
        self.dirty = True

    def start_slide(self, style, back):
        """Set a page turn moving: the page arriving drawn once, the one leaving kept.

        Both cards are then blits, which is what makes the direction free - a window cannot
        start at a negative origin, so a page cannot be *drawn* part way off the left of the
        screen, but a rect out of an image can be put anywhere. It also means the arriving
        page is rendered once for the turn instead of once a frame.

        The setup is the expensive part of a turn: 45ms to draw a page into an image against
        15 to draw it on the screen, because an image is on the heap in PSRAM where the
        framebuffer is SRAM, and another 23ms to keep the outgoing page for a deck. Paid once
        on the press, against 12 to 15ms a frame for the ten frames that follow.
        """
        page = self.current_page()
        if page is None:
            return
        if self._arriving is None:
            self._arriving = image(look.W, look.H)
        self.draw_page_into(self._arriving, page)
        # The header and footer belong to the page you are on, not to the movement: the
        # subtitle is the same host either way, and a pip row sliding past carries a mark for
        # a page nobody is going to. So they change now, and only the body travels.
        self._arriving.font = draw.FONT
        screen.blit(self._arriving.window(rect(0, 0, look.W, look.HEADER_H)), vec2(0, 0))
        screen.blit(self._arriving.window(rect(0, look.H - look.FOOTER_H, look.W,
                                              look.FOOTER_H)),
                    vec2(0, look.H - look.FOOTER_H))
        self.leaving = None
        if style == "deck":
            # Whatever was on the screen, toast and all: that is what was there to look at.
            if self._kept is None:
                self._kept = image(look.W, look.H)
            self._kept.blit(screen, vec2(0, 0))
            self.leaving = self._kept
        self.arriving = self._arriving
        self.slide_back = back
        self.sliding = tween(0.0, 1.0, SLIDE_MS, tween.QUAD_OUT).start()

    def draw_page_into(self, target, page):
        """Render a page somewhere other than the screen.

        `screen` is a builtin, so it is rebound rather than passed: an extension's page
        renderer draws through the same name and would otherwise put its clock face on the
        screen while the app drew everything else into the image. Rebound from whatever
        `screen` is *now* - `badge.mode` replaces it, so a copy taken at import time is the
        160x120 screen the app started with, and a 320-wide page drawn into that wraps two
        rows into one.
        """
        was = screen
        target.font = draw.FONT
        target.antialias = image.X4
        builtins.screen = target
        try:
            pages_module.render(page, self.frame, self.history, self.theme,
                                self.page_index, len(self.page_list), self.subtitle())
        finally:
            builtins.screen = was

    def subtitle(self):
        if self._was_stale:
            return self.detail or "offline"
        return self.frame.get("sys", {}).get("host") or self.config.name

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

        # One the badge has never had, or one the host has revised: the rev rides in every
        # stats frame, so a config change is picked up on the next poll. What is on screen
        # stays there until the new layout lands, which is a page swapping rather than the
        # display dropping out for a second.
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
            # Only meaningful when the lights follow a reading, and cheap once a second.
            self.apply_caselights()
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
        # The host sends the colours, so a theme it has and this app has never heard of
        # still draws. Only its own name to fall back on, for a host too old to send them.
        theme = (look.from_palette(theme_name, (self.layout or {}).get("palette"))
                 or look.get(theme_name))
        if theme.name != self.theme.name or theme is not self.theme:
            self.theme = theme
            draw.clear_cache()
        # The first layout to land is the badge coming up, so it takes its brightness
        # rather than ramping to it.
        self.apply_backlight(self._lit)
        self._lit = True
        draw.SMOOTH = bool((self.layout or {}).get("smooth", True))
        animate = bool((self.layout or {}).get("animate", False))
        if animate != pages_module.ANIMATE:
            pages_module.ANIMATE = animate
            pages_module.sweep_reset()
        self.apply_caselights()
        if self.page_index >= len(self.page_list):
            self.page_index = 0

    def apply_backlight(self, ease=True):
        """The configured brightness, scaled by the room if the setting says so.

        The scale is a floor plus what the sensor reads, so `brightness` stays the ceiling
        the user asked for and ambient only ever takes some of it away. The button that
        cycles brightness overrides the configured level until the next press, since
        someone reaching for it wants this badge dimmer now and not a config edit.

        Eased, except at startup: the first level is what the badge should have come up at,
        and ramping to it from full brightness is a flash in a dark room.
        """
        wanted = self.dimmed
        if wanted is None:
            wanted = float((self.layout or {}).get("brightness", 0.8))
        if (self.layout or {}).get("auto_brightness") and self.ambient is not None:
            wanted *= look.LIGHT_FLOOR + (1.0 - look.LIGHT_FLOOR) * self.ambient
        backlight_to(wanted, ease)

    def read_light(self):
        """Follow the room, slowly. Returns True when the panel wants setting again.

        Only while the setting is on: the read is cheap but the point of the setting being
        off is that nothing touches the brightness.
        """
        if not (self.layout or {}).get("auto_brightness"):
            return False
        try:
            raw = badge.light_level()
        except (AttributeError, OSError):
            return False           # not a Tufty, or no sensor on this board
        if raw > self.light_ceiling:
            self.light_ceiling = raw
        fraction = look.ambient_fraction(raw, self.light_ceiling)
        if self.ambient is None:
            self.ambient = fraction
        else:
            moved = (fraction - self.ambient) * LIGHT_FOLLOW
            # Under a step of the sensor's own resolution there is nothing to follow.
            if abs(moved) < 0.005:
                return False
            self.ambient += moved
        return True

    def apply_caselights(self):
        """Off, the theme's own level, or a level that follows a reading.

        A reading maps onto CASELIGHT_FLOOR of the theme's level up to all of it, so an
        idle machine still glows: dark is what the setting being off looks like, and the
        two should not be the same. A field the host is not sending sits at the floor.
        """
        setting = (self.layout or {}).get("caselights", True)
        if not setting:
            badge.caselights(0.0)
            return
        level = self.theme.case
        if isinstance(setting, str):
            fraction = pages_module.fraction_of(
                setting, pages_module.value_of(self.frame, setting)) or 0.0
            level *= CASELIGHT_FLOOR + (1.0 - CASELIGHT_FLOOR) * fraction
        badge.caselights(level)

    # -- input --------------------------------------------------------------

    def buttons(self):
        touched = False
        if badge.pressed(BUTTON_UP):
            self.turn(-1)
            touched = True
        if badge.pressed(BUTTON_DOWN):
            self.turn(1)
            touched = True
        for name, button in (("a", BUTTON_A), ("b", BUTTON_B), ("c", BUTTON_C)):
            if badge.pressed(button):
                self.press(name)
                touched = True
        if touched:
            # Only a press counts as being touched: the page turns this class makes for
            # itself must not, or the first one would put the badge back to sleep.
            self._pressed_at = time.ticks_ms()
            self._advanced_at = 0

    def press(self, which):
        """What a button is bound to: something this badge does, or a host command."""
        binding = ((self.layout or {}).get("buttons") or {}).get(which)
        if not binding:
            return
        if binding.startswith(LOCAL_PREFIX):
            self.local(binding)
        else:
            self.send_command(binding)

    def local(self, action):
        if action == "badge.prev":
            self.turn(-1)
        elif action == "badge.next":
            self.turn(1)
        elif action == "badge.brightness":
            self.cycle_brightness()

    def cycle_brightness(self):
        """Step the panel down and round again, over the configured level.

        A local override rather than a config edit: someone reaching for the button wants
        this badge dimmer now. Back at the top it hands control to the config again.
        """
        self.dim_step = (self.dim_step + 1) % len(BRIGHTNESS_STEPS)
        share = BRIGHTNESS_STEPS[self.dim_step]
        base = float((self.layout or {}).get("brightness", 0.8))
        self.dimmed = None if share >= 1.0 else base * share
        self.apply_backlight()
        self.note(f"brightness {round(share * 100)}%")

    def send_command(self, command):
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

    def toast_fade(self):
        """How solid the toast should be drawn: 1 while it is being read, 0 once it is gone.

        Time, not a frame count, so the note leaves at the same speed whatever the page it
        is sitting on costs to redraw.
        """
        left = time.ticks_diff(self.toast_until, time.ticks_ms())
        if left >= draw.TOAST_FADE_MS:
            return 1.0
        return max(0.0, left / draw.TOAST_FADE_MS)

    def tick(self):
        """Notice the things that change with time rather than with an event.

        Without this a page would keep a toast forever and never admit the host had
        gone away, because nothing would mark it for redraw.
        """
        now = time.ticks_ms()
        if self.toast_text:
            left = time.ticks_diff(self.toast_until, now)
            if left <= 0:
                self.toast_text = None
                self.dirty = True
            elif left < draw.TOAST_FADE_MS:
                # Frames while it thins out and not before: the page under the note has to
                # be redrawn for it to fade over anything, and it holds for most of its life.
                self.dirty = True
        stale = time.ticks_diff(now, self._last_ok) > 5000
        if stale != self._was_stale:
            self._was_stale = stale
            self.dirty = True
        self.advance_if_idle(now)
        if time.ticks_diff(now, self._light_at) > LIGHT_EVERY_MS:
            self._light_at = now
            if self.read_light():
                self.apply_backlight()
        page = self.current_page()
        if page is not None and page.get("kind") in pages_module.ANIMATED:
            # This page moves on its own, so it gets a frame regardless of polling.
            self.dirty = True
        if pages_module.moving or self.sliding is not None:
            # A gauge is part way to its reading, or a page is part way on. Frames only
            # while that is true, so a sweeping page costs a third of a second's drawing
            # and not the whole second.
            self.dirty = True
        if pages_module.ANIMATE and page is not None and page.get("kind") in pages_module.PLOTS:
            # A plot walks left the whole time between readings, so unlike a gauge it is
            # never resting: it wants every frame, the way the waterfall does.
            pages_module.PHASE = self.poll_phase(now)
            self.dirty = True

    def poll_phase(self, now):
        """How far through the interval between polls this frame is, 0 to 1.

        From the reading's own arrival rather than a frame count, so a plot walks at the
        speed the readings actually come and stops at a sample's width when one is late.
        """
        interval = int((self.layout or {}).get("interval_ms", 1000)) or 1000
        since = time.ticks_diff(now, self._last_ok)
        if since <= 0:
            return 0.0
        return 1.0 if since >= interval else since / interval

    def advance_if_idle(self, now):
        """Page on by itself when nobody has pressed anything for a while.

        Off unless a timeout is configured, which is the default: a display that moves on
        its own is a choice, and one that does it while somebody is reading is a nuisance.
        The first turn comes as soon as the badge counts as idle, since the reader stopped
        that long ago already, and the rest follow at the configured pace.
        """
        after = int((self.layout or {}).get("idle_advance_s", 0))
        if not after or len(self.page_list) < 2:
            return
        if time.ticks_diff(now, self._pressed_at) < after * 1000:
            return
        every = max(1, int((self.layout or {}).get("advance_every_s", 10)))
        if self._advanced_at and time.ticks_diff(now, self._advanced_at) < every * 1000:
            return
        self._advanced_at = now
        self.turn(1)

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

        subtitle = self.subtitle()
        if self.sliding is None:
            pages_module.render(page, self.frame, self.history, theme,
                                self.page_index, len(self.page_list), subtitle)
        else:
            self.render_sliding(page, theme, subtitle)
        if self.toast_text:
            draw.toast(theme, self.toast_text, self.toast_fade())

    def render_sliding(self, page, theme, subtitle):
        """A page turn part way through: two cards, placed by a rect out of an image each.

        The next page comes in from the right and the previous one from the left, which is
        the direction the reader pressed. `over` moves only the arriving card and leaves the
        page underneath standing; `deck` moves both, the one leaving going the other way.

        Only the body band travels: the header and footer were put in place when the turn
        started, since they belong to the page rather than to the movement.

        A blit costs its pixels, so `over` averages half a band a frame and a deck a whole
        one. Nothing is rasterised - the arriving page was drawn once when the turn happened.
        """
        travel = int(look.W * self.sliding.now)
        if travel >= look.W or self.sliding.done or self.arriving is None:
            self.sliding = None
            self.arriving = None
            self.leaving = None
            pages_module.render(page, self.frame, self.history, theme,
                                self.page_index, len(self.page_list), subtitle)
            return
        if travel <= 0:
            return
        rest = look.W - travel
        top, deep = look.BODY_TOP, look.BODY_H
        if self.slide_back:
            # Arriving from the left, so its right hand edge is what shows first.
            screen.blit(self.arriving.window(rect(rest, top, travel, deep)), vec2(0, top))
            if self.leaving is not None:
                screen.blit(self.leaving.window(rect(0, top, rest, deep)),
                            vec2(travel, top))
        else:
            if self.leaving is not None:
                screen.blit(self.leaving.window(rect(travel, top, rest, deep)),
                            vec2(0, top))
            screen.blit(self.arriving.window(rect(0, top, travel, deep)), vec2(rest, top))

    # -- exit ---------------------------------------------------------------

    def home(self):
        """What HOME did this frame: None, "menu" or "exit"."""
        if badge.pressed(BUTTON_HOME):
            self._home_at = badge.ticks
            self._pressed_at = time.ticks_ms()
            self._advanced_at = 0
            return None
        if self._home_at is None:
            return None
        if badge.held(BUTTON_HOME):
            if badge.ticks - self._home_at > HOLD_TO_EXIT_MS:
                self._home_at = None
                return "exit"
            return None
        self._home_at = None
        return "menu"

    def save_page(self):
        """Persist the page index, if it moved. Called on the way out.

        modify, not save: save replaces the file and would drop the pairing that lives
        in it. Not called per keypress - that is a flash write inside the input handler,
        and the page is not worth one.
        """
        if self.page_index == self._saved_page:
            return
        State.modify(STATE_APP, {"page": self.page_index})
        self._saved_page = self.page_index


def main():
    global _app
    draw.prepare()
    load_extensions()
    app = App()
    _app = app

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
        pressed_home = app.home()
        if pressed_home == "exit":
            app.save_page()
            return
        if pressed_home == "menu":
            if pairing_ui().hosts_menu(app) == "exit":
                app.save_page()
                return
            app.apply_layout()
            app.dirty = True

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
        # The panel's own ramp, which owes nothing to drawing: a frame that redraws
        # nothing still moves the brightness along.
        backlight_step()

        # A stats page changes when a poll lands, once a second. Everything between
        # is a frame that would redraw the same picture, so it does not draw at all:
        # `badge.default_clear = None` leaves the framebuffer standing.
        if app.dirty:
            app.render()
            app.dirty = False
        badge.update()


_app = None


def on_exit():
    """Called by the launcher when HOME quits the app, and on a normal return."""
    if _app is not None:
        _app.save_page()


main()
