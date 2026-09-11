"""Extensions: a pip install away from a new page."""

import os
import sys
from importlib.metadata import entry_points

from . import geocode, state

GROUP = "statsbadge.sources"


def load(config=None, state_dir=None, geocoder=None):
    """Return every installed extension that loads cleanly."""
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
        # Namespaced by the entry point name rather than by whatever the class calls
        # itself: the entry point is what pip installed and what --without names.
        source.store = state.for_source(state_dir, entry.name)
        if geocoder is not None:
            source.geocode = geocoder
        loaded.append(source)
    return loaded


def set_home(sources, config):
    """Hand every source the badge-wide location, at startup and on every change to it."""
    home = geocode.home_from(config)
    for source in sources:
        source.home = home


def _entries():
    try:
        found = entry_points()
        if hasattr(found, "select"):
            return list(found.select(group=GROUP))
        return list(found.get(GROUP, []))
    except Exception:
        return []


def describe(disabled=()):
    """Return every discovered extension and how far it got, whether or not it loaded."""
    found = []
    for entry in _entries():
        record = {"name": entry.name, "version": _version(entry), "loaded": False,
                  "available": None, "provides": [], "badge_module": None,
                  "error": None, "disabled": entry.name in disabled}
        try:
            cls = entry.load()
        except Exception as exc:  # noqa: BLE001
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


def versions():
    """Return the distribution version behind each entry point, by entry point name."""
    return {entry.name: _version(entry) for entry in _entries()}


def forget(names):
    """Drop these extensions' modules, so the next import reads what is on disk now."""
    roots = {entry.name: _root(entry) for entry in _entries()}
    dropped = []
    for name in names:
        root = roots.get(name)
        if not root:
            continue
        for module in [held for held in sys.modules
                       if held == root or held.startswith(root + ".")]:
            del sys.modules[module]
        dropped.append(name)
    return dropped


def _root(entry):
    """Return the top-level package an entry point names, or "" where it names nothing."""
    named = getattr(entry, "module", None) or str(getattr(entry, "value", "")).split(":")[0]
    return named.split(".")[0]


def _version(entry):
    distribution = getattr(entry, "dist", None)
    return getattr(distribution, "version", None) if distribution else None


def catalogue():
    """Return the published extensions, from catalogue.toml, in the order it names them."""
    import tomllib
    from importlib import resources
    text = resources.files(__package__).joinpath("catalogue.toml").read_text(encoding="utf-8")
    return [{"name": name, "title": entry.get("title") or name,
             "summary": entry.get("summary", ""),
             "page": bool(entry.get("page")), "needs": entry.get("needs")}
            for name, entry in tomllib.loads(text).items()]


def offered(installed=None, wanted=(), disabled=()):
    """Return the catalogue with each entry's state, then anything installed it does not name."""
    from . import tooling
    present = {record["name"]: record for record in
               (describe() if installed is None else installed)}
    # extensions.txt holds requirements; an extension is keyed here by its short name.
    asked = {tooling.short_name(requirement) for requirement in wanted}
    listed = []
    for entry in catalogue():
        found = present.pop(entry["name"], None)
        listed.append({**entry, "installed": found is not None,
                       "asked": entry["name"] in asked,
                       "disabled": entry["name"] in disabled,
                       "managed": (found or {}).get("managed", True),
                       "version": (found or {}).get("version"),
                       "error": (found or {}).get("error")})
    # Installed extensions the catalogue does not name, so a third-party extension is
    # still listed and removable.
    for name, found in sorted(present.items()):
        listed.append({"name": name, "title": name, "summary": "",
                       "page": bool(found.get("badge_module")),
                       "needs": None, "installed": True, "asked": name in asked,
                       "disabled": name in disabled,
                       "managed": found.get("managed", True),
                       "version": found.get("version"), "error": found.get("error")})
    return listed


def badge_modules(sources):
    """Return the badge-side files the installer should push, as (name, path) pairs."""
    files = []
    for source in sources:
        name = getattr(source, "name", "ext")
        path = getattr(source, "badge_module", None)
        if path:
            files.append((name, path))
        for asset in getattr(source, "badge_assets", ()) or ():
            files.append((name, asset))
    return files


def model_groups(sources):
    """Return every frame group the loaded extensions declare, keyed by group name."""
    declared = {}
    for source in sources:
        for name, group in (getattr(source, "groups", None) or {}).items():
            into = declared.setdefault(name, {"label": name, "fields": {}})
            # Everything the group declares, `fields` apart: that one is merged so two
            # sources can each contribute to a group.
            for key, value in group.items():
                if key != "fields" and value is not None:
                    into[key] = value
            into["fields"].update(group.get("fields") or {})
    return declared


def group_owners(sources):
    """Return which source each declared group came from, by the name to head it with."""
    owners = {}
    for source in sources:
        name = getattr(source, "name", "ext")
        label = getattr(source, "label", None) or name.replace("_", " ").title()
        for group in (getattr(source, "groups", None) or {}):
            owners[group] = label
    return owners


def settings_schema(sources):
    """Return what each loaded extension can be told, keyed by extension name."""
    schema = {}
    for source in sources:
        declared = getattr(source, "settings", ()) or ()
        if declared:
            schema[getattr(source, "name", "ext")] = [dict(entry) for entry in declared]
    return schema


def page_settings_schema(sources):
    """Return what an extension's pages can be told, keyed by page kind."""
    schema = {}
    for source in sources:
        declared = getattr(source, "page_settings", ()) or ()
        if not declared:
            continue
        for page in badge_pages([source]):
            kind = page.get("kind")
            if kind:
                schema[kind] = [dict(entry) for entry in declared]
    return schema


def configure_pages(sources, pages):
    """Hand each source the configured pages of its own kinds."""
    for source in sources:
        kinds = {page.get("kind") for page in badge_pages([source])}
        mine = [page for page in (pages or ()) if page.get("kind") in kinds]
        try:
            source.pages(mine)
        except Exception as exc:  # noqa: BLE001
            print(f"statsbadge: extension {getattr(source, 'name', '?')!r} rejected its "
                  f"pages: {exc}", file=sys.stderr)


def configure(sources, settings):
    """Hand each source its own block of stored settings."""
    for source in sources:
        block = (settings or {}).get(getattr(source, "name", ""), {})
        if not block:
            continue
        try:
            source.configure(block)
        except Exception as exc:  # noqa: BLE001
            source.note_fault(exc)


def recipes(sources):
    """Return ready-made pages contributed by extensions, for the config UI's Quick Add."""
    found = []
    for source in sources:
        name = getattr(source, "name", "ext")
        for recipe in getattr(source, "badge_recipes", ()) or ():
            entry = dict(recipe)
            entry["name"] = f"{name}.{entry.get('name') or 'pages'}"
            entry["from_extension"] = name
            # On the pages too, which is what `prune` reads to keep a page whose fields
            # are the extension's and absent from the model's list.
            entry["pages"] = [{"from_extension": name, **page}
                              for page in entry.get("pages") or ()]
            found.append(entry)
    return found


def badge_pages(sources):
    """Return page descriptors contributed by extensions, for the config UI to offer."""
    pages = []
    for source in sources:
        page = getattr(source, "badge_page", None)
        if page:
            entry = dict(page)
            entry.setdefault("id", getattr(source, "name", "ext"))
            entry.setdefault("from_extension", getattr(source, "name", "ext"))
            pages.append(entry)
    return pages
