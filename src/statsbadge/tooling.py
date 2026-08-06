"""Managing extensions when statsbadge is installed as a uv tool.

`uv tool install` is declarative: each run replaces the last, so adding a second extension
means naming the first one again or losing it. That is a list worth keeping somewhere, so it
lives in the config directory as `extensions.txt` and every install is made from it.

The base requirement - `statsbadge`, or `statsbadge[nvidia]`, or a path for a checkout - comes
from uv's own receipt, which sits beside the tool's environment and records what it was built
from. Reading that rather than guessing is what keeps an extra from being dropped on the next
add.

uv has no `pipx inject`, so `uv tool install --with-requirements` is the way in. Its own
progress and its resolver's prose are not this command's output: uv runs quiet, one line is
printed here, and `--verbose` hands the terminal back to uv for when the reason matters.

`uv pip install --python <the tool environment>` would put a package in without a rebuild, and
is not used: nothing would record it, so the next `uv tool upgrade` would drop it again.
"""

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

# uv writes this beside the environment of every tool it installs, so finding one next to the
# running interpreter is what tells us this is a tool and not a venv or a checkout.
RECEIPT = "uv-receipt.toml"
# The extensions wanted on this host, one requirement a line, in the config directory: hand
# editable, and the thing every reinstall is made from.
WANTED = "extensions.txt"
# What a plugin is called if it is named by its short name.
PREFIX = "statsbadge-"
# Anything with one of these in it is already a requirement, a path or a URL, so it is passed
# through as it stands.
SPEC_MARKS = "/\\=<>@[]!~;: "


def receipt_path(prefix=None):
    return os.path.join(prefix or sys.prefix, RECEIPT)


def as_uv_tool(prefix=None):
    """uv's receipt for this interpreter, or None if statsbadge is not installed as a tool."""
    path = receipt_path(prefix)
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return None


def base_requirement(receipt, name="statsbadge"):
    """What to reinstall the tool itself from, extras and all, or None if it cannot be told.

    A receipt records a requirement per package: the tool and everything installed beside it.
    Only the tool's own entry is wanted, and it may be a registry name, a directory or a URL -
    so anything not recognised returns None and the caller offers the command instead of
    running a guess.
    """
    for requirement in (receipt or {}).get("tool", {}).get("requirements", ()):
        if requirement.get("name") != name:
            continue
        extras = requirement.get("extras") or ()
        marked = f"{name}[{','.join(extras)}]" if extras else name
        if requirement.get("directory"):
            # A path install keeps the path, or the reinstall would take the published package
            # in place of the checkout it came from.
            where = requirement["directory"]
            return f"{where}[{','.join(extras)}]" if extras else where
        if requirement.get("url"):
            return requirement["url"]
        if requirement.get("specifier"):
            return marked + requirement["specifier"]
        return marked
    return None


def installed_beside(receipt, name="statsbadge"):
    """Every requirement in the receipt other than the tool itself, as uv recorded them."""
    out = []
    for requirement in (receipt or {}).get("tool", {}).get("requirements", ()):
        if requirement.get("name") == name:
            continue
        out.append(requirement.get("directory") or requirement.get("url")
                   or requirement.get("name", ""))
    return [entry for entry in out if entry]


def wanted_path(config_dir):
    return os.path.join(config_dir, WANTED)


def read_wanted(config_dir):
    """The extensions this host asks for, in the order they were added."""
    try:
        with open(wanted_path(config_dir)) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def write_wanted(config_dir, requirements):
    os.makedirs(config_dir, exist_ok=True)
    with open(wanted_path(config_dir), "w") as handle:
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
    """`clock` as `statsbadge-clock`, and anything already specific left alone."""
    name = name.strip()
    if name.startswith(PREFIX) or any(mark in name for mark in SPEC_MARKS):
        return name
    if name.startswith(".") or name.endswith((".whl", ".tar.gz")):
        return name
    return PREFIX + name


def names(requirements):
    """The short name of each, for asking whether one is already there.

    By short name and not by the string: the same extension can be named as `clock`, as
    `statsbadge-clock` or as a path to it, and a list holding two spellings of one plugin asks
    uv to install it twice.
    """
    return {short_name(requirement) for requirement in requirements}


def short_name(requirement):
    """What `ext add` would have been given for this requirement, for reporting."""
    tail = requirement.rstrip("/").replace("\\", "/").split("/")[-1]
    for mark in "[=<>@!~;":
        tail = tail.split(mark)[0]
    return tail[len(PREFIX):] if tail.startswith(PREFIX) else tail


def install_argv(base, config_dir, fresh=False):
    """The command that makes the tool environment match `extensions.txt`.

    --force because the tool is already there and this is a replacement, which is the only
    thing uv tool install does: there is no adding to an existing one.

    --fresh, and so --reinstall, for taking something out. Whether --force alone prunes depends
    on the uv doing the work: measured against a tool holding two extensions, uv 0.9.2 drops the
    package from site-packages and uv 0.4.28 writes the shorter receipt and leaves it there, with
    its entry point still registering a page. Nothing here chooses which uv a user has, and a
    removal that does not remove is worse than a slow one. Adding needs neither.
    """
    argv = [shutil.which("uv") or "uv", "tool", "install", "--force"]
    if fresh:
        argv.append("--reinstall")
    argv.append(base)
    if read_wanted(config_dir):
        argv += ["--with-requirements", wanted_path(config_dir)]
    return argv


def unpinned(base):
    """`statsbadge==1.0.0` as `statsbadge`, or None where there is no pin to drop.

    Only a registry requirement: a path install resolves to whatever is in the checkout, so
    there is nothing to relax, and a URL is already exact on purpose.
    """
    if not base or any(mark in base for mark in "/\\"):
        return None
    loosened = re.split(r"[=<>!~]", base, maxsplit=1)[0].strip()
    return loosened if loosened and loosened != base else None


def installed_version(name="statsbadge", prefix=None):
    """What version of `name` is in the environment on disk, or None.

    Read from the environment rather than from this process, because the point of asking is
    to notice that a rebuild moved it: `uv tool install` resolves the whole environment at
    once, so an extension asking for a newer statsbadge takes the tool with it.
    """
    site = importlib.metadata.MetadataPathFinder()
    context = importlib.metadata.DistributionFinder.Context(
        name=name, path=[p for p in sys.path if (prefix or sys.prefix) in p])
    for distribution in site.find_distributions(context):
        return distribution.version
    return None


def quoted(argv):
    """The command as a line someone can paste."""
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


# Where a plain name can be checked before anything is installed, so a name that is not a
# package is answered as one rather than as a failed rebuild.
SIMPLE_INDEX = "https://pypi.org/simple/{}/"


def on_index(requirement, timeout=4.0):
    """Whether a plain name is a project on PyPI, or None when it cannot be told.

    Only asked of a bare name: a path, a URL or a version specifier is something uv resolves
    its own way. An index that cannot be reached is not an answer either, so both come back
    None and the caller carries on and lets uv decide.
    """
    if requirement.startswith(".") or any(mark in requirement for mark in SPEC_MARKS):
        return None
    try:
        request = urllib.request.Request(SIMPLE_INDEX.format(requirement), method="HEAD")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (OSError, ValueError):
        # No network, a proxy in the way, a mangled name: none of those is an answer either.
        return None


def blamed(said):
    """The package that could not be installed, out of uv's own words or out of `explain`'s.

    Both, because the caller has the explained line and not the original: `explain` is what
    turns a resolver's paragraph into something worth printing, and the name survives it.
    """
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
    """One line, since uv wraps its prose to the terminal and a phrase spans the fold."""
    return " ".join((said or "").split())


# uv's answer when a name is not a package. Its resolver explains itself at length, and the
# useful part is the name.
MISSING = re.compile(r"Because (\S+) was not found in the package registry")

# And its answer when an extension wants a statsbadge this tool cannot have, which is what
# a plugin built against a newer host looks like from here. Three things are worth keeping:
# which extension, what it asks for, and what this tool is pinned to.
CONFLICT = re.compile(
    r"Because (?:all versions of )?(\S+) depends? on (\S+) and you require (\S+?)[,\s]")


def run_install(base, config_dir, fresh=False, verbose=False):
    """Replace the tool environment. Returns (ok, what went wrong).

    Whatever this process was going to do afterwards, it should not: the environment it is
    running out of has just been rebuilt underneath it.
    """
    argv = install_argv(base, config_dir, fresh)
    if verbose:
        print(f"  {quoted(argv)}")
    # uv writes to the same terminal, and what is already printed should be on it first.
    sys.stdout.flush()
    if verbose:
        try:
            return subprocess.run(argv, check=False).returncode == 0, ""
        except OSError as exc:
            return False, f"could not run uv: {exc}"
    try:
        done = subprocess.run([*argv, "--quiet"], capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"could not run uv: {exc}"
    return done.returncode == 0, explain(done.stderr)


def explain(said):
    """uv's complaint as one line, or the name of whatever does not exist."""
    said = (said or "").strip()
    flat = _collapsed(said)
    missing = MISSING.search(flat)
    if missing:
        return f"no such package: {missing.group(1)}"
    # An extension built against a newer statsbadge than this tool is pinned to. Worth
    # translating rather than passing on: uv's own last line is "your requirements are
    # unsatisfiable", which is true of every resolution failure and says nothing about the
    # versions, and the versions are the whole of it.
    clash = CONFLICT.search(flat)
    if clash:
        wants, needs, held = clash.groups()
        return f"{wants} needs {needs}, and this tool is installed as {held}"
    for line in said.splitlines():
        if line.startswith("error: "):
            return line[len("error: "):]
    return said.splitlines()[-1] if said else "uv did not say why"
