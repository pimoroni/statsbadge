#!/usr/bin/env python3
"""Write the host's palettes where a badge-side tool can read them.

    python3 tools/dump_themes.py
    mpremote connect PORT mount . run tools/probe.py

`tools/probe.py` draws with the palette the host would send, which carries the ramps a
gauge fills with and the greys a picture is redrawn in. It cannot ask for them itself:
`statsbadge.themes` reads a TOML file through importlib, and MicroPython has neither.

So the palettes are dumped to JSON here, on the host, and read over the mount there.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from statsbadge import themes  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "themes.json")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # Through palette() and not written(). palette() is what a badge is sent, and it adds
    # the greys a picture is redrawn in; written() is the table as typed.
    made = {name: themes.palette(name, themes.written()[name]["accent"])
            for name in themes.written()}
    with open(OUT, "w") as handle:
        json.dump(made, handle)
    print(f"wrote {len(made)} palettes to {os.path.normpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
