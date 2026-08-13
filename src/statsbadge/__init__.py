"""System stats from a host PC, drawn on a Badgeware badge.

No `__version__` here: pyproject.toml names the version and the installed
distribution carries it, so `version()` reads the one that is actually installed
rather than a second copy that can disagree with it.
"""

import os
import sys
from importlib.metadata import PackageNotFoundError, version as _installed


def version():
    """What this install says it is, or "unknown" from a checkout that is not installed."""
    try:
        return _installed("statsbadge")
    except PackageNotFoundError:
        return "unknown"


def bundled():
    """Whether this is a packaged app, where `sys.executable` is the app's own binary.

    A briefcase bundle leaves no marker, so the tell is that the executable is not a
    Python. It matters twice: running it with `-m pip` starts a second copy of the app,
    and a login entry has to name the app itself and not an interpreter with flags.
    """
    if getattr(sys, "frozen", False):
        return True
    return not os.path.basename(sys.executable or "").lower().startswith("python")
