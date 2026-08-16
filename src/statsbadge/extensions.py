"""Extensions: a pip install away from a new page.

An extension is a normal package advertising a `statsbadge.sources` entry point. It
contributes a source, and optionally badge-side Python that the server pushes across, so
a page can animate at 45fps instead of being a picture fetched over the wire.

    [project.entry-points."statsbadge.sources"]
    weather = "statsbadge_weather:Weather"

The class is a `sources.base.Source` with two extras:

    badge_module     path to a .py to install into the app's `pages/` directory
    badge_assets     paths to further files the badge side needs, an .af icon font say
    badge_page       the page descriptor the config UI should offer

Anything under a group the frame already names is merged; an extension may also add a
top-level group.
"""

import os
import sys
from importlib.metadata import entry_points

from . import state

GROUP = "statsbadge.sources"


def load(config=None, state_dir=None):
    """Every installed extension that loads cleanly.

    `state_dir` is where each one's store is kept, one file per extension named after it.
    Without one they get a store that keeps what they learn in memory, which suits a
    one-shot load: `install` builds these only to ask what badge modules they ship.
    """
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
        # Namespaced by the entry point name rather than by whatever the class calls itself:
        # the entry point is what pip installed and what --without names, so it is the one
        # thing that cannot collide with another extension's.
        source.store = state.for_source(state_dir, entry.name)
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


def describe(disabled=()):
    """Every discovered extension and how far it got, whether or not it loaded.

    A pip install that did not take is otherwise invisible until a page fails to turn
    up, so this reports the failure instead of skipping it the way load() does.
    """
    found = []
    for entry in _entries():
        record = {"name": entry.name, "version": _version(entry), "loaded": False,
                  "available": None, "provides": [], "badge_module": None,
                  "error": None, "disabled": entry.name in disabled}
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


def catalogue():
    """The published extensions, from catalogue.toml, in the order it names them."""
    import tomllib
    from importlib import resources
    text = resources.files(__package__).joinpath("catalogue.toml").read_text(encoding="utf-8")
    return [{"name": name, "title": entry.get("title") or name,
             "summary": entry.get("summary", ""),
             "page": bool(entry.get("page")), "needs": entry.get("needs")}
            for name, entry in tomllib.loads(text).items()]


def offered(installed=None, wanted=(), disabled=()):
    """The catalogue with each entry's state, then anything installed it does not name.

    `wanted` is `extensions.txt`, so one asked for but absent reads as adrift rather than
    as never having been asked for.
    """
    from . import tooling
    present = {record["name"]: record for record in
               (describe() if installed is None else installed)}
    # extensions.txt holds requirements; a plugin is known here by its short name.
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
    # Whatever else is installed, so a third-party one is still listed and removable.
    for name, found in sorted(present.items()):
        listed.append({"name": name, "title": name, "summary": "",
                       "page": bool(found.get("badge_module")),
                       "needs": None, "installed": True, "asked": name in asked,
                       "disabled": name in disabled,
                       "managed": found.get("managed", True),
                       "version": found.get("version"), "error": found.get("error")})
    return listed


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


def model_groups(sources):
    """Every frame group the loaded extensions declare, keyed by group name.

    Read off each source and not its class, so one that discovers its groups - a domain
    per site an account holds - has them offered as soon as they are set. A later
    source declaring a group an earlier one already has adds its fields to it.
    """
    declared = {}
    for source in sources:
        for name, group in (getattr(source, "groups", None) or {}).items():
            into = declared.setdefault(name, {"label": name, "fields": {}})
            # Everything the group says about itself, `fields` apart: that one is merged so
            # two sources can each contribute to a group.
            for key, value in group.items():
                if key != "fields" and value is not None:
                    into[key] = value
            into["fields"].update(group.get("fields") or {})
    return declared


def group_owners(sources):
    """Which source each declared group came from, by the name the UI should head it with.

    A picker groups the sources it offers by whoever provides them, and the frame is flat:
    `cf_pinout_xyz` says nothing about being Cloudflare's. Only the groups an extension
    declared are in here, so anything missing is the host measuring itself.
    """
    owners = {}
    for source in sources:
        name = getattr(source, "name", "ext")
        label = getattr(source, "label", None) or name.replace("_", " ").title()
        for group in (getattr(source, "groups", None) or {}):
            owners[group] = label
    return owners


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


def page_settings_schema(sources):
    """What an extension's pages can be told, keyed by page kind.

    Keyed by kind and not by extension, since the config UI is editing a page and a page
    carries its kind. An extension contributing two kinds can declare settings once and
    have both carry them.
    """
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
    """Hand each source the configured pages of its own kinds.

    So a source can do per-page work - one weather lookup per place on the badge -
    without knowing anything about the layout beyond its own pages.
    """
    for source in sources:
        kinds = {page.get("kind") for page in badge_pages([source])}
        mine = [page for page in (pages or ()) if page.get("kind") in kinds]
        try:
            source.pages(mine)
        except Exception as exc:  # noqa: BLE001  one source must not stop the others
            print(f"statsbadge: extension {getattr(source, 'name', '?')!r} rejected its "
                  f"pages: {exc}", file=sys.stderr)


def configure(sources, settings):
    """Hand each source its own block of stored settings.

    A source that raises is recorded and left alone: one extension refusing a setting
    must not stop the others taking theirs.

    An empty block is skipped, not passed on. `Source.configure` merges, so there would be
    nothing in it to apply, and every extension here zeroes its fetch timers when told, so
    calling it would refetch on a save that changed something else. A field is cleared by
    arriving as null, not by being left out.
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
