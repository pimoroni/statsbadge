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

    shape = layout.KIND_SHAPE.get(page["kind"], {"many": "fields"})
    one, many = shape.get("one"), shape.get("many")
    fitted = dict(page)
    if one and fitted.get(one) and not has(fitted[one]):
        return None
    if many and fitted.get(many):
        kept = [ref for ref in fitted[many] if has(ref)]
        if not kept and not one:
            return None
        fitted[many] = kept
    return fitted
