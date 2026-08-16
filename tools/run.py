"""Run the whole app from a mounted checkout, without installing it.

    mpremote connect PORT mount . run tools/run.py

The app normally lives in /system/apps/stats and puts that on sys.path itself; this
points it at the mount instead. UP/DOWN page, hold HOME to leave.
"""

import sys

# Import it as a package so its __init__ runs, which the launcher does.
sys.path.insert(0, "/remote/src/statsbadge")

import badge_app  # noqa: F401  the module runs the app on import
