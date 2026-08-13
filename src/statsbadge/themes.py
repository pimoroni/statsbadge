"""The themes, loaded from themes.toml.

A theme is a table of colours and one gradient rule, so it is config. It lives in a data
file, where nobody can write a special case. The selected one travels in the layout, and
the badge carries one to boot with.

  bg / panel      the page, and the header, footer and tiles on it
  ink / dim       text, and text that is only labelling something
  accent          the one colour that says "this is the thing"
  accent_b        chrome, and a graph's second series; the accent where a theme names none
  grid            the unfilled part of any gauge, and a graph's rules
  ramp            what a gauge fills with as it climbs, cold to hot

A table names those, or carries a `derived` spec and is built from whatever accent the
config picked. Nothing declares which mode it is: `records` reads that off the background,
since a theme that had to declare it could declare it wrong.
"""

import tomllib
from importlib import resources

from . import derive

DEFAULT = "dark"

# Where a page stops being dark and starts being light, as OKLCH lightness of the background.
PALE_FROM = 0.5

# The roles a written palette names. `accent_b` is optional; the rest are not.
ROLES = ("bg", "panel", "ink", "dim", "accent", "grid")


def _load():
    text = resources.files(__package__).joinpath("themes.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    aliases = data.pop("aliases", {})
    return data, aliases


THEMES, ALIASES = _load()


def label(name):
    """What a picker calls a theme: the label given, or the name title cased."""
    given = THEMES.get(name, {}).get("label")
    return given or name.replace("-", " ").replace("_", " ").title()


def _stored(record):
    """A written table as a palette: tuples, and only the colour keys."""
    palette = {role: tuple(record[role]) for role in ROLES}
    if "accent_b" in record:
        palette["accent_b"] = tuple(record["accent_b"])
    palette["ramp"] = tuple((at, tuple(rgb)) for at, rgb in record["ramp"])
    return palette


def written():
    """The written-down palettes, keyed by name, without the picker's metadata."""
    return {name: _stored(record) for name, record in THEMES.items()
            if "derived" not in record}


def mode(name):
    """Which half of the picker a theme belongs in, read off its background.

    A derived theme's background lightness is its shape's, whatever accent it is built from,
    so no palette has to be built to answer this.
    """
    record = THEMES.get(name) or THEMES[DEFAULT]
    spec = record.get("derived")
    lightness = (derive.SHAPES[spec["shape"]]["bg"] if spec
                 else derive.oklch(record["bg"])[0])
    return "light" if lightness >= PALE_FROM else "dark"


def records():
    """Every theme with what a picker needs: the label, the mode, and whether it takes an
    accent. `derived` is what the UI gates the accent picker on."""
    return [{"name": name, "label": label(name), "mode": mode(name),
             "pair": record.get("pair"), "derived": "derived" in record}
            for name, record in THEMES.items()]


def resolve(name, tint):
    """A theme name and accent, with a retired name mapped onto what replaced it."""
    aliased = ALIASES.get(name)
    if not aliased:
        return name, tint
    at = derive.ACCENT_HUES.index(int(aliased["hue"])) if "hue" in aliased else None
    return aliased["theme"], (list(derive.accents("saturated")[at]) if at is not None
                              else tint)


def palette(name, accent, second="same"):
    """The palette a theme draws with: derived from the accent, or looked up.

    `accent` and `second` are consulted only where the theme is derived. A written palette is
    fixed as drawn, which is the point of writing it down.
    """
    record = THEMES.get(name) or THEMES[DEFAULT]
    spec = record.get("derived")
    if spec:
        return derive.palette(tuple(accent), spec["shape"], spec.get("bold", False), second)
    stored = _stored(record)
    # Derived from the accent's hue, as `stripe` is.
    stored["image"] = derive.image_ramps(stored["accent"])
    return stored
