"""A whole palette out of one accent colour.

The badge has colour arithmetic in firmware, but a palette has to be built here. The
config UI shows one before anybody commits to it, and what travels to the badge is a
palette like any other, leaving the badge unable to tell a derived theme from a written one.

Everything below works in OKLCH, where lightness matches perceived lightness, which turns
"ink has to be readable on bg" into arithmetic. The conversion mirrors the firmware's;
checked against it colour by colour, the round trip agrees to within a count.

Bytes in every axis for the same reason the firmware uses them: l spans 0-1 lightness, c
spans 0-0.35 chroma, h is 256 counts to a turn.

Two decisions carry most of the weight.

**How severity is shown.** `signal` travels to red the way a warning light does. `mono`
holds the accent's hue and says it with lightness and chroma, as the mono, cyan and
luminescence palettes do. Chosen from the accent and never asked about, since red is what
a hot end means. The shipped palettes whose ramp holds still all sit within 45 degrees of
their hot end, and SIGNAL_NEAR comes from that.

**Where a picture's levels sit.** Fixed, and the same for every theme. The host dithers to
a position on that ramp with no say in which theme draws it, so index 2 of four has to mean
the same brightness on every palette. Both level counts travel, since index 2 of four and
index 2 of eight are different brightnesses and a badge cannot work one out from the other.
"""

import math

# The accents on offer. Twelve hues evenly round the wheel, in four families. A taste call
# and not a derivation. These are the twelve a wheel gives, and the list is arguable.
ACCENT_HUES = tuple(range(0, 360, 30))
# The four families of accent, as (lightness, share of the hue's chroma limit). Twelve hues
# each, so forty-eight colours. The family says how loud the accent is and the hue is the
# choice.
#
# `saturated` is where the single-hue themes sat, every one within 0.003 of its hue's limit.
# `normal` is a little short of it.
#
# How much chroma a hue can hold varies enormously, 0.128 at cyan against 0.287 at magenta
# on a dark page. A fraction of the limit keeps a family looking like one family where a
# fixed number does not.
ACCENT_FAMILIES = {
    "pastel": (0.86, 0.34),
    "normal": (0.72, 0.62),
    "saturated": (0.68, 0.98),
    "dark": (0.45, 0.85),
}
DEFAULT_FAMILY = "normal"
# What an accent has to clear against the page it is going on. Below a text ratio, an
# accent being a rule, a pip and a plot. A pastel on a pale page disappears.
ACCENT_RATIO = 1.8
# How much chroma a ramp's hot end gains over its cold end, and how far a mono ramp moves
# along lightness. From the shipped palettes: hot ends near 0.21 chroma, cold nearer 0.14.
HOT_C = 0.21
MONO_TRAVEL = 0.30
# How far a bold ramp sweeps either side of the accent, toward the page then away. From the
# single-hue themes this replaces, whose ramps run dark accent, accent, pale accent.
# Measured at 0.23 of lightness below and 0.20 above.
BOLD_TOWARD = 0.23
BOLD_AWAY = 0.20

# What a mode sets. Where the page sits on the lightness scale, which way its panel steps,
# and how much of the accent's hue the greys carry. A page carrying none of it looks like
# another theme's furniture behind the accent, and too much tints the whole screen.
#
# The lightnesses are read back out of the shipped dark and light palettes. Placed by hand,
# not found by walking out from the page until the contrast passes, which lands on the
# dimmest ink that clears the bar and looks like it.
MODES = {
    "dark": {"bg": 0.193, "panel": 0.237, "grid": 0.323, "dim": 0.665, "ink": 0.971,
             "accent": 0.720, "hot": 0.560, "chroma": 0.020, "case": 0.22},
    "light": {"bg": 0.977, "panel": 0.944, "grid": 0.862, "dim": 0.486, "ink": 0.220,
              "accent": 0.600, "hot": 0.400, "chroma": 0.012, "case": 0.3},
}
# What ink and dim have to clear against the page. AAA for something being read, AA for
# something only naming what is beside it. Checked, and a placed lightness that misses is
# walked outwards until it passes.
INK_RATIO = 7.0
DIM_RATIO = 4.5

# Where `signal` ends up, and how near an accent has to be for the travel to be pointless.
SIGNAL_HUE = 30.0
SIGNAL_NEAR = 45.0

# The second accent. A colour used sparingly beside the first, a graph's second series
# being all of it. `same` matches a palette without one, and the rotations come off the
# wheel. `contrasting` is measured, and picked in `_contrasting`.
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

    How much that is turns on the hue. Measured over the twelve offered, from 0.128 at
    cyan to 0.287 at magenta on a dark page. One fixed chroma for all of them leaves some
    tame and others flat.

    Found by bisection, the gamut being convex along chroma for a fixed lightness and
    hue.
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

    Placed first and checked second. A lightness taken from a palette that works is a
    better starting point than the far side of a threshold, and most pass untouched. One
    that misses moves away from the page, never towards it.
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

    The same twelve whichever page they are going on. The family sets the lightness and
    the chroma, and a swatch is the colour that will be used, not a stand-in.

    Where one would be lost against its page, `palette` moves it. The swatch says which
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

    Not a choice anybody is asked to make. Travelling to red is what a warning light does and
    reads as severity without being learned, so it is the answer wherever it can be - and where
    the accent is already red, saying it again says nothing.
    """
    hue = oklch(accent)[2]
    away = abs((hue - SIGNAL_HUE + 180.0) % 360.0 - 180.0)
    return "signal" if away >= SIGNAL_NEAR else "mono"


def _signal_ramp(lightness, chroma, hue, shape):
    """Cold at the accent's hue, hot at red, going the short way round.

    Four stops and not two. A hue takes the short way between neighbours, so a pair more
    than half a turn apart would collapse. The positions match the shipped ramps, where
    most of the travel happens in the top third.

    The hot end lands where the mode says a hot end belongs, and not a fixed distance below
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

    The way the mono, cyan and luminescence palettes do it. Nothing changes colour, and a
    page built out of one hue stays built out of it.

    Away from the page, not towards it. A ramp that darkens on a dark page has its hot end
    receding into the background just as the reading gets interesting, as the shipped `cyan`
    does, which is why its second graph series falls back to grey.

    The whole travel always happens. Where the accent is already near the top of the scale,
    a pastel on a dark page, the window slides down. Squashed against the ceiling instead,
    the two ends of the ramp came out all but the same colour.
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


# Where a picture's levels sit, in OKLCH lightness. Ends short of black and white, a page
# being neither, and a picture running past its background is a hole in the screen.
IMAGE_DARK = 0.16
IMAGE_LIGHT = 0.94
# How colourful a picture is. The share of its hue's limit that the theme's accent takes
# of its. `luminescence` takes 0.91 of its green and gets a phosphor screen. `mono` has a grey
# accent at 0.00 and gets a grey picture. The level is the information, the hue is whose
# screen it is on.
#
# Taken against the hue's limit at each lightness, and not one number for the ramp. Chroma
# capacity varies along the scale, and the ends come out near neutral, as a monochrome
# display does with its blacks and whites.

# The level counts `imaging` produces. Both travel; see the module docstring.
IMAGE_LEVELS = (4, 8)


def image_ramp(accent, levels):
    """The shades a picture of `levels` is drawn in, darkest first.

    Evenly spaced across a fixed lightness range, in the accent's hue and at its share of
    what that hue can hold. Held apart from the theme's `ramp`, which travels calm to
    alarming and would draw a photograph as a heat map.
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
    """Both level counts, keyed by how many, for a palette to carry."""
    return {str(levels): [list(colour) for colour in image_ramp(accent, levels)]
            for levels in IMAGE_LEVELS}


def palette(accent, mode="dark", bold=False, second="same"):
    """A whole palette from one accent, as `themes.PALETTES` holds them.

    The greys carry a little of the accent's hue so the furniture belongs to it. `ink` and
    `dim` are placed by contrast and not by taste: 7 is AAA for body text and 4.5 is AA,
    which suits a label that only names the thing beside it.

    `bold` is the other variant, a ramp that stays in the accent's hue and sweeps lightness
    without travelling to red. `second` is how the second accent is chosen: the colour a
    graph's second series is drawn in, and the only place a palette repeats itself.
    """
    if mode not in MODES:
        mode = "dark"
    ramp = ramp_for(accent)
    shape = MODES[mode]
    lightness, chroma, hue = oklch(accent)
    tint = shape["chroma"]
    background = rgb(shape["bg"], tint, hue)
    # As picked, unless the page would swallow it. A pastel disappears on a pale page and
    # a dark accent on a dark one, and the same swatch is offered for both.
    placed = readable_on(lightness, chroma, hue, background, ACCENT_RATIO)
    lightness, chroma, _hue = oklch(placed)
    build = _bold_ramp if bold else (_signal_ramp if ramp == "signal" else _mono_ramp)
    return {
        "bg": background,
        "panel": rgb(shape["panel"], tint, hue),
        "ink": readable_on(shape["ink"], tint * 0.7, hue, background, INK_RATIO),
        "dim": readable_on(shape["dim"], tint * 1.8, hue, background, DIM_RATIO),
        "accent": rgb(lightness, chroma, hue),
        "accent_b": second_accent(rgb(lightness, chroma, hue), second),
        "grid": rgb(shape["grid"], tint * 1.5, hue),
        "case": shape["case"],
        "ramp": build(lightness, chroma, hue, shape),
        # Fixed lightnesses in this theme's hue, giving a picture the same levels whatever
        # palette is drawing it.
        "image": image_ramps(rgb(lightness, chroma, hue)),
    }
