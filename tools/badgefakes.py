"""Stand-ins for the badge's builtins, so the app's pure-logic modules import on a host.

The firmware injects `color`, `shape`, `vec2` and the rest into builtins. `look` builds
a Theme at import, so nothing under `badge_app/` can be imported without them. These cover
the surfaces that are arithmetic and layout; what rasterises is left alone. Faking a
rasteriser proves nothing about the one on the badge, so anything that draws is tested
against the real firmware under the WASM port instead - see DEVELOPMENT.md.

`install()` before the first `import look`, and note what is left out in NOT_FAKED.
`tests/test_badgefakes.py` holds this to the builtins list in `ci/ruff.toml`, so a firmware
name arriving without a decision here fails there and not as a NameError later.
"""

import builtins
import time

# Declared in ci/ruff.toml but not provided here, and why. Everything that draws belongs to
# the badge test suite; the rest is used only inside `badge_app/app.py`, which cannot be
# imported on a host at all.
NOT_FAKED = {
    "screen": "draws",
    "image": "draws",
    "badge": "the app's own module scope calls badge.mode() at import",
    "display": "draws",
    "font": "loads a font off the badge's filesystem",
    "mat3": "draws",
    "pen": "draws",
    "algorithm": "draws",
    "pixel_font": "draws",
    "vector_font": "draws",
    "tween": "runs against the badge's clock",
    "micropython": "the runtime itself",
    "const": "the runtime itself",
    "ptr8": "the runtime itself",
    "ptr32": "the runtime itself",
    "launch": "the launcher's",
    "run": "the launcher's",
    "loop": "the launcher's",
    "reset": "the launcher's",
    "fatal_error": "the launcher's",
    "wait_for_button_or_alarm": "the launcher's",
    "clamp": "unused by this app",
    "rnd": "unused by this app",
    "frnd": "unused by this app",
    "text": "unused by this app",
    "add_glyph": "unused by this app",
    "add_sprite": "unused by this app",
    "file_exists": "unused by this app",
    "is_dir": "unused by this app",
    "rtc": "unused by this app",
    "State": "unused by this app",
    "CENTER": "unused by this app",
    "RIGHT": "unused by this app",
    "MIDDLE": "unused by this app",
    "BOTTOM": "unused by this app",
    "CLIP": "unused by this app",
    "LORES": "used only in badge_app/app.py",
    "HIRES": "used only in badge_app/app.py",
    "VSYNC": "used only in badge_app/app.py",
    "FAST_UPDATE": "used only in badge_app/app.py",
    "FULL_UPDATE": "used only in badge_app/app.py",
    "MEDIUM_UPDATE": "used only in badge_app/app.py",
    "NON_BLOCKING": "used only in badge_app/app.py",
    "DITHER": "used only in badge_app/app.py",
    "BUTTON_A": "an object with .irq(), used in app.py and setup.py",
    "BUTTON_B": "an object with .irq(), used in app.py and setup.py",
    "BUTTON_C": "an object with .irq(), used in app.py and setup.py",
    "BUTTON_UP": "an object with .irq(), used in app.py and setup.py",
    "BUTTON_DOWN": "an object with .irq(), used in app.py and setup.py",
    "BUTTON_HOME": "an object with .irq(), used in app.py and setup.py",
}


class Colour:
    """Enough of the badge's `color` for the app modules to be imported here.

    A theme holds `color` objects, so `look` cannot be imported without one. Only what the
    app calls of it, and only far enough to be compared: what the firmware does is measured
    on the badge, not here.
    """

    def __init__(self, r, g, b, a=255):
        self.r, self.g, self.b, self.a = int(r) & 255, int(g) & 255, int(b) & 255, a

    @classmethod
    def rgb(cls, r, g, b, a=255):
        return cls(r, g, b, a)

    def mix(self, other, t):
        part = t / 255.0
        return Colour(*(a + (b - a) * part
                        for a, b in ((self.r, other.r), (self.g, other.g),
                                     (self.b, other.b), (self.a, other.a))))

    def with_alpha(self, a):
        return Colour(self.r, self.g, self.b, a)

    def lighten(self, n):
        clamp = lambda v: max(0, min(255, v))  # noqa: E731
        return Colour(clamp(self.r + n), clamp(self.g + n), clamp(self.b + n), self.a)

    def darken(self, n):
        return self.lighten(-n)

    def to_oklch(self):
        # The app converts a palette's stops so the ramp interpolates perceptually. Only
        # a colour coming back matters here, so this stands in for the transform, which is
        # measured on the badge.
        return self

    def to_rgb(self):
        return self

    @staticmethod
    def ramp(stops, count):
        """`color.ramp`: count colours sampled across the stops, endpoints included."""
        out = []
        for step in range(count):
            fraction = step / (count - 1.0) if count > 1 else 0.0
            if fraction <= stops[0][0]:
                out.append(stops[0][1])
                continue
            for index in range(1, len(stops)):
                position, colour = stops[index]
                if fraction <= position:
                    previous, before = stops[index - 1]
                    span = position - previous
                    t = 0.0 if span <= 0 else (fraction - previous) / span
                    out.append(before.mix(colour, int(t * 255 + 0.5)))
                    break
            else:
                out.append(stops[-1][1])
        return out

    def over(self, background):
        return self.with_alpha(255).mix(background, 255 - self.a)

    def difference(self, other):
        """Near enough to order two candidates: sRGB distance scaled so black to white is 100.

        The firmware's is perceptual, and what it reports for a given pair is measured on the
        badge. This only has to put a clear difference above a threshold and a near match
        below it.
        """
        gap = sum((a - b) ** 2 for a, b in ((self.r, other.r), (self.g, other.g),
                                            (self.b, other.b))) ** 0.5
        return 100.0 * gap / (3 * 255 ** 2) ** 0.5

    def __eq__(self, other):
        return isinstance(other, Colour) and self.parts() == other.parts()

    def __hash__(self):
        return hash(self.parts())

    def parts(self):
        return (self.r, self.g, self.b, self.a)

    def __repr__(self):
        return "color.rgb({}, {}, {}, {})".format(*self.parts())


class Outline:
    """Stands in for a shape, so the functions that build one can be called here. What it
    rasterises to is the badge's business; this only has to be handed around."""

    def __init__(self, points):
        self.points = list(points)

    def stroke(self, _weight, _flags=0, _miter_limit=4.0):
        return self


class Shape:
    """The stroke flags draw.py names at import, and enough of the rest to build a shape.
    Mirrors picovector's `stroke_flags_t`, though the values are free to change."""

    PATH_OPEN = 1 << 2
    ALIGN_CENTER = 2
    JOIN_MITER = 0
    CAP_BUTT = 0

    @staticmethod
    def custom(*contours):
        return Outline(contours[0] if contours else ())


class Brush:
    """`brush.gradient` far enough to see what a gauge was built from. What the firmware makes
    of the stops is measured on the badge; here they only have to be readable back."""

    CONICAL = "conical"
    LINEAR = "linear"
    RADIAL = "radial"

    def __init__(self, kind, points, stops):
        self.kind, self.points, self.stops = kind, points, list(stops)

    @classmethod
    def gradient(cls, kind, x1, y1, x2, y2, stops):
        return cls(kind, (x1, y1, x2, y2), stops)

    @staticmethod
    def erase():
        return "erase"


class Vec2:
    """A point, far enough to be built and read back. The rasterising is the badge's."""

    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)

    def __repr__(self):
        return f"vec2({self.x}, {self.y})"


class Rect:
    """`rect` is the firmware's; a crop only needs somewhere to put four numbers."""

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    def __repr__(self):
        return f"rect({self.x}, {self.y}, {self.w}, {self.h})"


# The names installed, and what stands in for each.
FAKES = {"color": Colour, "shape": Shape, "brush": Brush, "vec2": Vec2, "rect": Rect,
         "LEFT": 0, "TOP": 0, "ELLIPSES": 2}


def install():
    """Put the stand-ins where the badge finds them, before anything imports look or draw."""
    for name, fake in FAKES.items():
        setattr(builtins, name, fake)
    _install_ticks()


def _install_ticks():
    """MicroPython's tick helpers, which the app uses for every interval it measures.

    ticks_ms wraps on the badge and ticks_diff covers that; here the clocks are handed in by
    the tests, so subtraction covers it.
    """
    if hasattr(time, "ticks_diff"):
        return
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_us = lambda: int(time.monotonic() * 1000000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b
    time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)
