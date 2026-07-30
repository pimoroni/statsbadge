"""Extensions: a pip install away from a new page.

An extension is a normal package advertising a `statsbadge.sources` entry point. It
contributes a source (so it can put anything in the frame) and, optionally, badge-side
Python that the server pushes to the badge so a page can animate at 45fps instead of
being a picture fetched over the wire.

    [project.entry-points."statsbadge.sources"]
    weather = "statsbadge_weather:Weather"

The class is a `sources.base.Source` with two extras:

    badge_module     path to a .py to install into the app's `pages/` directory
    badge_page       the page descriptor the config UI should offer

Anything under the frame's own group names is merged; an extension may also add its
own top-level group, which the badge draws by name.
"""

import sys

if sys.version_info >= (3, 10):
    from importlib.metadata import entry_points
else:  # pragma: no cover
    from importlib_metadata import entry_points

GROUP = "statsbadge.sources"


def load(config=None):
    """Every installed extension that loads cleanly."""
    config = config or {}
    disabled = set(config.get("disabled_extensions", ()))
    loaded = []
    for entry in _entries():
        if entry.name in disabled:
            continue
        try:
            cls = entry.load()
        except Exception as exc:
            print(f"statsbadge: extension {entry.name!r} failed to import: {exc}",
                  file=sys.stderr)
            continue
        try:
            if not cls.available():
                continue
            source = cls(config.get("extensions", {}).get(entry.name, {}))
        except Exception as exc:
            print(f"statsbadge: extension {entry.name!r} failed to start: {exc}",
                  file=sys.stderr)
            continue
        source.name = getattr(source, "name", entry.name)
        loaded.append(source)
    return loaded


def _entries():
    try:
        found = entry_points()
        if hasattr(found, "select"):
            return list(found.select(group=GROUP))
        return list(found.get(GROUP, []))
    except Exception:
        return []


def badge_modules(sources):
    """The badge-side files the installer should push, as (name, path) pairs."""
    modules = []
    for source in sources:
        path = getattr(source, "badge_module", None)
        if path:
            modules.append((getattr(source, "name", "ext"), path))
    return modules


def badge_pages(sources):
    """Page descriptors contributed by extensions, for the config UI to offer."""
    pages = []
    for source in sources:
        page = getattr(source, "badge_page", None)
        if page:
            entry = dict(page)
            entry.setdefault("id", getattr(source, "name", "ext"))
            entry.setdefault("from_extension", getattr(source, "name", "ext"))
            pages.append(entry)
    return pages
