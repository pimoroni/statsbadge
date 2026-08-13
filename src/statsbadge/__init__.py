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

    A briefcase bundle leaves no marker of its own, so the tell is the executable: a
    Python, or something else. It matters twice. Running it with `-m pip` starts a second
    copy of the app, and a login entry has to name the app itself.
    """
    if getattr(sys, "frozen", False):
        return True
    beside = os.path.dirname(sys.executable or "")
    if os.path.basename(sys.executable or "").lower().startswith("python"):
        return False
    # A console script's launcher sits beside the interpreter it runs, which is what
    # `statsbadge-tray.exe` in a venv's Scripts is. A bundle's binary has no such
    # neighbour: its Python is somewhere else inside the app.
    return not any(os.path.exists(os.path.join(beside, name)) for name in
                   ("python.exe", "pythonw.exe", "python3", "python"))
