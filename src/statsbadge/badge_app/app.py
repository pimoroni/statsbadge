"""Stats: a host PC's vitals on the badge, paged with UP and DOWN.

UP/DOWN     page through what the host is configured to show
A B C       whatever the host has bound them to, if anything
HOME        open the hosts menu; hold to leave

Where a screen takes A/B/C for itself, they are used in the order they sit in:
A back, B select, C next.
"""

import builtins
import gc
import os
import sys
import time

EXT_DIR = "ext"

import look  # noqa: E402

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None

import splash  # noqa: E402

splash.show()

import draw  # noqa: E402
import net  # noqa: E402
import pages as pages_module  # noqa: E402
import secrets  # noqa: E402
import wifi  # noqa: E402


def pairing_ui():
    """Import the pairing screens on demand."""
    import setup
    return setup


def load_extensions(app_dir):
    """Import the badge-side modules each extension pushed into its own EXT_DIR directory."""
    directory = f"{app_dir}/{EXT_DIR}"
    try:
        extensions = sorted(os.listdir(directory))
    except OSError:
        return []
    loaded = []
    for extension in extensions:
        where = f"{directory}/{extension}"
        try:
            names = sorted(os.listdir(where))
        except OSError:
            continue
        # Behind the app's own directory, so no extension module can stand in for one.
        if where not in sys.path:
            sys.path.append(where)
        for name in names:
            if not name.endswith(".py") or name.startswith("_"):
                continue
            try:
                __import__(name[:-3])
                loaded.append(name[:-3])
            except Exception as exc:  # noqa: BLE001
                print(f"extension {extension}/{name} failed: {exc}")
    return loaded

# HOME opens the hosts menu, so the launcher's exit irq is dropped and HOME is polled.
BUTTON_HOME.irq(None)
HOLD_TO_EXIT_MS = 700

BINDABLE = (("a", BUTTON_A), ("b", BUTTON_B), ("c", BUTTON_C))

GC_THRESHOLD = 256 * 1024
COLLECT_EVERY_MS = 1000

# Series one poll asks for, starting at the page on screen.
GRAPH_KEYS = 12

# A revision nothing has, so the first poll asks for everything.
NO_REV = -1
HUNT_AFTER = 3
SETUP_AFTER = 1

# The panel takes a byte, so anything finer only restarts the ramp.
BACKLIGHT_STEP = 1.0 / 255

CASELIGHT_FLOOR = 0.15


# Stepped towards, not followed directly, or the panel flickers at every passing hand.
LIGHT_FOLLOW = 0.1
LIGHT_EVERY_MS = 100

LIGHT_READS = 16

LOCAL_PREFIX = "badge."
BRIGHTNESS_STEPS = (1.0, 0.6, 0.3)

COMMAND_QUEUE = 4
COMMAND_WAIT_MS = 3000


SLIDE_MS = 220
# How long a press waits for another before the slide starts; see slide_due.
SLIDE_WAIT_MS = 120

BACKLIGHT_MS = 300
# Two values, so a new target is compared against the ramp's end, not its moving value.
_backlight_at = 1.0
_backlight_want = 1.0
_backlight_to = None


def backlight(fraction):
    """Set the display brightness, as a clamped 0-1 fraction of what the panel does."""
    display.backlight(max(0.0, min(1.0, fraction)))


def backlight_to(fraction, ms=BACKLIGHT_MS, shape=None):
    """Head for a brightness, easing there over `ms`. Zero sets it outright.

    `shape` picks how the ramp is walked; a follower retargeting mid-ramp takes LINEAR.
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
    """Move the panel along its ramp, if it is on one."""
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
        # The host sends these only when its slow_rev is past the one we send it.
        self.slow = {}
        self.slow_rev = NO_REV
        self.history = {}
        self.page_index = 0
        self.status = "starting"
        self.detail = None
        self._lit = False
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

        self._next_poll = 0
        self._pending = None
        self._history_due = 0
        self._queued = None
        # (tick, command) oldest first.
        self._commands = []
        # The newest point's age when the host answered, and when that answer landed.
        self._series_age = 0
        self._series_at = 0
        self._last_ok = 0
        self._was_stale = False
        self._next_hunt = 0
        self._listener = None
        self._listen_until = 0
        self.rejected = False

        self.page_index = self.config.page
        self._saved_page = self.page_index

    def setting(self, key, fallback=None):
        """Return a host layout setting, or `fallback` before a layout has landed."""
        layout = self.layout
        return fallback if layout is None else layout.get(key, fallback)

    @property
    def page_list(self):
        return self.setting("pages") or []

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
        style = self.setting("slide") or "off"
        if style != "off" and len(pages) > 1:
            self.sliding = None
            self._slide_at = time.ticks_add(time.ticks_ms(), SLIDE_WAIT_MS)
            self._slide_from = delta < 0
        self.dirty = True

    def slide_due(self, now):
        """Start a waiting page turn once the presses have stopped."""
        if not self._slide_at or self.sliding is not None:
            return
        if time.ticks_diff(now, self._slide_at) < 0:
            return
        self._slide_at = 0
        style = self.setting("slide") or "off"
        if style == "off":
            self.dirty = True
            return
        self.start_slide(style, self._slide_from)

    def start_slide(self, style, back):
        """Set a page turn moving: the page arriving drawn once, the one leaving kept."""
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

        `screen` is a builtin, so it is rebound rather than passed, and rebound from
        whatever `screen` is now: `badge.mode` replaces it, so a copy taken at import
        time is the 160x120 screen the app started with.
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

    def poll(self):
        """Advance whatever request is in flight, or start the next one due."""
        if not self.config.paired:
            return

        self.listen()

        if self._pending is not None:
            if not self.client.step():
                return
            self._finish(self._pending)
            self._pending = None
            return

        # Commands before polls: a press is user-facing and a poll can wait a frame.
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

        interval = self.setting("interval_ms", 1000)
        if self.client.failures:
            interval = min(15000, interval * (1 << min(self.client.failures, 4)))
        self._next_poll = time.ticks_add(now, interval)

        if self.client.failures >= HUNT_AFTER:
            self.hunt()

        if self.layout is None or self.layout_rev != (
                self.frame.get("layout_rev", self.layout_rev)):
            self._start("layout", "/v1/layout")
            return

        # Queued, not sent: the stats go first, or the badge misses a sample.
        if self._graph_keys():
            keys = ",".join(self._graph_keys())
            points = self.setting("graph_points", 48)
            self._queued = ("history",
                            f"/v1/history?keys={keys}&points={points}&v=3")
        # Always sent: the parameter marks this app as able to read a split frame.
        self._start("stats", f"/v1/stats?have={self.slow_rev}")

    def forget_host(self):
        """Drop everything belonging to the host we were talking to."""
        self.stop_listening()
        self.client.reset()
        self._pending = None
        self._next_poll = time.ticks_ms()
        self.frame = {}
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
        """Look for a paired host on the network after the current one went quiet."""
        now = time.ticks_ms()
        if self._listener is not None or time.ticks_diff(now, self._next_hunt) < 0:
            return
        self._next_hunt = time.ticks_add(now, 20000)
        self._listener = net.Listener()
        self._listen_until = time.ticks_add(now, net.DISCOVER_MS)

    def listen(self):
        """Act on any beacon heard since the last frame, while a hunt is on."""
        if self._listener is None:
            return
        for beacon in self._listener.step():
            if self._follow(beacon):
                self.stop_listening()
                return
        if time.ticks_diff(time.ticks_ms(), self._listen_until) >= 0:
            self.stop_listening()

    def stop_listening(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _follow(self, beacon):
        """Take up a beacon from a host this badge knows. True once the hunt is over."""
        server_id = beacon.get("id")
        if not server_id:
            return False
        if server_id == self.config.active:
            if self.config.note_address(server_id, beacon["host"], beacon["port"],
                                        beacon.get("name")):
                self.client.close()
                self.note(f"moved to {beacon['host']}")
                self.dirty = True
            return True
        if server_id in self.config.hosts:
            self.config.note_address(server_id, beacon["host"], beacon["port"],
                                     beacon.get("name"))
            if self.config.switch(server_id):
                self.forget_host()
                self.note(self.config.name or "switched host")
                self.dirty = True
            return True
        # An unpaired host we can see but cannot talk to.
        return self.config.adopt_id(server_id, beacon.get("name"))

    def take_slow(self, frame):
        """Keep the slow half of a frame, and graft it onto every frame after it."""
        rev = frame.get("slow_rev")
        if rev is None:
            # A host too old to split the frame sends every group inline.
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
            # 403 is the host refusing this badge; it has to be paired again.
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
            # v=2 wraps the series in how far apart the points are and how old the
            # newest was when the host answered. v=3 adds a pair for any ring a source
            # answers for itself, on whatever clock its readings are really on.
            #
            # Merged, not replaced: the ask is capped and starts at the page on screen,
            # so a ring fetched a poll ago is still the newest there is.
            self.history.update(payload.get("series", payload) or {})
            plotted = self._plot_refs()
            for ref in [ref for ref in self.history if ref not in plotted]:
                del self.history[ref]
            self._series_age = int(payload.get("age_ms", 0) or 0)
            self._series_at = time.ticks_ms()
            pages_module.note_spacing(payload.get("every_ms", 1000),
                                      self.setting("interval_ms", 1000))
            pages_module.note_series_spacing(payload.get("spacing"))
        self.dirty = True

    def _plot_refs(self, first=0):
        """Return every field a page draws a series of, walking the pages from `first`."""
        keys = []
        total = len(self.page_list)
        for step in range(total):
            page = self.page_list[(first + step) % total]
            if page.get("kind") not in pages_module.PLOTS:
                continue
            for ref in page.get("fields", []) + [page.get("field")]:
                if ref and ref not in keys:
                    keys.append(ref)
        return keys

    def _graph_keys(self):
        """Return the refs this poll asks for, nearest page first."""
        return self._plot_refs(self.page_index)[:GRAPH_KEYS]

    def apply_layout(self):
        theme_name = self.setting("theme", look.DEFAULT)
        theme = (look.from_palette(theme_name, self.setting("palette"))
                 or look.get(theme_name))
        if theme.key != self.theme.key:
            self.theme = theme
            draw.clear_cache()
        # The first layout to land is the badge coming up, so it takes its brightness.
        self.apply_backlight(BACKLIGHT_MS if self._lit else 0)
        self._lit = True
        draw.SMOOTH = bool(self.setting("smooth", True))
        draw.ROWS = self.setting("rows", "zebra")
        draw.GAUGE_FILL = self.setting("gauge_fill", "solid")
        pages_module.PLOT_ANIMATION = bool(
            self.setting("plot_animation", False))
        # Replaced, not updated: a group dropped from every page should stop being named.
        pages_module.LABELS = self.setting("labels") or {}
        draw.use_units(self.setting("units"))
        pages_module.use_facts(self.setting("scales"), self.setting("percent"))
        pages_module.forget_layout()
        animate = bool(self.setting("animate", False))
        if animate != pages_module.ANIMATE:
            pages_module.ANIMATE = animate
            pages_module.sweep_reset()
        self.apply_caselights()
        if self.page_index >= len(self.page_list):
            self.page_index = 0

    def wanted_brightness(self):
        """Return the brightness the panel should be showing, 0-1."""
        wanted = self.dimmed
        if wanted is None:
            wanted = float(self.setting("brightness", 0.8))
        if self.setting("auto_brightness") and self.ambient is not None:
            wanted *= look.LIGHT_FLOOR + (1.0 - look.LIGHT_FLOOR) * self.ambient
        return wanted

    def apply_backlight(self, ms=BACKLIGHT_MS, shape=None):
        """Head for that brightness, and take the case lights with it."""
        backlight_to(self.wanted_brightness(), ms, shape)
        self.apply_caselights()

    def read_light(self):
        """Follow the room's light slowly, returning True when the panel needs setting."""
        if not self.setting("auto_brightness"):
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
        """Return the case light level: off, the backlight's, or one following a reading."""
        wanted = self.setting("caselights", True)
        if not wanted:
            badge.caselights(0.0)
            return
        level = self.wanted_brightness()
        if isinstance(wanted, str):
            fraction = pages_module.fraction_of(
                wanted, pages_module.value_of(self.frame, wanted)) or 0.0
            level *= CASELIGHT_FLOOR + (1.0 - CASELIGHT_FLOOR) * fraction
        badge.caselights(level)

    def buttons(self):
        touched = False
        if badge.pressed(BUTTON_UP):
            self.turn(-1)
            touched = True
        if badge.pressed(BUTTON_DOWN):
            self.turn(1)
            touched = True
        for name, button in BINDABLE:
            if badge.pressed(button):
                if name == "c" and self.current_page() is None:
                    self.retry()
                else:
                    self.press(name)
                touched = True
        if touched:
            # Only a press counts: the page turns this class makes for itself must not.
            self._pressed_at = time.ticks_ms()
            self._advanced_at = 0

    def press(self, which):
        """Return what a button is bound to: something this badge does, or a host command."""
        binding = (self.setting("buttons") or {}).get(which)
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
        """Step the panel down and round again, over the configured level."""
        self.dim_step = (self.dim_step + 1) % len(BRIGHTNESS_STEPS)
        share = BRIGHTNESS_STEPS[self.dim_step]
        base = float(self.setting("brightness", 0.8))
        self.dimmed = None if share >= 1.0 else base * share
        self.apply_backlight()
        self.note(f"brightness {round(share * 100)}%")

    def send_command(self, command):
        """Hold a press for the host, to go out as soon as the connection is free."""
        if not command:
            return
        if len(self._commands) >= COMMAND_QUEUE:
            self.note("busy")
            return
        self._commands.append((command, time.ticks_ms()))
        self.note(command.replace("_", " "))

    def needs_setup(self):
        """Return whether to offer the pairing screens."""
        if not self.config.paired or self.rejected:
            return True
        return self.layout is None and self.client.failures >= SETUP_AFTER

    def retry(self):
        """Drop the connection and poll again now, resetting the backoff."""
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
        """Return how solid to draw the toast: 1 while it is read, 0 once it is gone."""
        left = time.ticks_diff(self.toast_until, time.ticks_ms())
        if left >= draw.TOAST_FADE_MS:
            return 1.0
        return max(0.0, left / draw.TOAST_FADE_MS)

    def tick(self):
        """Notice the things that change with time and not with an event."""
        now = time.ticks_ms()
        if self.toast_text:
            left = time.ticks_diff(self.toast_until, now)
            if left <= 0:
                self.toast_text = None
                self.dirty = True
            elif left < draw.TOAST_FADE_MS:
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
                # LINEAR over exactly the gap to the next reading, or the panel pulses.
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
        """Page on by itself once nothing has been pressed for a while."""
        after = int(self.setting("idle_advance_s", 0))
        if not after or len(self.page_list) < 2:
            return
        if time.ticks_diff(now, self._pressed_at) < after * 1000:
            return
        every = max(1, int(self.setting("advance_every_s", 10)))
        if self._advanced_at and time.ticks_diff(now, self._advanced_at) < every * 1000:
            return
        self._advanced_at = now
        self.turn(1)

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
            title = "Connecting" if not self.detail else self.detail
            draw.banner(theme, title,
                        f"{self.config.name or self.config.host}:{self.config.port}",
                        "C retry   B set up   HOME hosts")
            return

        subtitle = self.subtitle()
        if self._slide_at and time.ticks_diff(self._slide_at, time.ticks_ms()) > 0:
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
        """Draw a page turn part way through, as two cards placed out of an image each.

        `over` moves the arriving card alone; `deck` moves both.
        """
        top, deep = look.BODY_TOP, look.BODY_H
        travel = int(look.W * self.sliding.now)
        if travel >= look.W or self.sliding.done or self.arriving is None:
            if self.arriving is not None:
                # From the image, not a fresh render: a press mid-slide changes the page.
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

    def home(self):
        """Return what HOME did this frame: None, "menu" or "exit"."""
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
        """Collect between frames, at a moment when nothing is waiting on the result."""
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
        """Persist the page index, if it moved."""
        if self.page_index == self._saved_page:
            return
        self.config.page = self.page_index
        self.config.save()
        self._saved_page = self.page_index


def no_network(theme):
    """Draw the no-network screen."""
    draw.banner(theme, "No WiFi", "no network set",
                'statsbadge install --ssid "..."')
    draw.blit_label("HOME quit", look.SIZE_SMALL, theme.dim,
                    look.W // 2, look.H - 18, align=1)
    badge.update()
    while not badge.pressed(BUTTON_HOME):
        badge.update()


def main(app_dir):
    global _app
    look.APP_DIR = app_dir
    gc.threshold(GC_THRESHOLD)
    draw.prepare()
    load_extensions(app_dir)
    app = App()
    _app = app

    # wifi.connect() raises a fatal_error when no SSID is configured. Intercepted to
    # recommend `statsbadge install --ssid "..."`.
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
            # B chose a server, and would otherwise also fire B's binding here.
            badge.poll()
            if outcome == "exit":
                app.save_page()
                return
            app.apply_layout()
            app.dirty = True

        app.buttons()
        # B reaches setup whenever the connection is unusable, not only when unpaired.
        if app.needs_setup() and badge.pressed(BUTTON_B):
            app.stop_listening()
            if not pairing_ui().run(app):
                return
            app.forget_host()
            app.apply_layout()
        app.poll()
        app.tick()
        backlight_step()

        # `badge.default_clear = None` leaves the framebuffer standing between polls.
        if app.dirty:
            app.render()
            app.dirty = False
        badge.update()
        app.sweep()

_app = None


def on_exit():
    """Run on the way out, when HOME quits the app or it returns normally."""
    if _app is not None:
        _app.stop_listening()
        _app.save_page()
