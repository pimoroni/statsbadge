"""Where extensions live: a directory beside the config, put on sys.path at startup."""

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
    """Return what a generation is compatible with: this Python, on this architecture."""
    return f"{sys.implementation.cache_tag}-{platform.machine() or 'any'}"


def root(config_dir):
    return os.path.join(config_dir, LIB)


def generations(config_dir):
    """Return finished generations for this tag, oldest first."""
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
    """Put the live generation on sys.path, returning it or None."""
    where = current(config_dir)
    inside = os.path.normpath(root(config_dir)) + os.sep
    # Any generation a build replaced is removed first. Left where it is, earlier in
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
    """Return uv, on the PATH or where its installer leaves it."""
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
              # A bundle puts the binaries beside the packages, which is nowhere uv's
              # own finder looks.
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
    """Return whether pip travelled in the app."""
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def tool():
    """Return (which one, the argv up to its verb) for what can install here, or None."""
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
    """Return what installs into a target directory, or None where neither is available."""
    found = tool()
    if found is None:
        return None
    kind, argv = found
    argv = [*argv, "install"]
    if kind != "uv":
        return argv
    # uv installs into the environment it is pointed at, and picks none by itself. A
    # packaged app has no interpreter to point at, so uv is told the version instead.
    if bundled():
        return [*argv, "--python-version",
                f"{sys.version_info.major}.{sys.version_info.minor}"]
    return [*argv, "--python", sys.executable]


def outdated(config_dir, timeout=60):
    """Return what the library holds that has a newer release, as (entries, why)."""
    where = current(config_dir)
    if not where:
        return [], "no extension library has been built yet"
    found = tool()
    if found is None:
        return [], "neither uv nor pip is here to ask with"
    kind, argv = found
    # Uncached: an index page is cached for minutes, and a release made since is missed.
    argv = [*argv, "list", "--outdated", "--format", "json",
            "--no-cache" if kind == "uv" else "--no-cache-dir",
            "--target" if kind == "uv" else "--path", where]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return [], f"the index did not answer inside {timeout} seconds"
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"could not run {kind}: {exc}"
    if done.returncode != 0:
        said = (done.stderr or "").strip().splitlines()
        return [], f"{kind} said: {said[-1] if said else 'nothing, and failed anyway'}"
    try:
        listed = json.loads(done.stdout or "[]")
    except ValueError:
        return [], f"{kind} answered with something that is not JSON"
    return [{"name": entry.get("name", ""), "version": entry.get("version"),
             "latest": entry.get("latest_version")}
            for entry in listed if entry.get("name") and entry.get("latest_version")], None


def build(config_dir, requirements, verbose=False):
    """Install `requirements` into a new generation, returning (path, what went wrong)."""
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
    """Return every distribution in a generation, version by name."""
    found = {}
    for entry in os.listdir(where):
        if not entry.endswith(".dist-info"):
            continue
        name, _, version = entry[:-len(".dist-info")].rpartition("-")
        found[name.lower().replace("_", "-")] = version
    return found


def holds(where, short_name):
    """Return whether a generation carries that extension, by the name `ext add` takes."""
    return resolved(where, f"statsbadge-{short_name}") is not None


def elsewhere(config_dir, short_name):
    """Return where that extension is installed outside the library, or None."""
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
    """Return what version of `name` the installer put in the target, or None."""
    wanted = name.lower().replace("-", "_")
    for entry in os.listdir(target):
        if not entry.endswith(".dist-info"):
            continue
        found, _, version = entry[:-len(".dist-info")].rpartition("-")
        if found.lower().replace("-", "_") == wanted:
            return version
    return None


def _release(version):
    """Return the leading numbers, for comparing one version with another."""
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
    """Drop what the running environment already has at the same version."""
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
    """Remove bottom up, failing on rmdir rather than on the walk."""
    for below, _directories, _files in os.walk(target, topdown=False):
        if below == target:
            continue
        try:
            os.rmdir(below)
        except OSError:
            pass
