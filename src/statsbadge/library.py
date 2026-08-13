"""Where extensions live: a directory beside the config, put on sys.path at startup.

Installing into the environment statsbadge runs from means rebuilding that environment,
which only a `uv tool install` can do. A directory of its own works from a venv, a pipx
install or a checkout alike, survives an upgrade of statsbadge itself, and is the only
option for a packaged app, where nothing may be written inside a signed bundle.

The whole list is installed at once. `extensions.txt` stays the record, and a removal is
a rebuild without that line.

A build goes into `<tag>-<n>.partial` and is renamed into place, which keeps a
half-finished install from ever being picked up. Nothing is written over: an imported
.pyd cannot be replaced on Windows, and the generation holding it is swept at the next
start instead, before anything has imported from it.
"""

import csv
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys

from . import NO_WINDOW, PIP_VERB, bundled

LIB = "lib"
PARTIAL = ".partial"


def tag():
    """What a generation is compatible with: this Python, on this architecture.

    A wheel with compiled code is built for both, so a generation belongs to one Python
    on one architecture. After an upgrade the name no longer matches and the directory
    goes unread, where reading it would fail at import.
    """
    return f"{sys.implementation.cache_tag}-{platform.machine() or 'any'}"


def root(config_dir):
    return os.path.join(config_dir, LIB)


def generations(config_dir):
    """Finished generations for this tag, oldest first."""
    prefix = f"{tag()}-"
    try:
        found = os.listdir(root(config_dir))
    except OSError:
        return []
    return sorted(name for name in found
                  if name.startswith(prefix) and name[len(prefix):].isdigit())


def current(config_dir):
    found = generations(config_dir)
    return os.path.join(root(config_dir), found[-1]) if found else None


def activate(config_dir):
    """Put the live generation on sys.path. Returns it, or None.

    Appended, which leaves anything the environment already has winning the import. A
    generation is pruned of duplicates, but an older one need not be.

    Code already imported stays imported: this settles what a later import finds, so a
    newer release of something already running reaches the process at the next start.
    """
    where = current(config_dir)
    inside = os.path.normpath(root(config_dir)) + os.sep
    # Any generation a build replaced comes off first. Left where it is, earlier in
    # sys.path, it would go on answering the import.
    for entry in list(sys.path):
        if entry != where and (os.path.normpath(entry) + os.sep).startswith(inside):
            sys.path.remove(entry)
    if where and where not in sys.path:
        sys.path.append(where)
    importlib.invalidate_caches()
    return where


def sweep(config_dir):
    """Drop what the live generation replaced. Before anything has imported from it."""
    keep = os.path.basename(current(config_dir) or "")
    prefix = f"{tag()}-"
    try:
        found = os.listdir(root(config_dir))
    except OSError:
        return []
    dropped = []
    for name in found:
        if name == keep or not (name.startswith(prefix) or name.endswith(PARTIAL)):
            continue
        shutil.rmtree(os.path.join(root(config_dir), name), ignore_errors=True)
        dropped.append(name)
    return dropped


def _uv():
    """uv, on the PATH or where its installer leaves it.

    A tray started at login carries the PATH it was given then, and a uv tool environment
    has no pip behind it. Between them the Extensions tab went quiet on a machine that
    plainly had uv.
    """
    found = shutil.which("uv")
    if found:
        return found
    try:
        import uv
        found = uv.find_uv_bin()
        if found and os.path.isfile(found):
            return found
    except (ImportError, FileNotFoundError):
        pass
    name = "uv.exe" if os.name == "nt" else "uv"
    places = [os.path.join(os.path.expanduser("~"), ".local", "bin"),
              os.path.dirname(sys.executable or ""),
              # A bundle puts the binaries beside the packages, which is nowhere uv's own
              # finder looks: sys.prefix there is the Python it was built with.
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "bin")]
    if os.name == "nt":
        places.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "uv", "bin"))
    for place in places:
        candidate = os.path.join(place, name)
        if place and os.path.isfile(candidate):
            return candidate
    return None


def _packaged_pip():
    """Whether pip travelled in the app."""
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def tool():
    """(which one, the argv up to its verb) for what can install here, or None.

    uv first, since a uv-made virtualenv usually carries no pip at all.
    """
    found = _uv()
    if found:
        return "uv", [found, "pip"]
    # A packaged app's executable is the app, so `-m pip` there starts a second copy of
    # it. It spawns itself as pip instead, which is the one thing in the bundle that can
    # be an interpreter.
    if bundled():
        return ("pip", [sys.executable, PIP_VERB]) if _packaged_pip() else None
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"],
                       capture_output=True, check=True, **NO_WINDOW)
    except (OSError, subprocess.CalledProcessError):
        return None
    return "pip", [sys.executable, "-m", "pip"]


def installer():
    """What installs into a target directory, or None where neither is available."""
    found = tool()
    if found is None:
        return None
    kind, argv = found
    argv = [*argv, "install"]
    if kind != "uv":
        return argv
    # uv installs into the environment it is pointed at, and picks none by itself. A
    # packaged app has no interpreter to point at - briefcase ships the library and not
    # the binary - so uv is told the version instead, and resolves for this machine.
    if bundled():
        return [*argv, "--python-version",
                f"{sys.version_info.major}.{sys.version_info.minor}"]
    return [*argv, "--python", sys.executable]


def outdated(config_dir, timeout=60):
    """What the library holds that has a newer release, as name, version and latest.

    This asks an index, over the network, and can take a moment. Anything that cannot be
    told comes back empty: not knowing is a different thing from up to date, and the
    caller has nothing to show either way.
    """
    where = current(config_dir)
    found = tool()
    if not where or found is None:
        return []
    kind, argv = found
    argv = [*argv, "list", "--outdated", "--format", "json",
            "--target" if kind == "uv" else "--path", where]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    try:
        listed = json.loads(done.stdout or "[]")
    except ValueError:
        return []
    return [{"name": entry.get("name", ""), "version": entry.get("version"),
             "latest": entry.get("latest_version")}
            for entry in listed if entry.get("name") and entry.get("latest_version")]


def build(config_dir, requirements, verbose=False):
    """Install `requirements` into a new generation. Returns (path, what went wrong).

    An empty list still builds one. Removing the last extension has to take it out of
    the running environment at the next start.
    """
    argv = installer()
    if argv is None:
        return None, "there is no uv and no pip here to install with"

    here = root(config_dir)
    os.makedirs(here, exist_ok=True)
    found = generations(config_dir)
    number = int(found[-1].rsplit("-", 1)[1]) + 1 if found else 1
    final = os.path.join(here, f"{tag()}-{number:04d}")
    target = final + PARTIAL
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)

    if requirements:
        argv += ["--target", target, *requirements]
        if not verbose:
            argv.append("--quiet")
        try:
            done = subprocess.run(argv, capture_output=not verbose, text=True,
                                  encoding="utf-8", errors="replace", check=False,
                                  **NO_WINDOW)
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            return None, f"could not run the installer: {exc}"
        if done.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            return None, (done.stderr or "").strip() or "the installer would not say why"
        wanted_host = resolved(target, "statsbadge")
        running = importlib.metadata.version("statsbadge")
        if wanted_host and _release(wanted_host) > _release(running):
            shutil.rmtree(target, ignore_errors=True)
            return None, (f"one of these needs statsbadge {wanted_host}, and this is "
                          f"{running}. Upgrade statsbadge itself first.")
        prune(target, ignore=here)

    os.rename(target, final)
    return final, None


def installed(where):
    """Every distribution a generation carries, version by name."""
    found = {}
    for entry in os.listdir(where):
        if not entry.endswith(".dist-info"):
            continue
        name, _, version = entry[:-len(".dist-info")].rpartition("-")
        found[name.lower().replace("_", "-")] = version
    return found


def holds(where, short_name):
    """Whether a generation carries that extension, by the name `ext add` takes."""
    return resolved(where, f"statsbadge-{short_name}") is not None


def elsewhere(config_dir, short_name):
    """Where that extension is installed outside the library, or None.

    A build writes the library alone, so a copy in the environment survives one and goes
    on answering the import. An editable install is the usual way to get one.
    """
    inside = os.path.normpath(root(config_dir)) + os.sep
    wanted = {f"statsbadge-{short_name}".lower(), short_name.lower()}
    for distribution in importlib.metadata.distributions():
        name = (distribution.metadata["Name"] or "").lower().replace("_", "-")
        if name not in wanted:
            continue
        try:
            where = os.path.normpath(os.fspath(distribution.locate_file("")))
        except (TypeError, ValueError):
            continue
        if not (where + os.sep).startswith(inside):
            return where
    return None


def resolved(target, name):
    """What version of `name` the installer put in the target, or None.

    The name is split off before it is normalised: a dist-info separates name from version
    with the same hyphen that a name spells as an underscore, so normalising the whole
    stem made `statsbadge_clock-1.2.0` match nothing at all.
    """
    wanted = name.lower().replace("-", "_")
    for entry in os.listdir(target):
        if not entry.endswith(".dist-info"):
            continue
        found, _, version = entry[:-len(".dist-info")].rpartition("-")
        if found.lower().replace("-", "_") == wanted:
            return version
    return None


def _release(version):
    """The leading numbers, for comparing one version with another.

    Enough to tell 2.0 from 1.3.3, which is the question here. A dev build of the running
    package reads as its release, keeping a checkout from looking older than what it built.
    """
    numbers = []
    for chunk in version.split(".")[:3]:
        digits = ""
        for character in chunk:
            if not character.isdigit():
                break
            digits += character
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers)


def prune(target, ignore=None):
    """Drop what the running environment already has at the same version.

    `--target` resolves against an empty directory, so an extension asking only for
    statsbadge drags in a second copy of it, and of Pillow and psutil behind it. Same
    version means the copy is redundant; a different one is left alone, since the
    extension asked for something this environment does not have.

    `ignore` is the library itself. The generation being replaced is already on sys.path,
    and counting it would prune every extension out of the one replacing it.
    """
    ignore = os.path.normpath(ignore) + os.sep if ignore else None
    have = {}
    for distribution in importlib.metadata.distributions():
        name = (distribution.metadata["Name"] or "").lower().replace("-", "_")
        if not name:
            continue
        if ignore:
            try:
                where = os.path.normpath(os.fspath(distribution.locate_file("")))
            except (TypeError, ValueError):
                where = ""
            if (where + os.sep).startswith(ignore):
                continue
        have[name] = distribution.version

    dropped = []
    for entry in sorted(os.listdir(target)):
        if not entry.endswith(".dist-info"):
            continue
        name, _, version = entry[:-len(".dist-info")].rpartition("-")
        key = name.lower().replace("-", "_")
        # statsbadge whatever the version: an extension runs inside the one that loaded
        # it, so a second copy is never the one being used.
        if key != "statsbadge" and have.get(key) != version:
            continue
        _remove_recorded(target, os.path.join(target, entry))
        dropped.append(name)
    _remove_empty(target)
    return dropped


def _remove_recorded(target, dist_info):
    """Take away every file a distribution's RECORD names."""  # noqa: D401
    inside = os.path.normpath(target) + os.sep
    try:
        with open(os.path.join(dist_info, "RECORD"), newline="", encoding="utf-8") as handle:
            paths = [row[0] for row in csv.reader(handle) if row]
    except OSError:
        paths = []
    for relative in paths:
        where = os.path.normpath(os.path.join(target, relative))
        # RECORD can name a script outside the tree, which belongs to nothing here.
        if not where.startswith(inside):
            continue
        try:
            os.remove(where)
        except OSError:
            pass
    shutil.rmtree(dist_info, ignore_errors=True)


def _remove_empty(target):
    """Bottom up, and on rmdir failing rather than on the walk.

    os.walk lists a directory's children once. A child taken away during the walk is
    still counted, leaving the parent looking occupied.
    """
    for below, _directories, _files in os.walk(target, topdown=False):
        if below == target:
            continue
        try:
            os.rmdir(below)
        except OSError:
            pass
