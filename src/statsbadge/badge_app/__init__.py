"""Stats: a host PC's vitals on the badge, paged with UP and DOWN.

UP/DOWN     page through what the host is configured to show
A B C       whatever the host has bound them to, if anything
HOME        open the hosts menu; hold to leave

Where a screen takes A/B/C for itself instead of passing them to the host, they are used
in the order they sit in: A back, B select, C next.

The host settles what the pages are; this fetches them and draws them. Polling is a
generator advanced from the draw loop, and a slow reply costs latency and never a frame.

Measured on the board, since several settings below are otherwise arbitrary.

**The heap.** Left alone the collector runs only when an allocation fails, which on 8MB of
PSRAM lets megabytes pile up and leaves the free list in pieces: 71KB largest contiguous
run with 7MB free, from tools/mem_probe.py. A collect is 3.9ms. GC_THRESHOLD covers an
animated page, where a frame allocates up to 15KB and a collect every seventeen frames
amortises to 0.23ms; COLLECT_EVERY_MS sweeps a resting page, where the pause costs nothing.

**The panel.** The backlight driver raises its input to the power of 2.8 for the PWM duty,
so BACKLIGHT_FLOOR is 3.4% of duty and below it the panel is dark whatever the arithmetic
says; tools/backlight_floor.py measures it. display.backlight() is cast to a byte before
that correction, so a change under BACKLIGHT_STEP sets the panel to what it already shows
and only restarts the ramp.

**The light sensor.** A phototransistor a hand can shadow, read through the 12-bit ADC with
a couple of counts of noise either way. With the room and the panel held still, 256 reads
spanned 64-80 of the raw u16. That is nothing against the 4500 a lit room reads, but
darkness reads 48 and ambient_fraction is logarithmic, so the noise costs most where the
curve is steepest. LIGHT_READS of 16 is 256us and halves it.
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

    Each registers a page kind in `pages.EXTRA` on import. A broken extension
    must not take the app down with it, so each is imported inside a try.

    The directory is `ext`, not `pages`. A directory named `pages` on sys.path shadows
    the app's `pages.py`, and an extension importing `pages` would get the directory.
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

# HOME opens the hosts menu, so the launcher's exit irq comes off it and it is polled, the
# idiom BADGEWARE.md describes. Holding it still leaves, and that way out has to stay.
BUTTON_HOME.irq(None)
HOLD_TO_EXIT_MS = 700

# How much may be allocated between collects, and how often the heap is swept while the
# screen holds still; see the docstring.
GC_THRESHOLD = 256 * 1024
COLLECT_EVERY_MS = 1000

# State writes /state/<app>.json, the same file net.Config keeps the pairing in. Both read
# and write it, and page saves go through State.modify, which merges.
STATE_APP = "stats"

# The lowest display.backlight value that lights the panel, which every brightness is
# measured up from; see the docstring.
BACKLIGHT_FLOOR = 0.3

# The smallest change worth asking for; see the docstring.
BACKLIGHT_STEP = 1.0 / 255 / (1.0 - BACKLIGHT_FLOOR)

# The share of the theme's case light level a reading of zero still gets.
CASELIGHT_FLOOR = 0.15


# How much of a step to take towards a new ambient reading each poll, and how often to
# take one. Following the sensor directly flickers the panel at every passing hand.
LIGHT_FOLLOW = 0.1
LIGHT_EVERY_MS = 100

# How many reads go into one of those, against the ADC's noise; see the docstring.
LIGHT_READS = 16

# Bindings this badge answers itself, and the shares of the configured brightness its
# button steps through. Paging and the panel are the badge's business, and a round trip to
# the host would be slower than the press.
LOCAL_PREFIX = "badge."
BRIGHTNESS_STEPS = (1.0, 0.6, 0.3)

# How many presses can be waiting for the connection, and how long one waits before it is
# dropped. Longer than this is a host that stopped answering.
COMMAND_QUEUE = 4
COMMAND_WAIT_MS = 3000


# How long a page takes to slide on, when the layout asks for it. A quarter of a second
# between pressing for the next page and reading it.
SLIDE_MS = 220
# How long a press waits for another before the slide starts. Five quick presses are one
# slide onto the page they landed on, and the destination is only settled once they stop.
SLIDE_WAIT_MS = 120

# How long the panel takes to reach a new brightness. Short enough to answer a button
# press, long enough that the step is a change of light and not a click.
BACKLIGHT_MS = 300
# Where the panel is, and where it was last told to go. Both, because a ramp in flight has
# not arrived: comparing a new target against the moving one would let a slow drift restart
# the ramp every poll.
_backlight_at = 1.0
_backlight_want = 1.0
_backlight_to = None


def backlight(fraction):
    """Set the display brightness, over the range the panel responds to.

    Clamped as well as scaled: the binding casts to uint8_t, so anything over 1.0 wraps
    and blanks the screen over a framebuffer that still dumps perfectly.
    """
    fraction = max(0.0, min(1.0, fraction))
    display.backlight(BACKLIGHT_FLOOR + (1.0 - BACKLIGHT_FLOOR) * fraction)


def backlight_to(fraction, ms=BACKLIGHT_MS, shape=None):
    """Head for a brightness, easing there over `ms`. Zero sets it outright.

    A step is unmissable however small, the whole panel moving at once. Cycling the button
    or a curtain opening both land as a click where a ramp lands as the light changing.

    `shape` picks how the ramp is walked. The default eases in and out, which suits one
    change somebody asked for. A follower giving a new target before the last has arrived
    takes LINEAR, or every step starts and ends at a standstill and the panel pulses its
    way to the new level.
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
        # Where the ambient follower has got to.
        self.ambient = None
        # A local override of the configured brightness, for the button that cycles it.
        self.dimmed = None
        self.dim_step = 0
        self._light_at = 0
        self._swept = 0
        # When a button was last touched, and when the badge last turned a page by itself.
        self._pressed_at = time.ticks_ms()
        self._advanced_at = 0
        self.layout_rev = -1
        self.frame = {}
        # The half of the frame that hardly ever changes - a domain's traffic, fetched by
        # the host once a minute - kept here and merged into every frame. The host sends it
        # only when `slow_rev` moves past what we tell it we hold, and -1 is a revision
        # nothing ever has, so the first poll asks for all of it.
        self.slow = {}
        self.slow_rev = -1
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
        # A turn waiting for the presses to stop: when to start moving, and which way.
        self._slide_at = 0
        self._slide_from = False
        self.toast_until = 0
        self.toast_text = None
        self.dirty = True
        self._home_at = None

        # Poll state: one request in flight at a time, cycling stats, then layout or
        # history when they are due.
        self._next_poll = 0
        self._pending = None
        self._history_due = 0
        # A request to make on the very next pass and not at the next interval, letting
        # the series be refetched *as well as* the stats.
        self._queued = None
        # Presses waiting for the connection, oldest first, each with the tick it happened on.
        self._commands = []
        # How old the newest point in the series was when the host answered, and when that
        # answer landed here: between them, how far back in the series `now` is.
        self._series_age = 0
        self._series_at = 0
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
            # The movement waits for the presses to stop. Until then `render` puts up the
            # title and the pip for where this is going and leaves the body standing.
            #
            # A press during a slide abandons it where it stands. Queueing would give a
            # slide per press, each one late, and would stick the pip for as long as the
            # movement lasted. Paging through five pages is one transition onto the fifth.
            self.sliding = None
            self._slide_at = time.ticks_add(time.ticks_ms(), SLIDE_WAIT_MS)
            self._slide_from = delta < 0
        self.dirty = True

    def slide_due(self, now):
        """Start a waiting page turn once the presses have stopped.

        One slide a burst. While the wait keeps being pushed out the body stays where it
        is, and the movement that eventually runs goes from the page the reader was on to
        the one they landed on. Two at once drew over each other.
        """
        if not self._slide_at or self.sliding is not None:
            return
        if time.ticks_diff(now, self._slide_at) < 0:
            return
        # Only reached with nothing in flight: a press abandons the slide it lands in, so a
        # waiting turn never has one to queue behind.
        self._slide_at = 0
        style = (self.layout or {}).get("slide") or "off"
        if style == "off":
            self.dirty = True
            return
        self.start_slide(style, self._slide_from)

    def start_slide(self, style, back):
        """Set a page turn moving: the page arriving drawn once, the one leaving kept.

        Both cards are then blits, which makes the direction free. A window cannot start
        at a negative origin, so a page cannot be *drawn* part way off the left of the
        screen, where a rect out of an image goes anywhere. The arriving page is also
        rendered once for the turn and not once a frame.

        The setup is the expensive part: 45ms to draw a page into an image against 15 to
        draw it on the screen, an image being on the heap in PSRAM where the framebuffer is
        SRAM. Another 23ms keeps the outgoing page for a deck. Paid once on the press,
        against 12 to 15ms a frame for the ten frames that follow.
        """
        page = self.current_page()
        if page is None:
            return
        if self._arriving is None:
            self._arriving = image(look.W, look.H)
        self.draw_page_into(self._arriving, page)
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

        # Ahead of anything the badge asks for itself: somebody is standing there waiting
        # for this, and a poll can wait a frame.
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

        if self.client.failures >= 3:
            self.hunt()

        # One the badge has never had, or one the host has revised: the rev rides in every
        # stats frame, so a config change is picked up on the next poll. What is on screen
        # stays there until the new layout lands, showing as a page swapping and not as the
        # display dropping out for a second.
        if self.layout is None or self.layout_rev != (
                self.frame.get("layout_rev", self.layout_rev)):
            self._start("layout", "/v1/layout")
            return

        # The series follows the stats and never takes their turn. A skipped stats poll
        # is a sample the badge never sees, which shows as the host slowing down and walks
        # the plots at half pace.
        #
        # Queued and not sent now, one request being in flight at a time, and it has to be
        # the stats: they pair a new sample with the walk being restarted.
        #
        # Every poll, since a plot draws the series, and v=3 carries the spacing and the
        # age of the newest point.
        if self._graph_keys():
            keys = ",".join(self._graph_keys())
            points = (self.layout or {}).get("graph_points", 48)
            self._queued = ("history",
                            f"/v1/history?keys={keys}&points={points}&v=3")
        # Which slow readings we already hold, for the host to leave out. Always sent,
        # since asking marks this app as able to read them. Without the parameter the host
        # puts every group in the frame, as an older app needs.
        self._start("stats", f"/v1/stats?have={self.slow_rev}")

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
                    # Another host's slow readings are not this one's, and its revisions
                    # belong to it. Held on, they would be drawn under the new host's name
                    # until it happened to number one the same.
                    self.slow = {}
                    self.slow_rev = -1
                    self._queued = None
                    # A press meant for the host we just left.
                    self._commands = []
                    self._series_at = 0
                    draw.clear_cache()
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
            # A plot walks between the host's samples, and the pace comes off the frame.
            # The series only advances from the reading it carries when this badge saw
            # every sample.
            #
            # Polling slower than the host samples means several arrived at once and this
            # frame holds the last, so appending one point leaves the series behind and the
            # next sync jumps it forward.

            # Only meaningful when the lights follow a reading, and cheap once a second.
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
        if theme.name != self.theme.name or theme is not self.theme:
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

    def apply_backlight(self, ms=BACKLIGHT_MS, shape=None):
        """The configured brightness, scaled by the room if the setting says so.

        The scale is a floor plus what the sensor reads, so `brightness` stays the ceiling
        the user asked for and ambient only ever takes some of it away. The button that
        cycles brightness overrides the configured level until the next press, since
        someone reaching for it wants this badge dimmer now and not a config edit.

        Eased, except at startup. The first level is where the badge should have come up,
        and ramping to it from full brightness is a flash in a dark room.
        """
        wanted = self.dimmed
        if wanted is None:
            wanted = float((self.layout or {}).get("brightness", 0.8))
        if (self.layout or {}).get("auto_brightness") and self.ambient is not None:
            wanted *= look.LIGHT_FLOOR + (1.0 - look.LIGHT_FLOOR) * self.ambient
        backlight_to(wanted, ms, shape)

    def read_light(self):
        """Follow the room, slowly. Returns True when the panel needs setting again.

        Only while the setting is on. The read is cheap, and off means the brightness is
        left alone.

        Meaned over LIGHT_READS, and never taken as one reading. Whether the move is worth
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

        A local override and not a config edit. Someone reaching for the button expects
        this badge dimmer now. Back at the top it hands control to the config again.
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

        Not just when unpaired: a badge holding credentials a host rejects, or one that has
        never managed a poll, is otherwise stuck with no way to reach setup at all. One failed
        poll is enough - waiting for three left the only screen with nothing on it that does
        anything, which is the state somebody is in when they need setup most.
        """
        if not self.config.paired or self.rejected:
            return True
        return self.layout is None and self.client.failures >= 1

    def retry(self):
        """Drop the connection and poll again now.

        Polls back off to fifteen seconds apart while a host is not answering, which is right
        for a sleeping PC and no use at all to somebody standing there having just woken it.
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
                # Frames while it thins out and not before: the page under the note has to
                # be redrawn for it to fade over anything, and it holds for most of its life.
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
                # Over the gap to the next reading, so the steps of a follower that is
                # still moving run into one another and do not pulse.
                self.apply_backlight(LIGHT_EVERY_MS, tween.LINEAR)
        page = self.current_page()
        if page is not None and page.get("kind") in pages_module.ANIMATED:
            # This page moves unprompted, and gets a frame regardless of polling.
            self.dirty = True
        if pages_module.moving or self.sliding is not None:
            # A gauge is part way to its reading, or a page is part way on. Frames only
            # while that is true, so a sweeping page costs a third of a second's drawing
            # and not the whole second.
            self.dirty = True
        if (pages_module.PLOT_ANIMATION and page is not None
                and page.get("kind") in pages_module.SCROLLS):
            # A plot walks left the whole time between readings, and unlike a gauge it
            # never rests. Every frame, the way the waterfall goes.
            pages_module.BEHIND = pages_module.behind_at(
                self._series_age, time.ticks_diff(now, self._series_at))
            self.dirty = True

    def advance_if_idle(self, now):
        """Page on by itself when nobody has pressed anything for a while.

        Off unless a timeout is configured, which is the default: a display that moves on
        unprompted is a choice, and one doing it while somebody reads is a nuisance.
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
            # Whatever went wrong, this screen has to say what can be done about it: it is the
            # one a badge sits on when it cannot reach anything, and the reason alone left
            # somebody with a message and no way out.
            title = "Connecting" if not self.detail else self.detail
            draw.banner(theme, title,
                        f"{self.config.name or self.config.host}:{self.config.port}",
                        "C retry   B set up   HOME hosts")
            return

        subtitle = self.subtitle()
        if self._slide_at and time.ticks_diff(self._slide_at, time.ticks_ms()) > 0:
            # A turn is waiting for the presses to stop. The title and the pip say where
            # it is going, and the body stays put as what the movement travels away from.
            #
            # A press abandons the slide it lands in, and the test is on the deadline and
            # not on the flag, so the body is withheld for at most the wait.
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

        The next page comes in from the right and the previous one from the left, which is
        the direction the reader pressed. `over` moves only the arriving card and leaves the
        page underneath standing; `deck` moves both, the one leaving going the other way.

        Only the body band travels: the header and footer were put in place when the turn
        started, since they belong to the page and not to the movement.

        A blit costs its pixels, so `over` averages half a band a frame and a deck a whole
        one. Nothing is rasterised - the arriving page was drawn once when the turn happened.
        """
        top, deep = look.BODY_TOP, look.BODY_H
        travel = int(look.W * self.sliding.now)
        if travel >= look.W or self.sliding.done or self.arriving is None:
            if self.arriving is not None:
                # Landed: the finished page is already drawn in the image, so it is put down
                # from there. Drawing the *current* page instead would be a different page
                # whenever a press arrived mid-slide, which read as a turn going nowhere.
                screen.blit(self.arriving.window(rect(0, top, look.W, deep)), vec2(0, top))
            else:
                pages_module.render(page, self.frame, self.history, theme,
                                    self.page_index, len(self.page_list), subtitle)
            self.sliding = None
            self.arriving = None
            self.leaving = None
            # One more frame, so the page is redrawn live - or the next turn takes over.
            self.dirty = True
            return
        if travel <= 0:
            return
        rest = look.W - travel
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

        modify, not save: save replaces the file and would drop the pairing that lives
        in it. Not called per keypress - that is a flash write inside the input handler,
        and the page is not worth one.
        """
        if self.page_index == self._saved_page:
            return
        State.modify(STATE_APP, {"page": self.page_index})
        self._saved_page = self.page_index


def consume_press():
    """Take the press that closed a modal screen out of the current frame's edges.

    A screen returns the moment its button goes down, before the loop reaches
    `badge.update()`. That edge is still standing when `buttons()` runs, so B on the
    hosts menu chose a server and then fired whatever B was bound to on the page.
    """
    badge.poll()


def main():
    global _app
    # Before anything is drawn: a collect on allocation volume, and not only on failure.
    gc.threshold(GC_THRESHOLD)
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
        consume_press()
    app.apply_layout()

    while True:
        pressed_home = app.home()
        if pressed_home == "exit":
            app.save_page()
            return
        if pressed_home == "menu":
            outcome = pairing_ui().hosts_menu(app)
            consume_press()
            if outcome == "exit":
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
        # After the frame is composited, so a sweep lands between frames and never inside one.
        app.sweep()


_app = None


def on_exit():
    """Called by the launcher when HOME quits the app, and on a normal return."""
    if _app is not None:
        _app.save_page()


main()
