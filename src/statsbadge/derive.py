"""A whole palette out of one accent colour.

The badge's own colour arithmetic is in firmware, but a palette has to be built here: the
config UI shows it before anybody commits to it, and what travels to the badge is a palette
like any other, so nothing on the badge knows a theme was derived rather than written down.

Everything below works in OKLCH, where lightness means what the eye means by it - which is
what lets "ink has to be readable on bg" be arithmetic instead of a judgement. The conversion
mirrors the firmware's own; checked against it colour by colour, the round trip agrees to
within a count. Bytes in every axis for the same reason the firmware uses them: l spans 0-1
lightness, c spans 0-0.35 chroma, h is 256 counts to a turn.
"""

import math

# The accents on offer: twelve hues evenly round the wheel, in four families. A taste call
# rather than a derivation - these are the twelve a wheel gives, not the twelve a designer would
# pick, and the list is the thing to argue with.
ACCENT_HUES = tuple(range(0, 360, 30))
# The four families of accent, as (lightness, how much of the hue's own chroma limit to take).
# Twelve hues each, so forty-eight colours: the family says how loud the accent is and the hue
# is the choice. `saturated` is where the single-hue themes sat - every one of them within 0.003
# of its hue's limit - and `normal` is a little short of it, which is where one fixed chroma for
# all twelve used to land. How much chroma a hue can hold varies enormously (0.128 at cyan
# against 0.287 at magenta on a dark page), so a fraction of the limit keeps a family looking
# like one family where a fixed number does not.
ACCENT_FAMILIES = {
    "pastel": (0.86, 0.34),
    "normal": (0.72, 0.62),
    "saturated": (0.68, 0.98),
    "dark": (0.45, 0.85),
}
DEFAULT_FAMILY = "normal"
# What an accent has to clear against the page it is going on. Not a text ratio - an accent is
# a rule, a pip and a plot, none of which is read - but a pastel on a pale page is nothing at
# all, so the lightness moves away from the page until it is something.
ACCENT_RATIO = 1.8
# How much of its chroma the hot end of a ramp gains over its cold end, and how much a mono ramp
# moves along the lightness scale. Both from the shipped palettes: their hot ends sit around
# 0.21 chroma where the cold ends are nearer 0.14.
HOT_C = 0.21
MONO_TRAVEL = 0.30
# How far a bold ramp sweeps either side of the accent - toward the page first, then away. From
# the single-hue themes this replaces, whose ramps run a dark version of the accent, the accent,
# then a pale one: measured, 0.23 of lightness below and 0.20 above.
BOLD_TOWARD = 0.23
BOLD_AWAY = 0.20

# What a mode decides: where the page sits on the lightness scale, which way its panel steps,
# and how much of the accent's hue the greys carry. A page with none of it reads as a different
# theme's furniture behind the accent; too much and the whole screen is tinted.
# The lightnesses are the shipped dark and light palettes', read back out of them: placed
# deliberately rather than found by walking out from the page until the contrast passes, which
# lands on the dimmest ink that clears the bar and looks like it.
MODES = {
    "dark": {"bg": 0.193, "panel": 0.237, "grid": 0.323, "dim": 0.665, "ink": 0.971,
             "accent": 0.720, "hot": 0.560, "chroma": 0.020, "case": 0.22},
    "light": {"bg": 0.977, "panel": 0.944, "grid": 0.862, "dim": 0.486, "ink": 0.220,
              "accent": 0.600, "hot": 0.400, "chroma": 0.012, "case": 0.3},
}
# What ink and dim have to clear against the page: AAA for something being read, AA for
# something only naming what is beside it. Checked, not assumed - if a placed lightness misses,
# it is walked outwards until it does.
INK_RATIO = 7.0
DIM_RATIO = 4.5

# How severity is shown. `signal` travels to red the way a warning light does; `mono` stays in
# the accent's own hue and says it with lightness and chroma instead, which is what the mono,
# cyan and luminescence palettes do. Chosen from the accent rather than asked about: red is what
# a hot end means, so it is `signal` wherever the accent has somewhere to travel.
# Where `signal` ends up, and how near an accent has to be to that for the travel to be
# pointless. Measured across the shipped palettes: the ones whose ramp does not travel all sit
# within 45 degrees of their own hot end.
SIGNAL_HUE = 30.0
SIGNAL_NEAR = 45.0


def to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def to_srgb(c):
    c = 12.92 * c if c <= 0.0031308 else 1.055 * (max(c, 0.0) ** (1 / 2.4)) - 0.055
    return max(0, min(255, round(c * 255)))


def oklch(rgb):
    """(lightness 0-1, chroma, hue in degrees) for an sRGB triple."""
    r, g, b = (to_linear(v) for v in rgb)
    long = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    medium = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    short = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    long, medium, short = long ** (1 / 3), medium ** (1 / 3), short ** (1 / 3)
    lightness = 0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short
    green_red = 1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short
    blue_yellow = 0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short
    return (lightness, math.hypot(green_red, blue_yellow),
            math.degrees(math.atan2(blue_yellow, green_red)) % 360.0)


def _linear(lightness, chroma, hue):
    """Linear-light RGB for an OKLCH colour, outside 0-1 where it is outside the gamut."""
    radians = math.radians(hue)
    green_red = math.cos(radians) * chroma
    blue_yellow = math.sin(radians) * chroma
    long = lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow
    medium = lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow
    short = lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow
    long, medium, short = long ** 3, medium ** 3, short ** 3
    return (4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
            -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
            -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short)


def _in_gamut(linear):
    return all(-0.0015 <= channel <= 1.0015 for channel in linear)


def rgb(lightness, chroma, hue):
    """An sRGB triple for an OKLCH colour, with the chroma brought into gamut if it is not.

    Reducing chroma rather than clipping the channels: clipping shifts the hue, which on a
    ramp shows up as a leg that changes colour where it was only meant to darken.
    """
    for attempt in range(12):
        linear = _linear(lightness, chroma, hue)
        if _in_gamut(linear) or not chroma:
            return tuple(to_srgb(channel) for channel in linear)
        chroma *= 0.92 if attempt else 0.98
    return tuple(to_srgb(channel) for channel in linear)


def max_chroma(lightness, hue):
    """The most chroma sRGB can hold at that lightness and hue.

    How much that is depends entirely on the hue - measured over the twelve offered, from 0.128
    at cyan to 0.287 at magenta on a dark page - which is why one fixed chroma for all of them
    leaves some looking tame and others flat. Found by bisection, the gamut being convex along
    chroma for a fixed lightness and hue.
    """
    low, high = 0.0, 0.4
    for _ in range(16):
        middle = (low + high) / 2.0
        if _in_gamut(_linear(lightness, middle, hue)):
            low = middle
        else:
            high = middle
    return low


def contrast(one, other):
    """WCAG 2.1 ratio between two sRGB triples, 1.0 to 21.0.

    The same measure the firmware's `contrast` reports, so a threshold picked here means the
    same thing on the badge.
    """
    def luminance(colour):
        r, g, b = (to_linear(v) for v in colour)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    first, second = luminance(one), luminance(other)
    if second > first:
        first, second = second, first
    return (first + 0.05) / (second + 0.05)


def readable_on(lightness, chroma, hue, background, ratio):
    """That colour if it clears `ratio` against `background`, or the nearest one that does.

    Placed first and checked second: a lightness taken from a palette that works is a better
    starting point than the far side of a threshold, and most of them pass untouched. When one
    does not, it moves away from the page rather than towards it.
    """
    wanted = rgb(lightness, chroma, hue)
    if contrast(wanted, background) >= ratio:
        return wanted
    away = 1.0 if lightness >= oklch(background)[0] else -1.0
    for step in range(1, 101):
        moved = lightness + away * step * 0.01
        if not 0.0 <= moved <= 1.0:
            break
        candidate = rgb(moved, chroma, hue)
        if contrast(candidate, background) >= ratio:
            return candidate
    return rgb(1.0 if away > 0 else 0.0, 0.0, hue)


def accents(family=DEFAULT_FAMILY):
    """The twelve accents of one family, as sRGB triples.

    The same twelve whichever page they are going on: what the family sets is the lightness and
    the chroma, so a swatch is the colour that will be used and not a stand-in for it. Where one
    would be lost against the page it is going on, `palette` moves it - the swatch says which
    colour was chosen, the palette says what that means on a particular page.
    """
    lightness, part = ACCENT_FAMILIES.get(family, ACCENT_FAMILIES[DEFAULT_FAMILY])
    return [rgb(lightness, max_chroma(lightness, float(hue)) * part, float(hue))
            for hue in ACCENT_HUES]


def family_of(accent):
    """Which family a stored accent came from, or the default if it came from none."""
    wanted = tuple(accent)
    for family in ACCENT_FAMILIES:
        if wanted in [tuple(offer) for offer in accents(family)]:
            return family
    return DEFAULT_FAMILY


def offered():
    """Every accent every family offers, for checking a stored one against."""
    return [tuple(accent) for family in ACCENT_FAMILIES for accent in accents(family)]


def ramp_for(accent):
    """Which ramp suits this accent: `signal` where it has somewhere to travel, else `mono`.

    Not a choice anybody is asked to make. Travelling to red is what a warning light does and
    reads as severity without being learned, so it is the answer wherever it can be - and where
    the accent is already red, saying it again says nothing.
    """
    hue = oklch(accent)[2]
    away = abs((hue - SIGNAL_HUE + 180.0) % 360.0 - 180.0)
    return "signal" if away >= SIGNAL_NEAR else "mono"


def _signal_ramp(lightness, chroma, hue, shape):
    """Cold at the accent's own hue, hot at red, going the short way round.

    Four stops rather than two: a hue takes the short way between neighbours, so a pair more
    than half a turn apart would collapse, and the positions match the shipped ramps - most of
    the travel happens in the top third, where a reading is worth noticing.

    The hot end lands where the mode says a hot end belongs rather than a fixed distance below
    the accent, or an accent that is already dark ends up with a red nobody can see.
    """
    turn = (SIGNAL_HUE - hue + 540.0) % 360.0 - 180.0
    hot = shape["hot"]
    stops = []
    for position, part in ((0.0, 0.0), (0.45, 0.42), (0.72, 0.74), (1.0, 1.0)):
        # Held up until the last leg, the way the shipped ramps do it - 0.84, 0.79, 0.76 and
        # then 0.56 - so the climb reads as gradual and the top of it as a step.
        eased = part * part
        stops.append((position, rgb(lightness + (hot - lightness) * eased,
                                    chroma + (HOT_C - chroma) * eased,
                                    hue + turn * part)))
    return tuple(stops)


def _mono_ramp(lightness, chroma, hue, shape):
    """One hue throughout, saying severity with lightness and chroma.

    The way the mono, cyan and luminescence palettes do it: nothing changes colour, so a page
    built out of one hue stays built out of it.

    Away from the page, not towards it. A ramp that darkens on a dark page has its hot end
    receding into the background just as the reading gets interesting - which is what the
    shipped `cyan` does, and why its second graph series falls back to grey.

    The whole travel always happens: where the accent is already near the top of the scale - a
    pastel on a dark page - the window slides down instead of being squashed against the
    ceiling, which left the two ends of the ramp all but the same colour.
    """
    away = MONO_TRAVEL if shape["bg"] < 0.5 else -MONO_TRAVEL
    hot = min(0.98, max(0.06, lightness + away))
    cold = min(0.98, max(0.06, hot - away))
    stops = []
    for position, part in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)):
        stops.append((position, rgb(cold + (hot - cold) * part,
                                    chroma + (HOT_C - chroma) * part, hue)))
    return tuple(stops)


def _bold_ramp(lightness, chroma, hue, shape):
    """One hue swept through the range it has: a dark version of the accent, the accent, a pale
    one - or the other way round on a pale page, away from it being the direction that shows.

    What the single-hue themes did. Their ramps put the accent at 0.7 and travelled further
    below it than above, which is what makes the top of the sweep read as the reading getting
    away from you rather than as the gauge simply filling.
    """
    toward = BOLD_TOWARD if shape["bg"] < 0.5 else -BOLD_TOWARD
    stops = []
    for position, level, part in ((0.0, lightness - toward, 0.9),
                                  (0.7, lightness, 1.0),
                                  (1.0, lightness + toward * (BOLD_AWAY / BOLD_TOWARD), 0.85)):
        stops.append((position, rgb(min(0.97, max(0.06, level)), chroma * part, hue)))
    return tuple(stops)


def palette(accent, mode="dark", bold=False):
    """A whole palette from one accent, as `themes.PALETTES` holds them.

    The greys carry a little of the accent's hue so the furniture belongs to it, and `ink` and
    `dim` are placed by contrast rather than by taste: 7 is AAA for body text and 4.5 is AA,
    which is what a label wants when it is only naming the thing beside it.

    `bold` is the other variant: the accent at its hue's own limit and a ramp that stays in the
    hue, sweeping lightness instead of travelling to red.
    """
    if mode not in MODES:
        mode = "dark"
    ramp = ramp_for(accent)
    shape = MODES[mode]
    lightness, chroma, hue = oklch(accent)
    tint = shape["chroma"]
    background = rgb(shape["bg"], tint, hue)
    # As picked, unless the page it is going on would swallow it: a pastel is nothing on a pale
    # page and a dark accent nothing on a dark one, and the same swatch is offered for both.
    placed = readable_on(lightness, chroma, hue, background, ACCENT_RATIO)
    lightness, chroma, _hue = oklch(placed)
    build = _bold_ramp if bold else (_signal_ramp if ramp == "signal" else _mono_ramp)
    return {
        "bg": background,
        "panel": rgb(shape["panel"], tint, hue),
        "ink": readable_on(shape["ink"], tint * 0.7, hue, background, INK_RATIO),
        "dim": readable_on(shape["dim"], tint * 1.8, hue, background, DIM_RATIO),
        "accent": rgb(lightness, chroma, hue),
        "grid": rgb(shape["grid"], tint * 1.5, hue),
        "case": shape["case"],
        "ramp": build(lightness, chroma, hue, shape),
    }
