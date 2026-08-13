"""Managing extensions when statsbadge is installed as a uv tool.

`uv tool install` is declarative, each run replacing the last, so the list of extensions
lives in the config directory as `extensions.txt` and every install is made from it.

The base requirement - `statsbadge`, `statsbadge[nvidia]`, or a path for a checkout -
comes from uv's receipt beside the tool environment, so an extra is not dropped on the
next add.

uv has no `pipx inject`, so `uv tool install --with-requirements` is the way in. It runs
quiet, one line is printed here, and `--verbose` hands the terminal back to it.

`uv pip install --python <the tool environment>` is not used: nothing would record the
package, and the next `uv tool upgrade` would drop it.
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

# uv writes this beside the environment of every tool it installs, so one next to the
# running interpreter means a tool install and not a venv or a checkout.
RECEIPT = "uv-receipt.toml"
# The extensions listed for this host, one requirement a line. Hand editable, and what
# every reinstall is made from.
WANTED = "extensions.txt"
# What a plugin is called if it is named by its short name.
PREFIX = "statsbadge-"
# Anything carrying one of these is already a requirement, a path or a URL.
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
    Only the tool's entry is read, and it may be a registry name, a directory or a URL -
    so anything not recognised returns None and the caller offers the command instead of
    running a guess.
    """
    for requirement in (receipt or {}).get("tool", {}).get("requirements", ()):
        if requirement.get("name") != name:
            continue
        extras = requirement.get("extras") or ()
        marked = f"{name}[{','.join(extras)}]" if extras else name
        if requirement.get("directory"):
            # A path install keeps its path, or a reinstall takes the published package instead.
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


def adrift(config_dir, installed):
    """The extensions asked for in `extensions.txt` that are not in the environment.

    `uv tool install` and `uv tool upgrade` replace the environment whole, extensions and all,
    and leave this list alone: what is asked for and what is there part company without either
    side being edited. `ext sync` rebuilds the environment from the list.
    """
    present = set(installed)
    return [r for r in read_wanted(config_dir) if short_name(r) not in present]


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


def plan(verb, asking, wanted, present):
    """What `extensions.txt` should become. The caller writes it and installs from it.

    `changed` has to be installed or removed; `recorded` is here already and only needs
    listing. The notes are left for the caller: one prints them, the other answers a
    request with them.
    """
    wanted = list(wanted)
    done = {"wanted": wanted, "changed": [], "recorded": [], "already": [],
            "restored": [], "absent": [], "unknown": None}
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

        listed = short in names(wanted)
        if short in present:
            # Here already: pip installed into a virtualenv, or an editable checkout.
            done["already"].append(short)
            if not listed:
                wanted.append(requirement)
                done["recorded"].append(requirement)
            continue
        if listed:
            # On the list but absent from the environment, which is what a `uv tool
            # install` of statsbadge itself leaves behind. Asking for it is asking for it
            # back, so rebuild instead of reporting an install nothing can see.
            done["restored"].append(short)
            done["changed"].append(requirement)
            continue
        # Asked of the index before anything is written or rebuilt. The rebuild installs
        # the whole list, so an unknown name comes back as a failure naming whichever
        # entry uv tripped over, which may well be a different one.
        if on_index(requirement) is False:
            done["unknown"] = requirement
            return done
        wanted.append(requirement)
        done["changed"].append(requirement)
    return done


def apply(config_dir, verb, asking, present, verbose=False, announce=None):
    """Plan the change, write the list, then rebuild the environment.

    Both callers take these steps in this order and report them differently, so what went
    wrong comes back for the caller to say. `announce` is called once with what is about
    to be installed, since there is a wait.
    """
    receipt = as_uv_tool()
    before = read_wanted(config_dir)
    had_list = os.path.isfile(wanted_path(config_dir))
    wanted = list(before)
    if not wanted and receipt:
        # Nothing written down yet, but uv records what the tool was built with, and
        # adopting that is the point. `ext add` on a tool installed with --with would
        # otherwise reinstall naming only the new one, dropping everything already there.
        wanted = installed_beside(receipt)

    done = plan(verb, asking, wanted, present)
    done.update({"ok": False, "why": None, "base": None, "moved": None, "nothing": False})
    if done["unknown"]:
        return done
    if verb != "sync" and not done["changed"] and not done["recorded"]:
        done["ok"] = done["nothing"] = True
        return done

    done["base"] = base_requirement(receipt) if receipt else None
    if done["base"] is None:
        # Not a uv tool, or a receipt this cannot read: nothing here builds an environment.
        # Listed anyway, for a later `ext sync` from a tool install.
        if done["recorded"]:
            write_wanted(config_dir, done["wanted"])
        # `sync` names nothing, so fall back to what is adrift.
        done["absent_here"] = done["changed"] or adrift(config_dir, present)
        done["nothing"] = not done["absent_here"]
        done["ok"] = done["nothing"]
        return done

    write_wanted(config_dir, done["wanted"])
    if announce:
        announce(done["changed"] or done["wanted"])
    # uv resolves the whole environment at once, so an extension asking for a newer
    # statsbadge takes the tool with it. That is the right answer and a quiet one.
    was = installed_version()
    done["ok"], done["why"] = run_install(done["base"], config_dir,
                                          fresh=verb != "add", verbose=verbose)
    if done["ok"]:
        # explain() reads uv's stderr, and a quiet success leaves it saying nothing useful.
        done["why"] = None
    else:
        # Put the list back: it records what is installed, and the rebuild failed.
        if had_list:
            write_wanted(config_dir, before)
        else:
            forget_wanted(config_dir)
        return done

    importlib.invalidate_caches()
    now = installed_version()
    if was and now and was != now:
        done["moved"] = (was, now)
    return done


def install_argv(base, config_dir, fresh=False):
    """The command that makes the tool environment match `extensions.txt`.

    --force because the tool is already there and this is a replacement, which is the only
    thing uv tool install does: there is no adding to an existing one.

    --fresh, and so --reinstall, for taking something out: whether --force alone prunes
    depends on the uv doing the work. Against a tool holding two extensions, uv 0.9.2 drops
    the package and uv 0.4.28 leaves it there with its entry point still registering a page.
    Adding needs neither.
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

    Only a registry requirement. A path install resolves to whatever is in the checkout,
    leaving nothing to relax, and a URL is already exact.
    """
    if not base or any(mark in base for mark in "/\\"):
        return None
    loosened = re.split(r"[=<>!~]", base, maxsplit=1)[0].strip()
    return loosened if loosened and loosened != base else None


def installed_version(name="statsbadge", prefix=None):
    """What version of `name` is in the environment on disk, or None.

    Read from the environment and not from this process, the point of asking being to
    notice that a rebuild moved it. `uv tool install` resolves the whole environment at
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


# Where a plain name is checked before anything is installed, so an unknown one is
# answered as unknown and not as a failed rebuild.
SIMPLE_INDEX = "https://pypi.org/simple/{}/"


def on_index(requirement, timeout=4.0):
    """Whether a plain name is a project on PyPI, or None when it cannot be told.

    Only asked of a bare name; a path, a URL or a version specifier goes straight to uv. An
    unreachable index is no answer either, so both come back None and the caller carries on
    without the check.
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
        return None


def blamed(said):
    """The package that could not be installed, out of uv's words or out of `explain`'s.

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


# uv's answer when a name is not a package. The resolver is long; the name is the useful
# part.
MISSING = re.compile(r"Because (\S+) was not found in the package registry")

# A plugin built against a newer host. Three things are kept: which extension, what it
# needs, and what this tool is pinned to.
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
    # uv writes to the same terminal, so flush before handing it over.
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
    # Translated and not passed on: uv ends with "your requirements are unsatisfiable",
    # which is true of every resolution failure and names no versions.
    clash = CONFLICT.search(flat)
    if clash:
        wants, needs, held = clash.groups()
        return f"{wants} needs {needs}, and this tool is installed as {held}"
    for line in said.splitlines():
        if line.startswith("error: "):
            return line[len("error: "):]
    return said.splitlines()[-1] if said else "uv did not say why"
