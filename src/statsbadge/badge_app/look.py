"""Themes and the 320x240 layout."""

import math

W = 320
H = 240

HEADER_H = 30
FOOTER_H = 20
BODY_TOP = HEADER_H
BODY_H = H - HEADER_H - FOOTER_H
BODY_MID = BODY_TOP + BODY_H // 2

PAD = 10

DIAL_GAP = 16
DIAL_OUTER = 82
DIAL_INNER = 62
DIAL_C = (DIAL_GAP + DIAL_OUTER, BODY_TOP + BODY_H // 2 + 2)
# Angles start at the top and run clockwise, so this is a 270 degree sweep from
# lower-left to lower-right with the gap centred on the bottom.
DIAL_FROM = 225.0
DIAL_TO = 495.0

READOUT_X = DIAL_C[0] + DIAL_OUTER + DIAL_GAP
READOUT_W = W - READOUT_X - DIAL_GAP
READOUT_H = 38
READOUT_NOTE_H = 46


def readout_rows(count, height=READOUT_H):
    """Return the top of each of `count` readout rows."""
    room = BODY_TOP + BODY_H - 6 - count * height
    top = max(BODY_TOP + 6, min(DIAL_C[1] - DIAL_OUTER, room))
    return [top + index * height for index in range(count)]


FONT_FILE = "fonts/lexend-regular.af"
FONT_NAME = "lexend"

# For an install predating the app's own font, or a partial copy.
FALLBACK_FONT_PATH = "/system/assets/fonts/MonaSans-Medium.af"

# A name, not a path: draw.add_font searches, since the app directory depends on
# how the app was started.
ICON_FILE = "icons.af"
APP_DIR = "/system/apps/stats"

# Raw `badge.light_level()` counts. Darkness reads 46-53, a lit room 4500, a phone
# torch 61706.
LIGHT_DIM = 48
LIGHT_BRIGHT = 4000
# The fraction of configured brightness a curtained room gets. Off zero, so a dark
# room does not read as a fault.
LIGHT_FLOOR = 0.1
LIGHT_SPAN = math.log(LIGHT_BRIGHT / LIGHT_DIM)


def ambient_fraction(raw):
    """Map a raw light reading onto 0-1, logarithmically."""
    return max(0.0, min(1.0, math.log(max(raw, LIGHT_DIM) / LIGHT_DIM) / LIGHT_SPAN))


# Point sizes for the .af font. A capital stands draw.CAP of the size, and
# text(x, y) puts the baseline at y + size.
SIZE_TITLE = 19
SIZE_HUGE = 44
SIZE_BIG = 26
SIZE_LABEL = 12
SIZE_VALUE = 17
SIZE_SMALL = 11

# Body-band gauges keyed by how many there are: centres, ring radii, and the type
# sizes that fit inside one.
DIALS = {
    1: {"centres": ((160, 125),), "outer": 74, "inner": 56,
        "value": SIZE_HUGE, "label": SIZE_VALUE},
    2: {"centres": ((85, 125), (235, 125)), "outer": 62, "inner": 46,
        "value": 34, "label": SIZE_LABEL},
    3: {"centres": ((60, 125), (160, 125), (260, 125)), "outer": 46, "inner": 34,
        "value": 26, "label": SIZE_SMALL},
    4: {"centres": ((85, 84), (235, 84), (85, 166), (235, 166)), "outer": 40,
        "inner": 29, "value": 22, "label": SIZE_SMALL},
}


RAMP_STEPS = 65

# Lightness counts, on all three channels.
STRIPE = 10


class Theme:
    """A palette's colours, plus the ramp a gauge fills with as it climbs."""

    def __init__(self, name, bg, panel, ink, dim, accent, ramp, grid=None,
                 accent_b=None, image=None, pale=False, series=None, series_alpha=None):
        self.name = name
        self.bg = color.rgb(*bg)
        self.panel = color.rgb(*panel)
        self.ink = color.rgb(*ink)
        self.dim = color.rgb(*dim)
        self.accent = color.rgb(*accent)
        self.accent_b = color.rgb(*accent_b) if accent_b else self.accent
        # Stops in OKLCH, so the table interpolates there. sRGB turns the
        # green-to-amber leg olive, 39 counts adrift at 0.64 of the ramp.
        self.ramp = tuple((pos, color.rgb(*rgb).to_oklch()) for pos, rgb in ramp)
        self.grid = color.rgb(*grid) if grid else self.dim
        self.key = (name, tuple(bg), tuple(accent),
                    tuple(accent_b) if accent_b else tuple(accent),
                    tuple(ramp[0][1]), tuple(ramp[-1][1]))
        self.pale = pale
        self.series = (tuple(color.rgb(*rgb) for rgb in series) if series
                       else (self.accent, self.accent_b))
        self.series_alpha = tuple(series_alpha) if series_alpha else (255, 255)
        self.stripe = self.bg.darken(STRIPE) if self.pale else self.bg.lighten(STRIPE)
        self.steps = tuple(color.ramp(self.ramp, RAMP_STEPS))
        # Keyed by shade count, to assign into an indexed image's table in one write.
        self.image = {count: tuple(color.rgb(*rgb) for rgb in greys)
                      for count, greys in (image or {}).items()}

    def at(self, fraction):
        """Return the ramp colour for a 0-1 value."""
        if fraction <= 0.0:
            return self.steps[0]
        if fraction >= 1.0:
            return self.steps[-1]
        return self.steps[int(fraction * (RAMP_STEPS - 1) + 0.5)]


# themes.toml's default, copied out because MicroPython cannot read it. A check
# holds the two the same.
THEMES = {
    "dark": Theme(
        "dark",
        bg=(18, 20, 28), panel=(26, 30, 43), ink=(242, 245, 255), dim=(139, 147, 171),
        accent=(56, 232, 209),
        ramp=((0.0, (56, 232, 209)), (0.45, (126, 211, 117)),
              (0.72, (236, 159, 7)), (1.0, (215, 25, 8))),
        grid=(44, 51, 70),
    ),
}

DEFAULT = "dark"


def get(name):
    return THEMES.get(name, THEMES[DEFAULT])


def from_palette(name, palette):
    """Build a theme from the colours the host sent, or None if unusable."""
    if not isinstance(palette, dict):
        return None
    try:
        colours = {key: tuple(int(v) for v in palette[key][:3])
                   for key in ("bg", "panel", "ink", "dim", "accent")}
        for rgb in colours.values():
            if len(rgb) != 3:
                return None
        grid = palette.get("grid")
        second = palette.get("accent_b")
        ramp = tuple((float(pos), tuple(int(v) for v in rgb[:3]))
                     for pos, rgb in palette["ramp"])
        if not ramp:
            return None
        image = {len(greys): [tuple(int(v) for v in rgb[:3]) for rgb in greys]
                 for greys in (palette.get("image") or {}).values()}
        return Theme(name, ramp=ramp,
                     grid=tuple(int(v) for v in grid[:3]) if grid else None,
                     accent_b=tuple(int(v) for v in second[:3]) if second else None,
                     image=image, pale=bool(palette.get("pale")),
                     series=[tuple(int(v) for v in rgb[:3])
                             for rgb in palette.get("series") or ()],
                     series_alpha=palette.get("series_alpha"), **colours)
    except (TypeError, ValueError, KeyError, IndexError):
        return None
