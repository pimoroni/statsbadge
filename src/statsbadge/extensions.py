"""Extensions: a pip install away from a new page.

An extension is a normal package advertising a `statsbadge.sources` entry point. It
contributes a source (so it can put anything in the frame) and, optionally, badge-side
Python that the server pushes to the badge so a page can animate at 45fps instead of
being a picture fetched over the wire.

    [project.entry-points."statsbadge.sources"]
    weather = "statsbadge_weather:Weather"

The class is a `sources.base.Source` with two extras:

    badge_module     path to a .py to install into the app's `pages/` directory
    badge_assets     paths to further files the badge side needs, an .af icon font say
    badge_page       the page descriptor the config UI should offer

Anything under the frame's own group names is merged; an extension may also add its
own top-level group, which the badge draws by name.
"""

import os
import sys
from importlib.metadata import entry_points

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


def describe():
    """Every discovered extension and how far it got, whether or not it loaded.

    A pip install that did not take is otherwise invisible until a page fails to turn
    up, so this reports the failure instead of skipping it the way load() does.
    """
    found = []
    for entry in _entries():
        record = {"name": entry.name, "version": _version(entry), "loaded": False,
                  "available": None, "provides": [], "badge_module": None,
                  "error": None}
        try:
            cls = entry.load()
        except Exception as exc:  # noqa: BLE001  any import failure is worth reporting
            record["error"] = f"{type(exc).__name__}: {exc}"
            found.append(record)
            continue
        record["loaded"] = True
        record["provides"] = list(getattr(cls, "provides", ()) or ())
        module = getattr(cls, "badge_module", None)
        record["badge_module"] = os.path.basename(module) if module else None
        try:
            record["available"] = bool(cls.available())
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"available() raised {type(exc).__name__}: {exc}"
        found.append(record)
    return sorted(found, key=lambda record: record["name"])


def _version(entry):
    distribution = getattr(entry, "dist", None)
    return getattr(distribution, "version", None) if distribution else None


def badge_modules(sources):
    """The badge-side files the installer should push, as (name, path) pairs.

    Modules and their assets together: the installer copies both into the app's ext/
    directory, and load_extensions() imports only the .py it finds there.
    """
    files = []
    for source in sources:
        name = getattr(source, "name", "ext")
        path = getattr(source, "badge_module", None)
        if path:
            files.append((name, path))
        for asset in getattr(source, "badge_assets", ()) or ():
            files.append((name, asset))
    return files


def settings_schema(sources):
    """What each loaded extension can be told, keyed by extension name.

    The config UI builds its fields from this, so an extension that declares nothing
    gets no section and cannot be configured from the browser.
    """
    schema = {}
    for source in sources:
        declared = getattr(source, "settings", ()) or ()
        if declared:
            schema[getattr(source, "name", "ext")] = [dict(entry) for entry in declared]
    return schema


def configure(sources, settings):
    """Hand each source its own block of stored settings.

    A source that raises is recorded and left alone: one extension refusing a setting
    must not stop the others taking theirs.
    """
    for source in sources:
        block = (settings or {}).get(getattr(source, "name", ""), {})
        if not block:
            continue
        try:
            source.configure(block)
        except Exception as exc:  # noqa: BLE001  a bad setting is the source's problem
            source.note_fault(exc)


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
