#!/usr/bin/env python3
"""Write the host's palettes where a badge-side tool can read them."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from statsbadge import derive, themes  # noqa: E402

# A few of the derived themes at chosen accents, so the project page can show what tinting
# does. The accent is taken from the family the picker offers, by position.
TINTS = (
    ("tinted_bold_magenta", "tinted-bold-dark", "saturated", 0),
    ("tinted_glow_amber", "tinted-glow-dark", "saturated", 2),
    # Bold rather than plain Tinted: plain keeps the signal ramp, which travels to red
    # whatever the accent.
    ("tinted_bold_blue", "tinted-bold-light", "saturated", 8),
)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "themes.json")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # Through palette() and not written(). palette() is what a badge is sent, and it adds
    # the greys a picture is redrawn in.
    made = {name: themes.palette(name, themes.written()[name]["accent"])
            for name in themes.written()}
    for key, theme, family, at in TINTS:
        made[key] = themes.palette(theme, derive.accents(family)[at])
    with open(OUT, "w") as handle:
        json.dump(made, handle)
    print(f"wrote {len(made)} palettes to {os.path.normpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
