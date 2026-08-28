"""Ready-made pages, loaded from recipes.toml.

A recipe is a page, or a few, with the fields already picked: "GPU Overview" against a
dial and four dropdowns. It is data, so a new one is a table in the file and needs no
release of anything.

An extension contributes its own in the same shape, through `badge_recipes` on its source.

`offered` is what the config UI lists. Only what the host reports gets there: a page keeps
the fields this machine can fill, and a recipe with no page left is not offered, which is
the check `layout.prune` makes. Which pool a slot draws from is not checked, so a graph
pointed at a field the host keeps no history for is an authoring mistake in the file rather
than something caught here.
"""

import copy
import tomllib
from importlib import resources

from . import layout

# The recipe built from layout.DEFAULT_PAGES rather than written out again, so what a new
# badge shows and what this puts back cannot drift apart.
DEFAULTS = "defaults"


def _load():
    text = resources.files(__package__).joinpath("recipes.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


WRITTEN = _load()


def written():
    """The recipes in recipes.toml, in the order it names them."""
    return [_record(name, entry) for name, entry in WRITTEN.items()]


def _record(name, entry, from_extension=None):
    return {"name": name,
            "title": entry.get("title") or name,
            "summary": entry.get("summary", ""),
            "from_extension": from_extension,
            "pages": [dict(page) for page in entry.get("pages") or ()]}


def _defaults():
    return {"name": DEFAULTS, "title": "Default Pages",
            "summary": "Every page a badge starts out showing.",
            "from_extension": None,
            "pages": copy.deepcopy(layout.DEFAULT_PAGES)}


def offered(capabilities, extra=()):
    """Every recipe this host can fill in, each trimmed to the fields it reports.

    `extra` is what the installed extensions contribute, which is filtered the same way:
    an extension's page kind is only drawable while it is installed.
    """
    kinds = set(layout.KINDS)
    kinds |= {page.get("kind") for page in capabilities.get("extension_pages") or ()}
    available = capabilities.get("available") or {}

    contributed = [_record(entry.get("name"), entry, entry.get("from_extension"))
                   for entry in extra]

    ready = []
    for recipe in written() + [_defaults()] + contributed:
        fitted = [_fitted(page, kinds, available) for page in recipe["pages"]]
        pages = [page for page in fitted if page]
        if pages:
            ready.append({**recipe, "pages": pages})
    return ready


def _fitted(page, kinds, available):
    """One page with the fields this host cannot fill dropped, or None if nothing is left.

    A recipe's gauge is the page: without the field it points at there is no page to add. A
    readout beside it is not, and a list of fields keeps whichever of them arrived.
    """
    if page.get("kind") not in kinds:
        return None

    def has(ref):
        group, _dot, field = ref.partition(".")
        return field in available.get(group, ())

    fitted = dict(page)
    if fitted.get("field") and not has(fitted["field"]):
        return None
    if fitted.get("readouts"):
        fitted["readouts"] = [ref for ref in fitted["readouts"] if has(ref)]
    if fitted.get("fields"):
        kept = [ref for ref in fitted["fields"] if has(ref)]
        if not kept:
            return None
        fitted["fields"] = kept
    return fitted
