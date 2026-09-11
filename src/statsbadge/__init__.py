"""System stats from a host PC, drawn on a Badgeware badge."""

import os
import sys
from importlib.metadata import PackageNotFoundError, version as _installed


def version():
    """The version recorded for this install, or "unknown" from an uninstalled checkout."""
    try:
        return _installed("statsbadge")
    except PackageNotFoundError:
        return "unknown"


# Windows opens a console window for every child a GUI app starts, and a tray is one.
# subprocess.CREATE_NO_WINDOW, named here so nothing has to import subprocess to say it.
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}

# What a packaged app spawns itself as to be pip. There is no interpreter in a bundle to
# run `-m pip` with.
PIP_VERB = "--be-pip"


def bundled():
    """Return whether this is a packaged app, where `sys.executable` is the app binary."""
    if getattr(sys, "frozen", False):
        return True
    beside = os.path.dirname(sys.executable or "")
    if os.path.basename(sys.executable or "").lower().startswith("python"):
        return False
    # A console script's launcher sits beside the interpreter it runs, which
    # `statsbadge-tray.exe` in a venv's Scripts is. A bundle's binary has no such
    # neighbour.
    return not any(os.path.exists(os.path.join(beside, name)) for name in
                   ("python.exe", "pythonw.exe", "python3", "python"))
