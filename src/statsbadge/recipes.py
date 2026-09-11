"""Ready-made pages, loaded from recipes.toml."""

import copy
import tomllib
from importlib import resources

from . import layout

# Built from layout.DEFAULT_PAGES rather than written out again, so what a new badge
# shows and what this puts back cannot drift apart.
DEFAULTS = "defaults"


def _load():
    text = resources.files(__package__).joinpath("recipes.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


WRITTEN = _load()


def written():
    """Return the recipes in recipes.toml, in the order it names them."""
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
    """Return every recipe this host can fill in, each trimmed to the fields it reports."""
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
    """Return one page with the fields this host cannot fill dropped, or None."""
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
