"""System stats from a host PC, drawn on a Badgeware badge.

No `__version__` here: pyproject.toml names the version and the installed
distribution carries it, so `version()` reads the one that is actually installed
rather than a second copy that can disagree with it.
"""

from importlib.metadata import PackageNotFoundError, version as _installed


def version():
    """What this install says it is, or "unknown" from a checkout that is not installed."""
    try:
        return _installed("statsbadge")
    except PackageNotFoundError:
        return "unknown"
