"""Themes and the 320x240 layout.

Everything drawn is a vector shape taking its colours from here, which makes a theme a
table of colours and one gradient rule. Pages are shapes and not sprites for that reason:
swapping `THEME` restyles the badge with no assets to rebuild.

Measured on the board, since several of the settings below are otherwise arbitrary:

`badge.light_level()` is a raw u16 off the phototransistor, 16us a read, stepping in
sixteens off a 12-bit conversion. Darkness sits at 46-53, three counts off the bottom of
the ADC. A curtained room reads around 320 and a lit one around 4500. A phone torch reads
61706, so LIGHT_BRIGHT tops the scale well below the maximum; measuring a room against a
torch leaves the room a fraction of the way up.

A `color` built per pen set costs 36.5us against 18.4 for one already made, and a ramp
lookup 54.6 against 11.9, so a Theme holds objects and not tuples.

Ramp stops are interpolated in OKLCH. sRGB drags blue along the green-to-amber leg and
turns it olive, 39 counts adrift at 0.64 of the ramp, where a gauge spends its time.

Four gauges cost the same frame as one, an arc being charged for by its edges, so the
radii in DIALS are as large as the band allows.
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

# Dial geometry in the body band: the left half of any page that splits into a gauge and
# a column. The single dial, the ring stack and a clock face all draw here.
#
# The gauge, the gap and the right margin are all DIAL_GAP. The radius is as large as
# that leaves room for.
DIAL_GAP = 16
DIAL_OUTER = 82
DIAL_INNER = 62
# Nudged down, since a gauge with its gap at the bottom carries its weight high and looks
# as though it sits above centre when it is on it.
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

    Level with the top of the dial, which sets the gauge and the column reading as
    one block, and lifted only if that many rows would otherwise run past the band. Every
    page that draws a gauge and a column uses this, so nothing moves when you page between
    them.
    """
    room = BODY_TOP + BODY_H - 6 - count * height
    top = max(BODY_TOP + 6, min(DIAL_C[1] - DIAL_OUTER, room))
    return [top + index * height for index in range(count)]


# The app carries a text font of its own, and does not borrow one off the badge. What is
# in /system/assets belongs to the firmware and can change underneath. Built from Lexend
# by tools/make_text_font.py.
FONT_FILE = "fonts/lexend-regular.af"
FONT_NAME = "lexend"

# Only if the app's font did not arrive: an install that predates it, or a partial copy.
# Text is the one thing the app cannot draw a page without, so it borrows one.
FALLBACK_FONT_PATH = "/system/assets/fonts/MonaSans-Medium.af"

# The app's Material Symbols, built from ci/badge-icons.txt by tools/make_icon_font.py.
# A name and not a path. An install puts it in the app directory, and where that is
# depends on how the app was started, so draw.add_font searches.
ICON_FILE = "icons.af"
APP_DIR = "/system/apps/stats"

# The ends of the ambient scale, in raw `badge.light_level()` counts. Past BRIGHT the
# panel is already at full and the answer stops changing.
LIGHT_DIM = 48
LIGHT_BRIGHT = 4000
# What ambient light is allowed to take away. A curtained room gets this much of the
# configured brightness and full daylight gets all of it. Held off zero, since a dark room
# at zero looks like a fault.
LIGHT_FLOOR = 0.2


def ambient_fraction(raw):
    """Where a raw light reading sits on 0-1, logarithmically.

    Neither the sensor nor human vision is linear, and most of the adjustment worth
    making lies between a curtained room and an overcast window, a small fraction of the
    way up the sensor's scale.
    """
    import math

    span = math.log(LIGHT_BRIGHT / LIGHT_DIM)
    return max(0.0, min(1.0, math.log(max(raw, LIGHT_DIM) / LIGHT_DIM) / span))


# Sizes are point sizes for the .af font. The size is the font's em scaled, a capital
# stands draw.CAP of it, and text(x, y) puts the baseline at y + size.
SIZE_TITLE = 19
SIZE_HUGE = 44
SIZE_BIG = 26
SIZE_LABEL = 12
SIZE_VALUE = 17
SIZE_SMALL = 11

# Several gauges in the body band, keyed by how many there are. Where their centres go,
# the ring radii, and the type sizes that fit inside one.
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


# Colours a theme's ramp is resolved to when it is built, letting a gauge fill by lookup.
# Sixty-five is a quarter of a percent of 100, and steps of one or two in a channel.
RAMP_STEPS = 65

# A background this bright or brighter counts as a pale page, as the sum of its channels.
PALE_SUM = 384

# How far a banded row sits from the page, in counts of lightness. All three channels move
# together, so a band is the page a step away. The panel colour may differ in hue, which
# stripes a near-black page.
STRIPE = 10


class Theme:
    """A palette plus the two decisions that make it look like one thing.

    `ramp` is the colours a gauge fills with as it climbs, and sets whether 90% CPU
    looks alarming or bright. `track` is the unfilled part of any gauge.

    `accent_b` is a second colour used sparingly, for a page that needs somewhere else to
    go. A graph's second series takes it where a palette names one, and works one out of
    the ramp otherwise.

    `stripe` is worked out from the rest and not named in a palette. It is a step from the
    page, and a palette that had to state it could state it wrong.

    Built from the palette data a layout carries, and held as `color` objects for a pen
    to take.
    """

    def __init__(self, name, bg, panel, ink, dim, accent, ramp, grid=None,
                 case=0.1, accent_b=None, image=None):
        self.name = name
        self.bg = color.rgb(*bg)
        self.panel = color.rgb(*panel)
        self.ink = color.rgb(*ink)
        self.dim = color.rgb(*dim)
        self.accent = color.rgb(*accent)
        # One more colour, used sparingly, a graph's second series being all of it. The
        # accent again where a palette names none.
        self.accent_b = color.rgb(*accent_b) if accent_b else self.accent
        # Stops in OKLCH, so the table interpolates there. The palette arrives as sRGB
        # and is converted here, the round trip being within a count.
        self.ramp = tuple((pos, color.rgb(*rgb).to_oklch()) for pos, rgb in ramp)
        self.grid = color.rgb(*grid) if grid else self.dim
        # The four case lights are single-channel PWM, one brightness fraction each.
        # badge.caselights takes one value for all four or four values.
        self.case = case
        self.pale = sum(bg) >= PALE_SUM
        # A banded row: toward the ink on a dark page and away from it on a pale one,
        # `lighten` having nowhere to go on a background that is already near white.
        self.stripe = self.bg.darken(STRIPE) if self.pale else self.bg.lighten(STRIPE)
        self.steps = tuple(color.ramp(self.ramp, RAMP_STEPS))
        # The greys a picture is drawn in, keyed by how many shades it has. Assigned
        # straight into an indexed image's table, which recolours it in one write.
        #
        # Held apart from `ramp`, which would draw a photograph as a heat map.
        self.image = {count: tuple(color.rgb(*rgb) for rgb in greys)
                      for count, greys in (image or {}).items()}

    def at(self, fraction):
        """The ramp colour for a 0-1 value, off a table built with the theme.

        Interpolating per call is 30us against 12 for a lookup, and a page with sixteen
        bars asks sixteen times a frame. RAMP_STEPS is finer than a gauge fill resolves,
        and finer than most of the ramps have stops.

        `color.ramp` samples the stops in one call: 850us for the whole table, against
        4.9ms to interpolate the same 65 steps here.
        """
        if fraction <= 0.0:
            return self.steps[0]
        if fraction >= 1.0:
            return self.steps[-1]
        return self.steps[int(fraction * (RAMP_STEPS - 1) + 0.5)]



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

    A palette arrives over the network, so it is checked here and the colours are built
    here: a bad one would otherwise be a crash on every frame instead of a page in the
    theme it booted with.
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
        second = palette.get("accent_b")
        ramp = tuple((float(pos), tuple(int(v) for v in rgb[:3]))
                     for pos, rgb in palette["ramp"])
        if not ramp:
            return None
        # Keyed by the number of shades, matching an indexed image's table length. A host
        # too old to send these leaves a theme that draws no pictures, and still builds.
        image = {len(greys): [tuple(int(v) for v in rgb[:3]) for rgb in greys]
                 for greys in (palette.get("image") or {}).values()}
        return Theme(name, ramp=ramp, case=float(palette.get("case", 0.1)),
                     grid=tuple(int(v) for v in grid[:3]) if grid else None,
                     accent_b=tuple(int(v) for v in second[:3]) if second else None,
                     image=image, **colours)
    except (TypeError, ValueError, KeyError, IndexError):
        return None
