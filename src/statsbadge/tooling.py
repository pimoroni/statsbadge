"""Which extensions this host asks for, and putting that list into effect."""

import os
import re
import urllib.error
import urllib.request

from . import library

# The extensions listed for this host, one requirement a line. Hand editable, and what
# every rebuild is made from.
WANTED = "extensions.txt"
# The ones to leave unloaded, by short name. Beside the list and not inside it: one the
# environment installed is never on the list.
DISABLED = "disabled.txt"
# What an extension is called when it is named by its short name.
PREFIX = "statsbadge-"
# Anything carrying one of these is already a requirement, a path or a URL.
SPEC_MARKS = "/\\=<>@[]!~;: "


def wanted_path(config_dir):
    return os.path.join(config_dir, WANTED)


def read_wanted(config_dir):
    """Return the extensions this host asks for, in the order they were added."""
    try:
        with open(wanted_path(config_dir), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def disabled_path(config_dir):
    return os.path.join(config_dir, DISABLED)


def read_disabled(config_dir):
    """Return the extensions switched off here, by short name."""
    try:
        with open(disabled_path(config_dir), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def write_disabled(config_dir, wanted):
    os.makedirs(config_dir, exist_ok=True)
    with open(disabled_path(config_dir), "w", encoding="utf-8") as handle:
        handle.write("# Extensions statsbadge leaves unloaded, one short name a line.\n")
        for name in sorted(set(wanted)):
            handle.write(f"{name}\n")


def switch(config_dir, names_asked, off):
    """Turn extensions off or on, returning the short names that changed."""
    was = set(read_disabled(config_dir))
    asked = {short_name(as_requirement(name)) for name in names_asked}
    now = (was | asked) if off else (was - asked)
    if now == was:
        return []
    write_disabled(config_dir, now)
    return sorted(asked & (now ^ was))


def adrift(config_dir, installed):
    """Return the extensions asked for in `extensions.txt` that are absent."""
    present = set(installed)
    return [r for r in read_wanted(config_dir) if short_name(r) not in present]


def write_wanted(config_dir, requirements):
    os.makedirs(config_dir, exist_ok=True)
    with open(wanted_path(config_dir), "w", encoding="utf-8") as handle:
        handle.write("# Extensions statsbadge is installed with, one requirement a line.\n")
        handle.write("# Edit by hand or with `statsbadge ext add`, then run `statsbadge ext"
                     " sync`.\n")
        for requirement in requirements:
            handle.write(f"{requirement}\n")


def forget_wanted(config_dir):
    """Take the list away again, for a first add that could not be installed."""
    try:
        os.remove(wanted_path(config_dir))
    except OSError:
        pass


def as_requirement(name):
    """Expand `clock` to `statsbadge-clock`, leaving anything already specific alone."""
    name = name.strip()
    if name.startswith(PREFIX) or any(mark in name for mark in SPEC_MARKS):
        return name
    if name.startswith(".") or name.endswith((".whl", ".tar.gz")):
        return name
    return PREFIX + name


def names(requirements):
    """Return the short name of each, for asking whether one is already there."""
    return {short_name(requirement) for requirement in requirements}


def short_name(requirement):
    """Return what `ext add` would have been given for this requirement, for reporting."""
    tail = requirement.rstrip("/").replace("\\", "/").split("/")[-1]
    for mark in "[=<>@!~;":
        tail = tail.split(mark)[0]
    return tail[len(PREFIX):] if tail.startswith(PREFIX) else tail


def plan(verb, asking, wanted, present):
    """Return what `extensions.txt` should become."""
    wanted = list(wanted)
    done = {"wanted": wanted, "changed": [], "recorded": [], "already": [],
            "restored": [], "absent": [], "unknown": None, "unpinned": []}
    if verb == "upgrade" and not asking:
        # Naming nothing means all of them.
        done["changed"] = list(wanted)
        return done
    for name in asking or ():
        requirement = as_requirement(name)
        short = short_name(requirement)
        if verb == "remove":
            matches = [r for r in wanted if r == requirement or short_name(r) == short]
            if not matches:
                done["absent"].append(short)
            for match in matches:
                wanted.remove(match)
                done["changed"].append(match)
            continue

        if verb == "upgrade":
            matches = [r for r in wanted if short_name(r) == short]
            if not matches:
                done["absent"].append(short)
            for match in matches:
                # Naming one is asking for it to move, so a version it was pinned to
                # stops being the answer. A bare `ext upgrade` leaves every pin alone.
                loose = without_pin(match)
                if loose != match:
                    wanted[wanted.index(match)] = loose
                    done["unpinned"].append(f"{short} was pinned to {match}")
                done["changed"].append(loose)
            continue

        listed = short in names(wanted)
        if short in present:
            # Here already: pip installed into a virtualenv, or an editable checkout.
            done["already"].append(short)
            if not listed:
                wanted.append(requirement)
                done["recorded"].append(requirement)
            continue
        if listed:
            # On the list but absent from the environment, which a `uv tool install` of
            # statsbadge itself leaves behind. Rebuild rather than report an install
            # nothing can see.
            done["restored"].append(short)
            done["changed"].append(requirement)
            continue
        # Asked of the index before anything is written or rebuilt. The rebuild installs
        # the whole list, so an unknown name comes back naming whichever entry uv
        # tripped over.
        if on_index(requirement) is False:
            done["unknown"] = requirement
            return done
        wanted.append(requirement)
        done["changed"].append(requirement)
    return done


def without_pin(requirement):
    """Reduce `statsbadge-clock==1.1.0` to `statsbadge-clock`, leaving a path as it is."""
    if any(mark in requirement for mark in "/\\ "):
        return requirement
    loosened = re.split(r"[=<>!~]", requirement, maxsplit=1)[0].strip()
    return loosened or requirement


def pinned(wanted):
    """Return the short names carrying a version, which a bare upgrade holds in place."""
    return {short_name(r) for r in wanted if without_pin(r) != r}


def holding(config_dir, wanted, moving):
    """Return the list to build so `moving` takes a newer release and the rest stay put."""
    where = library.current(config_dir)
    versions = {short_name(name): version
                for name, version in library.installed(where).items()} if where else {}
    held = []
    for requirement in wanted:
        short = short_name(requirement)
        version = versions.get(short)
        if short in moving or not version or any(m in requirement for m in SPEC_MARKS):
            held.append(requirement)
        else:
            held.append(f"{requirement}=={version}")
    return held


def apply(config_dir, verb, asking, present, verbose=False, announce=None):
    """Plan the change, write the list, then rebuild the environment."""
    before = read_wanted(config_dir)
    had_list = os.path.isfile(wanted_path(config_dir))

    done = plan(verb, asking, before, present)
    done.update({"ok": False, "why": None, "nothing": False, "stuck": [], "shadowed": []})
    if done["unknown"]:
        return done

    if verb == "remove" and done["changed"]:
        # Before the list is written, and before any build: a copy in the environment
        # survives both, so the list has to go on asking for what is still installed.
        done["stuck"] = _outside(config_dir, done["changed"])
        held = {entry["name"] for entry in done["stuck"]}
        if held:
            done["changed"] = [r for r in done["changed"] if short_name(r) not in held]
            going = names(done["changed"])
            done["wanted"] = [r for r in before if short_name(r) not in going]

    if verb != "sync" and not done["changed"]:
        # Nothing to build. Something already installed is only written down, and a
        # build would put a copy in the library that the environment's own answers over.
        if done["recorded"]:
            write_wanted(config_dir, done["wanted"])
            done["shadowed"] = _outside(config_dir, done["recorded"])
        done["ok"] = True
        done["nothing"] = not done["recorded"]
        return done

    write_wanted(config_dir, done["wanted"])
    if announce:
        announce(done["changed"] or done["wanted"])
    # The whole list every time, so the library matches what is asked for and a removal
    # is a build without that line.
    building = done["wanted"]
    if verb == "upgrade":
        building = holding(config_dir, done["wanted"],
                           names(done["changed"]))
    where, why = library.build(config_dir, building, verbose=verbose)
    if where is None:
        # Put the list back: it records what is installed, and the build failed.
        if had_list:
            write_wanted(config_dir, before)
        else:
            forget_wanted(config_dir)
        done["why"] = explain(why)
        return done

    library.activate(config_dir)
    done["ok"] = True
    if verb != "remove":
        done["shadowed"] = _outside(config_dir, done["changed"])
    return done


def _outside(config_dir, requirements):
    """Return which of these are in the environment, and where."""
    found = []
    for requirement in requirements:
        short = short_name(requirement)
        where = library.elsewhere(config_dir, short)
        if where:
            found.append({"name": short, "where": where})
    return sorted(found, key=lambda entry: entry["name"])


def quoted(argv):
    """Return the command as a line someone can paste."""
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


# Where a plain name is checked before anything is installed, so an unknown one is
# answered as unknown and not as a failed rebuild.
SIMPLE_INDEX = "https://pypi.org/simple/{}/"


def on_index(requirement, timeout=4.0):
    """Return whether a plain name is a project on PyPI, or None when it cannot be told."""
    if requirement.startswith(".") or any(mark in requirement for mark in SPEC_MARKS):
        return None
    try:
        request = urllib.request.Request(SIMPLE_INDEX.format(requirement), method="HEAD")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (OSError, ValueError):
        return None


def blamed(said):
    """Return the package that could not be installed, out of uv's words or `explain`'s."""
    said = said or ""
    for pattern in (r"no such package: (\S+)", r"^(\S+) needs "):
        summarised = re.search(pattern, said, re.M)
        if summarised:
            return summarised.group(1)
    for pattern in (MISSING, CONFLICT):
        found = pattern.search(_collapsed(said))
        if found:
            return found.group(1)
    return None


def _collapsed(said):
    """Fold to one line, since uv wraps its prose and a phrase spans the fold."""
    return " ".join((said or "").split())


# uv's answer when a name is not a package. The name is the useful part.
MISSING = re.compile(r"Because (\S+) was not found in the package registry")

# An extension built against a newer host. Three things are kept: which extension, what
# it needs, and what this tool is pinned to.
CONFLICT = re.compile(
    r"Because (?:all versions of )?(\S+) depends? on (\S+) and you require (\S+?)[,\s]")


def explain(said):
    """Return uv's complaint as one line, or the name of whatever does not exist."""
    said = (said or "").strip()
    flat = _collapsed(said)
    missing = MISSING.search(flat)
    if missing:
        return f"no such package: {missing.group(1)}"
    # Translated: uv ends with "your requirements are unsatisfiable", which is true of
    # every resolution failure and names no versions.
    clash = CONFLICT.search(flat)
    if clash:
        wants, needs, held = clash.groups()
        return f"{wants} needs {needs}, and this is {held}"
    for line in said.splitlines():
        if line.startswith("error: "):
            return line[len("error: "):]
    return said.splitlines()[-1] if said else "uv did not say why"
