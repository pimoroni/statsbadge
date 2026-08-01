"""Themes and the 320x240 layout.

Everything drawn is a vector shape taking its colours from here, so a theme is a
table of colours and one gradient rule rather than a set of pictures. That is the
whole reason the pages are shapes and not sprites: swapping `THEME` restyles the
badge with no assets to rebuild.
"""

W = 320
H = 240

# A page owns the middle band. The header names it, the footer says where you are.
HEADER_H = 30
FOOTER_H = 20
BODY_TOP = HEADER_H
BODY_H = H - HEADER_H - FOOTER_H
BODY_MID = BODY_TOP + BODY_H // 2

PAD = 10

# Dial geometry, in the body band, and with it the whole of the left half of any page that
# splits into a gauge and a column: the single dial, the ring stack and an extension's clock
# face all draw here. The gauge, the gap to the column and the right margin are all DIAL_GAP,
# so the positions are worked out from it rather than picked one at a time, and the radius is
# as large as that leaves room for - a clock face is a picture of an object and wants the
# space, and a gauge that filled less of it made the layout jump between pages.
DIAL_GAP = 16
DIAL_OUTER = 82
DIAL_INNER = 62
# Nudged down, because a gauge with its gap at the bottom carries its weight high and reads
# as sitting above centre when it is on it.
DIAL_C = (DIAL_GAP + DIAL_OUTER, BODY_TOP + BODY_H // 2 + 2)
# A 270 degree sweep with the gap centred on the bottom, so it looks like a gauge.
# Angles start at the top and run clockwise: 225 is lower-left, and 495 is 135 once
# round, which is lower-right.
DIAL_FROM = 225.0
DIAL_TO = 495.0

# Where the readouts stack, to the right of whatever gauge the page draws.
READOUT_X = DIAL_C[0] + DIAL_OUTER + DIAL_GAP
READOUT_W = W - READOUT_X - DIAL_GAP
# Tall enough for the name, the reading and its bar, with a gap to the next name.
READOUT_H = 38
# A row that has to state its own full scale puts that where the bar would have gone, and
# needs the height back: at the plain pitch the note and the next row's name touch.
READOUT_NOTE_H = 46


def readout_rows(count, height=READOUT_H):
    """Where each of `count` readout rows starts.

    Level with the top of the dial, which is what makes the gauge and the column read as
    one block, and lifted only if that many rows would otherwise run past the band. Every
    page that draws a gauge and a column uses this, so nothing moves when you page between
    them.
    """
    room = BODY_TOP + BODY_H - 6 - count * height
    top = max(BODY_TOP + 6, min(DIAL_C[1] - DIAL_OUTER, room))
    return [top + index * height for index in range(count)]


# The app carries its own text font rather than borrowing one off the badge: what is in
# /system/assets belongs to the firmware and can change under us, and a display this small
# lives or dies on its type. Built from Lexend by tools/make_text_font.py.
FONT_FILE = "fonts/lexend-regular.af"
FONT_NAME = "lexend"

# Only if the app's own font did not arrive - an install that predates it, or a partial
# copy. Text is the one thing the app cannot draw a page without, so it borrows rather than
# gives up.
FALLBACK_FONT_PATH = "/system/assets/fonts/MonaSans-Medium.af"

# The app's Material Symbols, built from ci/badge-icons.txt by tools/make_icon_font.py.
# A name rather than a path: an install puts it in the app directory, and where that is
# depends on how the app was started, so draw.add_font looks for it.
ICON_FILE = "icons.af"
APP_DIR = "/system/apps/stats"

# Sizes are point sizes for the .af font: the size is what the font's em is scaled to, so a
# capital stands draw.CAP of it, and text(x, y) puts the baseline at y + size.
SIZE_TITLE = 19
SIZE_HUGE = 44
SIZE_BIG = 26
SIZE_LABEL = 12
SIZE_VALUE = 17
SIZE_SMALL = 11

# Several gauges in the body band, keyed by how many there are: where their centres go,
# the ring radii, and the type sizes that fit inside one. Measured on the board rather
# than derived - four gauges cost the same frame as one, because an arc is charged for by
# its area, so the radii are as large as the band allows and not as small as it takes to
# be quick.
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


class Theme:
    """A palette plus the two decisions that make it look like one thing.

    `ramp` is what a gauge fills with as it climbs, so a theme decides whether 90%
    CPU is alarming or just bright. `track` is the unfilled part of any gauge.
    """

    def __init__(self, name, bg, panel, ink, dim, accent, ramp, grid=None,
                 case=0.1):
        self.name = name
        self.bg = bg
        self.panel = panel
        self.ink = ink
        self.dim = dim
        self.accent = accent
        self.ramp = ramp          # ((position 0-1, (r, g, b)), ...) cold to hot
        self.grid = grid or dim
        # The four case lights are single-channel PWM, not RGB: one brightness
        # fraction each. badge.caselights takes one value for all four or four values.
        self.case = case

    def at(self, fraction):
        """The ramp colour for a 0-1 value, interpolated."""
        stops = self.ramp
        if fraction <= stops[0][0]:
            return stops[0][1]
        for i in range(1, len(stops)):
            pos, rgb = stops[i]
            if fraction <= pos:
                prev_pos, prev_rgb = stops[i - 1]
                span = pos - prev_pos
                t = 0.0 if span <= 0 else (fraction - prev_pos) / span
                return (
                    int(prev_rgb[0] + (rgb[0] - prev_rgb[0]) * t),
                    int(prev_rgb[1] + (rgb[1] - prev_rgb[1]) * t),
                    int(prev_rgb[2] + (rgb[2] - prev_rgb[2]) * t),
                )
        return stops[-1][1]


# What the app draws with before its first layout lands, and if one ever arrives without a
# palette. Every other theme is data on the host, in statsbadge/themes.py, and travels here
# in the layout - so a palette can be changed or invented with nothing installed.
THEMES = {
    "dark": Theme(
        "dark",
        bg=(18, 20, 28), panel=(26, 30, 43), ink=(242, 245, 255), dim=(139, 147, 171),
        accent=(56, 232, 209),
        ramp=((0.0, (56, 232, 209)), (0.45, (126, 211, 117)),
              (0.72, (236, 159, 7)), (1.0, (215, 25, 8))),
        grid=(44, 51, 70), case=0.22,
    ),
}

DEFAULT = "dark"


def get(name):
    return THEMES.get(name, THEMES[DEFAULT])


def from_palette(name, palette):
    """A theme out of the colours the host sent, or None if they are not usable.

    Anything the badge would go on to hand to `color.rgb` is checked here rather than at
    the point of drawing: a palette arrives over the network, and a bad one would otherwise
    be a crash on every frame instead of a page in the theme it booted with.
    """
    if not isinstance(palette, dict):
        return None
    try:
        colours = {key: tuple(int(v) for v in palette[key][:3])
                   for key in ("bg", "panel", "ink", "dim", "accent")}
        for rgb in colours.values():
            if len(rgb) != 3:
                return None
        grid = palette.get("grid")
        ramp = tuple((float(pos), tuple(int(v) for v in rgb[:3]))
                     for pos, rgb in palette["ramp"])
        if not ramp:
            return None
        return Theme(name, ramp=ramp, case=float(palette.get("case", 0.1)),
                     grid=tuple(int(v) for v in grid[:3]) if grid else None,
                     **colours)
    except (TypeError, ValueError, KeyError, IndexError):
        return None
