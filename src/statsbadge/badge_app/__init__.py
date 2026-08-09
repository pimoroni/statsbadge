"""Stats: a host PC's vitals on the badge, paged with UP and DOWN.

UP/DOWN     page through what the host is configured to show
A B C       whatever the host has bound them to, if anything
HOME        open the hosts menu; hold to leave

Where a screen takes A/B/C for itself instead of passing them to the host, they are used
in the order they sit in: A back, B select, C next.

The host settles what the pages are; this fetches them and draws them. Polling is a
generator advanced from the draw loop, and a slow reply costs latency and never a frame.

The heap, panel and light sensor settings below are measured; DEVELOPMENT.md has the
numbers.
"""

import builtins
import gc
import os
import sys
import time

APP_DIR = "/system/apps/stats"
try:
    os.chdir(APP_DIR)
except OSError:
    # Running from a mounted checkout. Locate the app by this file
    # and not by cwd, which under `mpremote mount` is the mount root and not the
    # app directory - so `pages/` would be looked for in the wrong place.
    here = globals().get("__file__")
    APP_DIR = here.rsplit("/", 1)[0] if here and "/" in here else os.getcwd()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Where the installer puts an extension's badge modules, and what goes on sys.path for
# them. Named `ext` and not `pages`: a `pages` directory on sys.path shadows the app's
# pages.py, and an extension importing `pages` would get the directory.
EXT_DIR = "ext"

import look  # noqa: E402

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None


import splash  # noqa: E402  a tiny module, imported before the expensive ones

splash.show()

import draw  # noqa: E402
import net  # noqa: E402
import pages as pages_module  # noqa: E402
import secrets  # noqa: E402
import wifi  # noqa: E402


def pairing_ui():
    """Import the pairing screens on demand: an already-paired badge never shows them."""
    import setup
    return setup


def load_extensions():
    """Import any badge-side modules an extension had pushed into EXT_DIR.

    Each registers a page kind in `pages.EXTRA` on import. A broken extension
    must not take the app down with it, so each is imported inside a try.
    """
    directory = f"{APP_DIR}/{EXT_DIR}"
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

# HOME opens the hosts menu, so the launcher's exit irq is dropped and it is polled
# instead, per BADGEWARE.md. Holding it still leaves.
BUTTON_HOME.irq(None)
HOLD_TO_EXIT_MS = 700

# How much may be allocated between collects, and how often the heap is swept while the
# screen holds still. A collect is 3.9ms; see DEVELOPMENT.md.
GC_THRESHOLD = 256 * 1024
COLLECT_EVERY_MS = 1000

# A revision nothing has, so the first poll asks the host for everything.
NO_REV = -1
# Failed polls before the badge listens for the host at another address. It is a scan a
# frame long, so not on the first one.
HUNT_AFTER = 3
# Failed polls before B opens setup. One, since waiting for three left the screen with
# nothing on it that worked.
SETUP_AFTER = 1

# The smallest change worth asking for. The panel takes a byte, so anything finer lands on
# the level it already shows and only restarts the ramp.
BACKLIGHT_STEP = 1.0 / 255

# The share of the backlight's level a reading of zero still gets.
CASELIGHT_FLOOR = 0.15


# How much of a step to take towards a new ambient reading each poll, and how often to
# take one. Following the sensor directly flickers the panel at every passing hand.
LIGHT_FOLLOW = 0.1
LIGHT_EVERY_MS = 100

# How many reads go into one of those. Sixteen is 256us and halves the ADC's noise.
LIGHT_READS = 16

# Bindings this badge answers itself, and the brightness shares its button steps through.
# A round trip to the host would be slower than the press.
LOCAL_PREFIX = "badge."
BRIGHTNESS_STEPS = (1.0, 0.6, 0.3)

# How many presses can be waiting for the connection, and how long one waits before it is
# dropped. Longer than this is a host that stopped answering.
COMMAND_QUEUE = 4
COMMAND_WAIT_MS = 3000


# How long a page takes to slide on, when the layout asks for it. A quarter of a second
# between pressing for the next page and reading it.
SLIDE_MS = 220
# How long a press waits for another before the slide starts; see slide_due.
SLIDE_WAIT_MS = 120

# How long the panel takes to reach a new brightness. Short enough to answer a button
# press, long enough that the step is a change of light and not a click.
BACKLIGHT_MS = 300
# Where the panel is, and where it was last told to go. Two values, so a new target is
# compared against the ramp's end rather than its moving value.
_backlight_at = 1.0
_backlight_want = 1.0
_backlight_to = None


def backlight(fraction):
    """Set the display brightness, as a 0-1 fraction of what the panel does.

    Clamped: the binding casts to a byte, so a value above 1.0 wraps to a dark panel.
    """
    display.backlight(max(0.0, min(1.0, fraction)))


def backlight_to(fraction, ms=BACKLIGHT_MS, shape=None):
    """Head for a brightness, easing there over `ms`. Zero sets it outright.

    `shape` picks how the ramp is walked. The default eases in and out, for a one-off
    change. A follower retargeting mid-ramp takes LINEAR, or every step starts and ends
    at a standstill and the panel pulses its way to the new level.
    """
    global _backlight_at, _backlight_to, _backlight_want
    fraction = max(0.0, min(1.0, fraction))
    if abs(fraction - _backlight_want) < BACKLIGHT_STEP:
        return
    _backlight_want = fraction
    if not ms:
        _backlight_to = None
        _backlight_at = fraction
        backlight(fraction)
        return
    _backlight_to = tween(_backlight_at, fraction, ms,
                          shape if shape is not None else tween.QUAD_INOUT).start()


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
        self.ambient = None
        self.dimmed = None
        self.dim_step = 0
        self._light_at = 0
        self._swept = 0
        self._pressed_at = time.ticks_ms()
        self._advanced_at = 0
        self.layout_rev = NO_REV
        self.frame = {}
        # Merged into every frame. The host sends these only when its slow_rev is past the
        # one we send it.
        self.slow = {}
        self.slow_rev = NO_REV
        self.history = {}
        self.page_index = 0
        self.status = "starting"
        self.detail = None
        self._lit = False
        # The page images are 307KB each, allocated on the first turn that needs one and
        # written over after that. A deck needs both, a slide-over only `arriving`.
        self.sliding = None
        self.slide_back = False
        self.arriving = None
        self.leaving = None
        self._arriving = None
        self._kept = None
        self._slide_at = 0
        self._slide_from = False
        self.toast_until = 0
        self.toast_text = None
        self.dirty = True
        self._home_at = None

        # One request in flight at a time, cycling stats, then layout or history when due.
        self._next_poll = 0
        self._pending = None
        self._history_due = 0
        # Sent on the next pass rather than the next interval, so the series can be
        # refetched as well as the stats.
        self._queued = None
        # (tick, command) oldest first.
        self._commands = []
        # The newest point's age when the host answered, and when that answer landed:
        # between them, how far back in the series `now` is.
        self._series_age = 0
        self._series_at = 0
        self._last_ok = 0
        self._was_stale = False
        self._next_hunt = 0
        self.rejected = False

        self.page_index = self.config.page
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
            # Arm the wait and drop any slide in flight; slide_due() starts the movement.
            self.sliding = None
            self._slide_at = time.ticks_add(time.ticks_ms(), SLIDE_WAIT_MS)
            self._slide_from = delta < 0
        self.dirty = True

    def slide_due(self, now):
        """Start a waiting page turn once the presses have stopped.

        One slide a burst: each press pushes the deadline out, and the movement runs from
        the page the reader started on to the one they landed on. Two at once drew over
        each other.
        """
        if not self._slide_at or self.sliding is not None:
            return
        if time.ticks_diff(now, self._slide_at) < 0:
            return
        self._slide_at = 0
        style = (self.layout or {}).get("slide") or "off"
        if style == "off":
            self.dirty = True
            return
        self.start_slide(style, self._slide_from)

    def start_slide(self, style, back):
        """Set a page turn moving: the page arriving drawn once, the one leaving kept.

        Drawing into an image is 45ms against 15 on the screen, and keeping the outgoing
        page another 23ms. Paid once on the press, where the ten frames that follow are
        blits at 12 to 15ms. A window also reaches off the left of the screen, which a
        page drawn there cannot.
        """
        page = self.current_page()
        if page is None:
            return
        if self._arriving is None:
            self._arriving = image(look.W, look.H)
        self.draw_page_into(self._arriving, page)
        self.leaving = None
        if style == "deck":
            if self._kept is None:
                self._kept = image(look.W, look.H)
            self._kept.blit(screen, vec2(0, 0))
            self.leaving = self._kept
        self.arriving = self._arriving
        self.slide_back = back
        self.sliding = tween(0.0, 1.0, SLIDE_MS, tween.QUAD_OUT).start()

    def draw_page_into(self, target, page):
        """Render a page somewhere other than the screen.

        `screen` is a builtin, so it is rebound and not passed. An extension's page
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

        # Commands go before polls: a press is user-facing and a poll can wait a frame.
        if self._commands:
            self._send_command()
            return

        if self._queued is not None:
            what, path = self._queued
            self._queued = None
            self._start(what, path)
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

        if self.client.failures >= HUNT_AFTER:
            self.hunt()

        # The rev rides in every stats frame, so a config change is picked up next poll.
        if self.layout is None or self.layout_rev != (
                self.frame.get("layout_rev", self.layout_rev)):
            self._start("layout", "/v1/layout")
            return

        # Queued rather than sent: the stats have to go first, or the badge misses a
        # sample and the plots walk at half pace.
        if self._graph_keys():
            keys = ",".join(self._graph_keys())
            points = (self.layout or {}).get("graph_points", 48)
            self._queued = ("history",
                            f"/v1/history?keys={keys}&points={points}&v=3")
        # Always sent: the parameter is what marks this app as able to read a split frame,
        # and without it the host inlines every group.
        self._start("stats", f"/v1/stats?have={self.slow_rev}")

    def forget_host(self):
        """Drop everything belonging to the host we were talking to.

        Readings, series and revisions are all numbered by whoever sent them, so held on
        they would be drawn under the new host's name until it happened to number one the
        same. A queued command was a press meant for the host we just left.
        """
        self.client.close()
        self.layout = None
        self.layout_rev = NO_REV
        self.history = {}
        self.slow = {}
        self.slow_rev = NO_REV
        self._queued = None
        self._commands = []
        self._series_age = 0
        self._series_at = 0
        self.rejected = False
        draw.clear_cache()

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

        # Ends as soon as a host we hold credentials for answers, so the full scan is only
        # paid for when there is nothing out there.
        for beacon in net.discover(wanted=self.config.hosts):
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
                    self.forget_host()
                    self.note(self.config.name or "switched host")
                    self.dirty = True
                return
            # An unpaired host we can see but cannot talk to. Adopt the id if this is
            # a flat install that has never learned it.
            if self.config.adopt_id(server_id, beacon.get("name")):
                return

    def take_slow(self, frame):
        """Keep the slow half of a frame, and put what we hold into every frame after it.

        The host leaves those groups out once we tell it which revision we hold, so a frame
        that carries them is the one that changed and every frame after it arrives without.
        Which is the trick the layout uses, and grafting it back on is what `layout_rev`
        does two lines away: `self.frame` is replaced outright on every reply.
        """
        rev = frame.get("slow_rev")
        if rev is None:
            # A host too old to split the frame sends every group inline, every time.
            return
        arrived = frame.pop("slow", None)
        if arrived is not None:
            self.slow_rev = rev
            self.slow = arrived
        pages_module.merge_slow(frame, self.slow)

    def _send_command(self):
        """Post the oldest press, having dropped any that waited past the point of use."""
        now = time.ticks_ms()
        waiting = [held for held in self._commands
                   if time.ticks_diff(now, held[1]) <= COMMAND_WAIT_MS]
        if len(waiting) != len(self._commands):
            self.note("dropped")
        self._commands = waiting
        if not self._commands:
            return
        command, _at = self._commands.pop(0)
        self._pending = "command"
        self.client.post("/v1/command", {"cmd": command})

    def _start(self, what, path):
        self._pending = what
        self.client.get(path)

    def _finish(self, what):
        if self.client.status != net.DONE:
            self.status = "offline"
            self.detail = self.client.error
            # 403 is the host refusing this badge. It has to be paired again, so say so
            # instead of sitting on "Connecting" forever.
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
            self.take_slow(payload)
            self.apply_caselights()
        elif what == "layout":
            self.layout = payload
            self.layout_rev = payload.get("rev", 0)
            self.frame["layout_rev"] = self.layout_rev
            self.apply_layout()
        elif what == "history":
            # v=2 wraps the series in the two things a plot needs to place it in time: how far
            # apart the points are, and how old the newest was when the host answered. v=3
            # adds a pair for any ring a source answers for itself, those being on
            # whatever clock the readings are really on - an hour, for a domain's traffic.
            self.history = payload.get("series", payload)
            self._series_age = int(payload.get("age_ms", 0) or 0)
            self._series_at = time.ticks_ms()
            pages_module.note_spacing(payload.get("every_ms", 1000),
                                      (self.layout or {}).get("interval_ms", 1000))
            pages_module.note_series_spacing(payload.get("spacing"))
        self.dirty = True

    def _graph_keys(self):
        """Every field a page draws a series of, which is not only the graph pages.

        A sparkline and a trend draw one too, and asking only for the graphs' fields left
        those pages plotting the live value twice - a flat line whatever the machine was
        doing.
        """
        keys = []
        for page in self.page_list:
            if page.get("kind") not in pages_module.PLOTS:
                continue
            for ref in page.get("fields", []) + [page.get("field")]:
                if ref and ref not in keys:
                    keys.append(ref)
        return keys[:6]

    def apply_layout(self):
        theme_name = (self.layout or {}).get("theme", look.DEFAULT)
        # The host sends the colours, so a theme it has and this app has never heard of
        # still draws. Only the key to fall back on, for a host too old to send them.
        theme = (look.from_palette(theme_name, (self.layout or {}).get("palette"))
                 or look.get(theme_name))
        if theme.key != self.theme.key:
            self.theme = theme
            draw.clear_cache()
        # The first layout to land is the badge coming up, so it takes its brightness
        # and does not ramp to it.
        self.apply_backlight(BACKLIGHT_MS if self._lit else 0)
        self._lit = True
        draw.SMOOTH = bool((self.layout or {}).get("smooth", True))
        draw.ROWS = (self.layout or {}).get("rows", "zebra")
        draw.GAUGE_FILL = (self.layout or {}).get("gauge_fill", "solid")
        pages_module.PLOT_ANIMATION = bool(
            (self.layout or {}).get("plot_animation", False))
        # What the host calls the groups an extension declared. Replaced and not
        # updated: a group dropped from every page should stop being named.
        pages_module.LABELS = (self.layout or {}).get("labels") or {}
        animate = bool((self.layout or {}).get("animate", False))
        if animate != pages_module.ANIMATE:
            pages_module.ANIMATE = animate
            pages_module.sweep_reset()
        self.apply_caselights()
        if self.page_index >= len(self.page_list):
            self.page_index = 0

    def wanted_brightness(self):
        """The brightness the panel should be showing, 0-1.

        The scale is a floor plus what the sensor reads, so `brightness` stays the ceiling
        and ambient only ever takes some of it away. `self.dimmed`, set by the brightness
        button, wins over both.
        """
        wanted = self.dimmed
        if wanted is None:
            wanted = float((self.layout or {}).get("brightness", 0.8))
        if (self.layout or {}).get("auto_brightness") and self.ambient is not None:
            wanted *= look.LIGHT_FLOOR + (1.0 - look.LIGHT_FLOOR) * self.ambient
        return wanted

    def apply_backlight(self, ms=BACKLIGHT_MS, shape=None):
        """Head for that brightness, and take the case lights with it.

        Eased, except at startup: ramping to the first level from full brightness is a
        flash in a dark room.

        The lights follow here and not only on a new layout. A button press would else
        dim the panel and leave four lights at the old level.
        """
        backlight_to(self.wanted_brightness(), ms, shape)
        self.apply_caselights()

    def read_light(self):
        """Follow the room, slowly. Returns True when the panel needs setting again.

        Only while the setting is on. The read is cheap, and off means the brightness is
        left alone.

        Meaned over LIGHT_READS. Whether the move is worth
        making is backlight_to's to answer, since what counts as too small to bother with is
        a step of the panel and not a step of the sensor.
        """
        if not (self.layout or {}).get("auto_brightness"):
            return False
        try:
            total = 0
            for _ in range(LIGHT_READS):
                total += badge.light_level()
        except (AttributeError, OSError):
            return False           # not a Tufty, or no sensor on this board
        raw = total // LIGHT_READS
        fraction = look.ambient_fraction(raw)
        if self.ambient is None:
            self.ambient = fraction
        else:
            self.ambient += (fraction - self.ambient) * LIGHT_FOLLOW
        return True

    def apply_caselights(self):
        """Off, the backlight's level, or a level that follows a reading.

        A case light is one brightness and not a colour, so a theme has nothing to say
        about it. They track the panel: lights burning at full while the screen dims for a
        dark room are the wrong way round.

        A reading maps onto CASELIGHT_FLOOR of that level up to all of it, so an idle
        machine still glows. Dark is what the setting being off looks like. A field the
        host is not sending sits at the floor.
        """
        setting = (self.layout or {}).get("caselights", True)
        if not setting:
            badge.caselights(0.0)
            return
        level = self.wanted_brightness()
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
                if name == "c" and self.current_page() is None:
                    # Only the notice is on screen, so C is no command. It is the
                    # way to ask again without waiting out the backoff.
                    self.retry()
                else:
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

        A local override, dropped when the cycle comes back to the top.
        """
        self.dim_step = (self.dim_step + 1) % len(BRIGHTNESS_STEPS)
        share = BRIGHTNESS_STEPS[self.dim_step]
        base = float((self.layout or {}).get("brightness", 0.8))
        self.dimmed = None if share >= 1.0 else base * share
        self.apply_backlight()
        self.note(f"brightness {round(share * 100)}%")

    def send_command(self, command):
        """Hold a press for the host, to go out as soon as the connection is free.

        One request is in flight at a time and the badge polls every interval, so a press
        that had to find the connection idle would mostly find it busy instead.
        """
        if not command:
            return
        if len(self._commands) >= COMMAND_QUEUE:
            self.note("busy")
            return
        self._commands.append((command, time.ticks_ms()))
        self.note(command.replace("_", " "))

    def needs_setup(self):
        """Whether to offer the pairing screens.

        Also when a host rejects the credentials, or when no poll has ever landed: both
        leave a badge with no other way to reach setup.
        """
        if not self.config.paired or self.rejected:
            return True
        return self.layout is None and self.client.failures >= SETUP_AFTER

    def retry(self):
        """Drop the connection and poll again now, resetting the backoff.

        Polls back off to fifteen seconds apart while a host is silent, which is too long
        to wait on one that has just been woken.
        """
        self.client.close()
        self.client.failures = 0
        self._pending = None
        self._queued = None
        self._commands = []
        self._next_poll = time.ticks_ms()
        self.detail = None
        self.status = "retrying"
        self.note("retrying")

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
        """Notice the things that change with time and not with an event.

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
                # Only while it thins out: fading over the page means redrawing the page.
                self.dirty = True
        stale = time.ticks_diff(now, self._last_ok) > 5000
        if stale != self._was_stale:
            self._was_stale = stale
            self.dirty = True
        self.advance_if_idle(now)
        self.slide_due(now)
        if time.ticks_diff(now, self._light_at) > LIGHT_EVERY_MS:
            self._light_at = now
            if self.read_light():
                # LINEAR over exactly the gap to the next reading, or consecutive steps
                # each ease to a standstill and the panel pulses.
                self.apply_backlight(LIGHT_EVERY_MS, tween.LINEAR)
        page = self.current_page()
        if page is not None and page.get("kind") in pages_module.ANIMATED:
            self.dirty = True
        if pages_module.moving or self.sliding is not None:
            self.dirty = True
        if (pages_module.PLOT_ANIMATION and page is not None
                and page.get("kind") in pages_module.SCROLLS):
            pages_module.BEHIND = pages_module.behind_at(
                self._series_age, time.ticks_diff(now, self._series_at))
            self.dirty = True

    def advance_if_idle(self, now):
        """Page on by itself once nothing has been pressed for a while.

        Off unless `idle_advance_s` is set. The first turn comes as soon as the badge
        counts as idle, the rest every `advance_every_s`.
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
            # The screen a badge sits on when it can reach nothing, so it carries the three
            # ways out as well as the reason.
            title = "Connecting" if not self.detail else self.detail
            draw.banner(theme, title,
                        f"{self.config.name or self.config.host}:{self.config.port}",
                        "C retry   B set up   HOME hosts")
            return

        subtitle = self.subtitle()
        if self._slide_at and time.ticks_diff(self._slide_at, time.ticks_ms()) > 0:
            # A turn is waiting: draw the new title and pip over the standing body. Tested
            # on the deadline, so the body is withheld for at most SLIDE_WAIT_MS.
            draw.furniture(theme, page.get("title", page.get("id", "")),
                           self.page_index, len(self.page_list), subtitle)
        elif self.sliding is not None:
            self.render_sliding(page, theme, subtitle)
        else:
            pages_module.render(page, self.frame, self.history, theme,
                                self.page_index, len(self.page_list), subtitle)
        if self.toast_text:
            draw.toast(theme, self.toast_text, self.toast_fade())

    def render_sliding(self, page, theme, subtitle):
        """A page turn part way through: two cards, placed by a rect out of an image each.

        `over` moves the arriving card alone and leaves the page under it standing; `deck`
        moves both. Only the body band travels; the header and footer went up when the
        turn started. Nothing is rasterised here.
        """
        top, deep = look.BODY_TOP, look.BODY_H
        travel = int(look.W * self.sliding.now)
        if travel >= look.W or self.sliding.done or self.arriving is None:
            if self.arriving is not None:
                # From the image, not a fresh render: a press mid-slide changes the current
                # page, and the turn then landed on a different one than it set off for.
                screen.blit(self.arriving.window(rect(0, top, look.W, deep)), vec2(0, top))
            else:
                pages_module.render(page, self.frame, self.history, theme,
                                    self.page_index, len(self.page_list), subtitle)
            self.sliding = None
            self.arriving = None
            self.leaving = None
            self.dirty = True
            return
        if travel <= 0:
            return
        rest = look.W - travel
        if self.slide_back:
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

    def sweep(self):
        """Collect between frames, at a moment when nothing is waiting on the result.

        Only while the page is resting: 3.9ms is a frame an animated page would drop, and the
        threshold set at launch is what keeps that case in hand. A page redrawn on the poll
        produces a poll's worth of garbage a second, which this covers.
        """
        now = time.ticks_ms()
        if time.ticks_diff(now, self._swept) < COLLECT_EVERY_MS:
            return
        page = self.current_page()
        if page is not None and page.get("kind") in pages_module.ANIMATED:
            return
        if pages_module.moving or self.sliding is not None:
            return
        self._swept = now
        gc.collect()

    def save_page(self):
        """Persist the page index, if it moved. Called on the way out.

        Not per keypress: that is a flash write inside the input handler, and the page is
        not worth one.
        """
        if self.page_index == self._saved_page:
            return
        self.config.page = self.page_index
        self.config.save()
        self._saved_page = self.page_index


def no_network(theme):
    """The command that sets a network, held until HOME."""
    draw.banner(theme, "No WiFi", "no network set",
                'statsbadge install --ssid "..."')
    draw.blit_label("HOME quit", look.SIZE_SMALL, theme.dim,
                    look.W // 2, look.H - 18, align=1)
    badge.update()
    while not badge.pressed(BUTTON_HOME):
        badge.update()


def main():
    global _app
    gc.threshold(GC_THRESHOLD)
    draw.prepare()
    load_extensions()
    app = App()
    _app = app

    # wifi.connect() triggers a fatal_error (no SSID) if WiFi is not configured
    # intercept it and display a dialog recommending `statsbadge install --ssid "..."`
    if not getattr(secrets, "WIFI_SSID", ""):
        no_network(app.theme)
        return

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
        # Drop the press that closed the screen, before buttons() sees it as an edge.
        badge.poll()
    app.apply_layout()

    while True:
        pressed_home = app.home()
        if pressed_home == "exit":
            app.save_page()
            return
        if pressed_home == "menu":
            outcome = pairing_ui().hosts_menu(app)
            # As above: B chose a server, and would otherwise also fire B's binding here.
            badge.poll()
            if outcome == "exit":
                app.save_page()
                return
            app.apply_layout()
            app.dirty = True

        app.buttons()
        # B reaches setup whenever the connection is unusable, not only when unpaired.
        # A host binding for B only arrives with a layout, so the two cannot collide.
        if app.needs_setup() and badge.pressed(BUTTON_B):
            if not pairing_ui().run(app):
                return
            app.forget_host()
            app.apply_layout()
        app.poll()
        app.tick()
        backlight_step()

        # A page changes when a poll lands, once a second, so most frames redraw nothing:
        # `badge.default_clear = None` leaves the framebuffer standing between them.
        if app.dirty:
            app.render()
            app.dirty = False
        badge.update()
        app.sweep()


_app = None


def on_exit():
    """Called by the launcher when HOME quits the app, and on a normal return."""
    if _app is not None:
        _app.save_page()


main()
