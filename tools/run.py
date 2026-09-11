"""Run the whole app from a mounted checkout, without installing it."""

import sys

# Import it as a package so its __init__ runs, which the launcher does.
sys.path.insert(0, "/remote/src/statsbadge")

import badge_app  # noqa: F401
