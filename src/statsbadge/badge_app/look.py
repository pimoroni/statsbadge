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

# Dial geometry, in the body band.
DIAL_C = (108, BODY_TOP + BODY_H // 2 + 4)
DIAL_OUTER = 74
DIAL_INNER = 56
# A 270 degree sweep with the gap centred on the bottom, so it reads as a gauge and
# not a ring. Angles start at the top and run clockwise: 225 is lower-left, and 495
# is 135 once round, which is lower-right.
DIAL_FROM = 225.0
DIAL_TO = 495.0

# Where a dial's readouts stack, to the right of it.
READOUT_X = 196
READOUT_W = W - READOUT_X - PAD
READOUT_H = 34

FONT_PATH = "/system/assets/fonts/MonaSans-Medium.af"

# Sizes are point sizes for the .af font. A capital stands 0.68 of size above the
# baseline, and text(x, y) puts the baseline at y + size.
SIZE_TITLE = 19
SIZE_HUGE = 44
SIZE_BIG = 26
SIZE_LABEL = 12
SIZE_VALUE = 17
SIZE_SMALL = 11


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


THEMES = {
    # The one this exists to replace: dark, hot at the top. The accent is dots' teal,
    # so the two projects on this badge agree on one colour.
    "dark": Theme(
        "dark",
        bg=(10, 10, 12), panel=(22, 22, 26), ink=(240, 238, 235), dim=(96, 96, 104),
        accent=(56, 232, 209),
        ramp=((0.0, (0, 190, 255)), (0.45, (120, 230, 90)),
              (0.72, (255, 190, 0)), (1.0, (255, 48, 32))),
        grid=(40, 40, 46), case=0.22,
    ),
    "mono": Theme(
        "mono",
        bg=(8, 8, 8), panel=(20, 20, 20), ink=(245, 245, 245), dim=(110, 110, 110),
        accent=(235, 235, 235),
        ramp=((0.0, (110, 110, 110)), (1.0, (255, 255, 255))),
        grid=(38, 38, 38), case=0.14,
    ),
    "amber": Theme(
        "amber",
        bg=(14, 8, 0), panel=(30, 18, 2), ink=(255, 190, 70), dim=(120, 80, 20),
        accent=(255, 176, 0),
        ramp=((0.0, (140, 80, 0)), (0.7, (255, 176, 0)), (1.0, (255, 240, 180))),
        grid=(56, 34, 4), case=0.26,
    ),
    "blueprint": Theme(
        "blueprint",
        bg=(6, 16, 34), panel=(12, 28, 56), ink=(214, 232, 255), dim=(88, 120, 170),
        accent=(90, 180, 255),
        ramp=((0.0, (60, 130, 220)), (0.6, (120, 210, 255)), (1.0, (255, 255, 255))),
        grid=(28, 56, 100), case=0.18,
    ),
    "vapor": Theme(
        "vapor",
        bg=(18, 8, 30), panel=(34, 14, 56), ink=(245, 225, 255), dim=(140, 100, 180),
        accent=(255, 90, 200),
        ramp=((0.0, (90, 220, 255)), (0.5, (190, 130, 255)), (1.0, (255, 80, 190))),
        grid=(56, 26, 90), case=0.24,
    ),
}

DEFAULT = "dark"


def get(name):
    return THEMES.get(name, THEMES[DEFAULT])
