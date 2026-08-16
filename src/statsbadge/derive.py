"""A whole palette out of one accent colour.

Built here and not on the badge, where a derived palette and a written one are the same
thing.
The config UI shows one before anybody commits to it.

OKLCH throughout, where lightness matches perceived lightness and "ink has to be readable
on bg" becomes arithmetic. The conversion mirrors the firmware's to within a count. Bytes
in every axis, as the firmware uses them: l spans 0-1, c spans 0-0.35, h is 256 to a turn.

`signal` sends the ramp to red as a warning light does. `mono` keeps the accent's hue and
carries severity in lightness and chroma instead. Picked from the accent, never asked about:
the shipped palettes whose ramp holds still sit within 45 degrees of their hot end, which is
where SIGNAL_NEAR comes from.

A picture's levels are fixed and the same for every theme: the host dithers to a position
on a ramp without knowing which theme will draw it, so index 2 of four means one
brightness everywhere. Both counts are sent, since index 2 of four is not index 2 of eight.
"""

import math

ACCENT_HUES = tuple(range(0, 360, 30))
# (lightness, share of the hue's chroma limit). A share and not a fixed chroma: capacity
# runs from 0.128 at cyan to 0.287 at magenta on a dark page, and one number for all of them
# does not hold a family together. `saturated` is where the single-hue themes sat.
ACCENT_FAMILIES = {
    "pastel": (0.86, 0.34),
    "normal": (0.72, 0.62),
    "saturated": (0.68, 0.98),
    "dark": (0.45, 0.85),
}
DEFAULT_FAMILY = "normal"
# What an accent has to clear against its page, under INK_RATIO and DIM_RATIO below: an
# accent is a rule or a pip, not something read.
ACCENT_RATIO = 1.8
# Chroma the hot end gains over the cold, and how far a mono ramp moves along lightness.
# From the shipped palettes: hot ends near 0.21 chroma, cold nearer 0.14.
HOT_C = 0.21
MONO_TRAVEL = 0.30
# How far a bold ramp sweeps either side of the accent. From the single-hue themes this
# replaces: 0.23 of lightness below and 0.20 above.
BOLD_TOWARD = 0.23
BOLD_AWAY = 0.20

# Where the page sits on the lightness scale, and how much of the accent's hue the greys
# carry. The lightnesses are the shipped palettes', placed by hand.
#
# Colourfulness is either `chroma`, an absolute, or `share`, a fraction of the hue's limit at
# that lightness. The glow shapes use the share: luminescence runs 0.030 to 0.100 of chroma
# across its roles at a share of about 0.6.
SHAPES = {
    "dark": {"bg": 0.193, "panel": 0.237, "grid": 0.323, "dim": 0.665, "ink": 0.971,
             "hot": 0.560, "chroma": 0.020},
    "light": {"bg": 0.977, "panel": 0.944, "grid": 0.862, "dim": 0.486, "ink": 0.220,
              "hot": 0.400, "chroma": 0.012},
    # A lit panel, always drawn with the bold ramp: a ramp travelling to red is not
    # monochrome whatever the furniture does. No `hot`; only the signal ramp reads it.
    "glow-dark": {"bg": 0.225, "panel": 0.272, "grid": 0.350, "dim": 0.600, "ink": 0.855,
                  "share": 0.58, "ink_ratio": 4.5, "dim_ratio": 2.2},
    "glow-light": {"bg": 0.930, "panel": 0.888, "grid": 0.820, "dim": 0.545, "ink": 0.345,
                   "share": 0.55, "ink_ratio": 4.5, "dim_ratio": 2.2},
}
DEFAULT_SHAPE = "dark"

# How much of the shape's colourfulness each role takes. Two tables, since one is a multiple
# of an absolute and the other of a share.
ROLE_CHROMA = {"bg": 1.0, "panel": 1.0, "grid": 1.5, "ink": 0.7, "dim": 1.8}
ROLE_SHARE = {"bg": 1.0, "panel": 1.0, "grid": 1.0, "ink": 0.55, "dim": 1.0}

# What ink and dim have to clear against the page: AAA for something read, AA for
# something naming what is beside it. A shape may lower them, and the glow pair do.
INK_RATIO = 7.0
DIM_RATIO = 4.5

# Where `signal` ends up, and how near an accent has to be for the travel to be pointless.
SIGNAL_HUE = 30.0
SIGNAL_NEAR = 45.0

# A colour used sparingly beside the first, a graph's second series most of all. `same`
# matches a palette without one; `contrasting` is measured, in `second_accent`.
ACCENT_B_RULES = ("same", "complementary", "triadic", "contrasting")
ACCENT_B_TURNS = {"same": 0.0, "triadic": 120.0, "complementary": 180.0}


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

    Chroma is reduced, and the channels are never clipped. Clipping shifts the hue, which
    on a ramp shows up as a leg that changes colour where it was only meant to darken.
    """
    for attempt in range(12):
        linear = _linear(lightness, chroma, hue)
        if _in_gamut(linear) or not chroma:
            return tuple(to_srgb(channel) for channel in linear)
        chroma *= 0.92 if attempt else 0.98
    return tuple(to_srgb(channel) for channel in linear)


def max_chroma(lightness, hue):
    """The most chroma sRGB can hold at that lightness and hue.

    By bisection, the gamut being convex along chroma for a fixed lightness and hue.
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

    The same measure the firmware's `contrast` reports, and a threshold picked here means the
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

    Placed first and checked second: a lightness taken from a palette that works beats the
    far side of a threshold, and most pass untouched. One that misses moves away from the
    page, never towards it.
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

    The same twelve whichever page they are going on: the family sets the lightness and the
    chroma. A swatch is the colour that will be used and not a stand-in, except where the
    page would swallow it, which `palette` corrects.
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


def apart(one, other):
    """How far two sRGB colours are in OKLab, black to white being 100.

    The same scale the firmware's `difference` reports, and a threshold means the same thing
    here as it does on the badge.
    """
    def lab(colour):
        lightness, chroma, hue = oklch(colour)
        radians = math.radians(hue)
        return (lightness, chroma * math.cos(radians), chroma * math.sin(radians))

    first, second = lab(one), lab(other)
    return 100.0 * math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second, strict=True)))


def second_accent(accent, rule="same"):
    """The accent used sparingly beside the first, by one of the rules.

    Kept in the accent's family, at the same lightness and the same share of its hue's
    limit, which makes the two look like one palette's two colours.
    """
    lightness, chroma, hue = oklch(accent)
    if rule not in ACCENT_B_RULES or rule == "same":
        return tuple(accent)
    limit = max_chroma(lightness, hue)
    part = min(1.0, chroma / limit) if limit else 0.0
    if rule == "contrasting":
        offers = [rgb(lightness, max_chroma(lightness, float(turn)) * part, float(turn))
                  for turn in ACCENT_HUES]
        return max(offers, key=lambda offer: apart(accent, offer))
    turned = (hue + ACCENT_B_TURNS[rule]) % 360.0
    return rgb(lightness, max_chroma(lightness, turned) * part, turned)


def ramp_for(accent):
    """Which ramp suits this accent: `signal` where it has somewhere to travel, else `mono`.

    Not a choice anybody is asked to make. Travelling to red reads as severity without being
    learned, so it wins wherever there is room. Where the accent is already red the travel
    would be invisible.
    """
    hue = oklch(accent)[2]
    away = abs((hue - SIGNAL_HUE + 180.0) % 360.0 - 180.0)
    return "signal" if away >= SIGNAL_NEAR else "mono"


def _signal_ramp(lightness, chroma, hue, shape):
    """Cold at the accent's hue, hot at red, going the short way round.

    Four stops and not two. A hue takes the short way between neighbours, so a pair more
    than half a turn apart would collapse. The positions match the shipped ramps, where
    most of the travel happens in the top third.

    The hot end lands on the shape's own `hot` lightness, not a fixed distance below the
    accent: an accent that is already dark would otherwise end in a red nobody can see.
    """
    turn = (SIGNAL_HUE - hue + 540.0) % 360.0 - 180.0
    hot = shape["hot"]
    stops = []
    for position, part in ((0.0, 0.0), (0.45, 0.42), (0.72, 0.74), (1.0, 1.0)):
        # Lightness held up until the last leg, as the shipped ramps do. Theirs run 0.84,
        # 0.79, 0.76, then 0.56.
        eased = part * part
        stops.append((position, rgb(lightness + (hot - lightness) * eased,
                                    chroma + (HOT_C - chroma) * eased,
                                    hue + turn * part)))
    return tuple(stops)


def _mono_ramp(lightness, chroma, hue, shape):
    """One hue throughout, saying severity with lightness and chroma.

    As the mono, cyan and luminescence palettes do it: nothing changes colour.

    Away from the page and not towards it. A ramp darkening on a dark page has its hot end
    receding just as the reading gets interesting, as the shipped `cyan` does, which is why
    its second graph series falls back to grey.

    The whole travel always happens, the window sliding down where the accent is near the top
    of the scale. Clamping the ends instead brought both out the same colour.
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
    """One hue swept through the range it has: a dark version of the accent, the accent, a
    pale one. The other way round on a pale page, away from the page being the direction
    that shows.

    What the single-hue themes did. Their ramps put the accent at 0.7 and travelled further
    below it than above, which makes the top of the sweep look like the reading getting away
    from you and not the gauge filling.
    """
    toward = BOLD_TOWARD if shape["bg"] < 0.5 else -BOLD_TOWARD
    stops = []
    for position, level, part in ((0.0, lightness - toward, 0.9),
                                  (0.7, lightness, 1.0),
                                  (1.0, lightness + toward * (BOLD_AWAY / BOLD_TOWARD), 0.85)):
        stops.append((position, rgb(min(0.97, max(0.06, level)), chroma * part, hue)))
    return tuple(stops)


# Where a picture's levels sit, in OKLCH lightness. Short of black and white at both
# ends, or the picture runs past its background and reads as a hole in the screen.
IMAGE_DARK = 0.16
IMAGE_LIGHT = 0.94
# The level counts `imaging` produces. Both travel; see the module docstring.
IMAGE_LEVELS = (4, 8)


def image_ramp(accent, levels):
    """The shades a picture of `levels` is drawn in, darkest first.

    Evenly spaced across a fixed lightness range, in the accent's hue at its share of what
    that hue can hold in chroma. The share is against the limit at each lightness, so the ends
    come out near neutral as a monochrome display does: `luminescence` takes 0.91 of its
    green and gets a phosphor screen, `mono` 0.00 and a grey picture.

    Held apart from the theme's `ramp`, which travels calm to alarming and would draw a
    photograph as a heat map.
    """
    if levels < 2:
        levels = 2
    lightness, chroma, hue = oklch(accent)
    limit = max_chroma(lightness, hue)
    share = (chroma / limit) if limit else 0.0
    span = (IMAGE_LIGHT - IMAGE_DARK) / (levels - 1)
    shades = []
    for step in range(levels):
        level = IMAGE_DARK + span * step
        shades.append(rgb(level, max_chroma(level, hue) * share, hue))
    return tuple(shades)


def image_ramps(accent):
    """Both level counts, keyed by how many, in the form a palette stores them."""
    return {str(levels): [list(colour) for colour in image_ramp(accent, levels)]
            for levels in IMAGE_LEVELS}


def tone(shape, role, hue):
    """The lightness and chroma a role takes in this shape, at this hue."""
    level = shape[role]
    if "share" in shape:
        return level, max_chroma(level, hue) * shape["share"] * ROLE_SHARE[role]
    return level, shape["chroma"] * ROLE_CHROMA[role]


def palette(accent, shape="dark", bold=False, second="same"):
    """A whole palette from one accent, shaped like the written-down ones.

    The greys carry some of the accent's hue so the furniture belongs to it. A little for
    the plain shapes; most of it for the glow pair, which makes a lit panel.

    `ink` and `dim` are placed by contrast and not by taste, against whatever the shape
    asks for. `bold` keeps the ramp in the accent's hue instead of sending it to red.
    `second` picks the second accent: the chrome, and a graph's second series.
    """
    shape = SHAPES.get(shape, SHAPES[DEFAULT_SHAPE])
    lightness, chroma, hue = oklch(accent)
    background = rgb(*tone(shape, "bg", hue), hue)
    # As picked, unless the page would swallow it: the same swatch is offered for both modes.
    placed = readable_on(lightness, chroma, hue, background, ACCENT_RATIO)
    lightness, chroma, _hue = oklch(placed)
    build = _bold_ramp if bold else (_signal_ramp if ramp_for(accent) == "signal"
                                     else _mono_ramp)

    def at(role, ratio=None):
        level, own = tone(shape, role, hue)
        if ratio is None:
            return rgb(level, own, hue)
        return readable_on(level, own, hue, background, ratio)

    settled = rgb(lightness, chroma, hue)
    return {
        "bg": background,
        "panel": at("panel"),
        "ink": at("ink", shape.get("ink_ratio", INK_RATIO)),
        "dim": at("dim", shape.get("dim_ratio", DIM_RATIO)),
        "accent": settled,
        "accent_b": second_accent(settled, second),
        "grid": at("grid"),
        "ramp": build(lightness, chroma, hue, shape),
        # Fixed lightnesses in this theme's hue, so a picture keeps its levels in any palette.
        "image": image_ramps(settled),
    }
