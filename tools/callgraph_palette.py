#!/usr/bin/env python3
"""Sample the app's own themes into a ramp the viewer can index.

    (imported by tools/callgraph.py, not run on its own)

The colours come from src/statsbadge/themes.py, which is where they are written down and
what index.html copies, and the interpolation from src/statsbadge/derive.py, which mirrors
the firmware's own OKLCH transform. So a hot node in the viewer is the colour the badge
would fill a gauge with at that reading, and there is no second copy of either.

Sampled here and not in the browser, for three reasons. No colour maths in the
JavaScript. No CSS to read back at runtime, since `getPropertyValue` hands back the
unresolved `light-dark(...)` token and not a colour. And the 32 steps are the
quantisation the viewer's sprite cache indexes at in any case.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from statsbadge import derive, themes  # noqa: E402

# Enough steps to read as a gradient, few enough to pre-render a sprite for each.
STEPS = 32

# The two the page itself is drawn in. The rest of PALETTES is offered to a badge, and a
# theme picker is not what this tool is for.
MODES = ("light", "dark")


def palettes():
    """Both palettes, each with its ramp flattened to `STEPS` hex strings."""
    built = {}
    for mode in MODES:
        palette = themes.written()[mode]
        built[mode] = {
            "bg": hex_of(palette["bg"]),
            "panel": hex_of(palette["panel"]),
            "ink": hex_of(palette["ink"]),
            "dim": hex_of(palette["dim"]),
            "accent": hex_of(palette["accent"]),
            "grid": hex_of(palette["grid"]),
            "ramp": [hex_of(colour) for colour in sample(palette["ramp"])],
            "stops": [hex_of(colour) for _, colour in palette["ramp"]],
        }
    return built


def sample(stops, steps=STEPS):
    """A ramp's stops as evenly spaced sRGB, interpolated in OKLCH.

    The stops are not evenly spaced - the default ramp puts them at 0, 0.45, 0.72 and 1 -
    and cyan to green to amber interpolated in sRGB runs through a muddy olive on the way.
    Lightness and chroma move linearly and hue takes the shorter way round, which keeps
    each leg the colour it was picked to be.
    """
    lab = [(where, derive.oklch(colour)) for where, colour in stops]
    out = []
    for step in range(steps):
        at = step / (steps - 1) if steps > 1 else 0.0
        out.append(derive.rgb(*between(lab, at)))
    return out


def between(lab, at):
    """The OKLCH colour a fraction of the way along a ramp of positioned stops."""
    if at <= lab[0][0]:
        return lab[0][1]
    for (low, first), (high, second) in zip(lab, lab[1:], strict=False):
        if at > high:
            continue
        span = high - low
        along = (at - low) / span if span else 0.0
        lightness = first[0] + (second[0] - first[0]) * along
        chroma = first[1] + (second[1] - first[1]) * along
        return (lightness, chroma, hue_between(first[2], second[2], along))
    return lab[-1][1]


def hue_between(first, second, along):
    """Around the hue circle the short way, so a ramp never doubles back through green."""
    difference = (second - first + 180.0) % 360.0 - 180.0
    return (first + difference * along) % 360.0


def hex_of(colour):
    red, green, blue = colour
    return f"#{red:02x}{green:02x}{blue:02x}"
