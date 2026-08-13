"""What each badge was last seen holding, for telling a stale badge from an updated one.

`install.desired_hashes` is worked out on this host alone, so comparing it against a record
of the last install answers the question with no badge connected. That is the only way the
config UI can offer to update a badge before it is plugged in.

It is a best guess. Another machine can have installed something else since, and the answer
is only ever a prompt to connect the badge and look.
"""

import json
import os
import time

from . import install

PUSHED = "pushed.json"

# Which build the hashes came from. Bytecode and sources hash differently, so a comparison
# has to be made against the same kind. A directory named on the command line cannot be
# found again later, and anything from one is left uncompared.
PACKAGED = "packaged"
SOURCE = "source"
ELSEWHERE = "elsewhere"


def path(config_dir):
    return os.path.join(config_dir, PUSHED)


def read(config_dir):
    try:
        with open(path(config_dir), encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def record(config_dir, badge_id, hashes, source=None):
    """Note what a badge holds now, by uid."""
    found = read(config_dir)
    found[badge_id] = {"at": int(time.time()), "source": flavour(source),
                       "hashes": hashes}
    _write(config_dir, found)
    return found[badge_id]


def forget(config_dir, badge_id):
    found = read(config_dir)
    if found.pop(badge_id, None) is None:
        return False
    _write(config_dir, found)
    return True


def _write(config_dir, found):
    os.makedirs(config_dir, exist_ok=True)
    where = path(config_dir)
    tmp = where + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(found, handle, indent=2, sort_keys=True)
    os.replace(tmp, where)


def flavour(source):
    """Which build a directory is, as it will have to be recognised again later."""
    if not source:
        return SOURCE
    packaged = install.packaged_mpy_dir()
    if packaged and os.path.normpath(packaged) == os.path.normpath(source):
        return PACKAGED
    return ELSEWHERE


def _source_for(kind):
    """(directory to hash, whether it can be) for a recorded flavour."""
    if kind == SOURCE:
        return None, True
    if kind == PACKAGED:
        packaged = install.packaged_mpy_dir()
        return packaged, packaged is not None
    return None, False


def behind(config_dir, badge_id, modules=()):
    """What an install would change on a badge nobody has connected, or None.

    None where there is nothing recorded, or where what was recorded cannot be worked out
    again, such as an install from a precompiled build that is no longer in this package.
    """
    entry = read(config_dir).get(badge_id) or {}
    held = entry.get("hashes")
    if not held:
        return None
    source, known = _source_for(entry.get("source"))
    if not known:
        return None
    try:
        desired = install.desired_hashes(source, modules)
    except (OSError, install.InstallError):
        return None
    added, changed, removed = install.app_changes(held, desired)
    return {"at": entry.get("at"), "added": added, "changed": changed,
            "removed": removed, "behind": bool(added or changed or removed)}
