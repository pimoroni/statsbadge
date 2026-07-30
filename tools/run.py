"""Run the whole app from a mounted checkout, without installing it.

    mpremote connect PORT mount . run tools/run.py

The app normally lives in /system/apps/stats and puts that on sys.path itself; this
points it at the mount instead. UP/DOWN page, hold HOME to leave.
"""

import sys

sys.path.insert(0, "/remote/stats")

import stats  # noqa: F401  the module runs the app on import
