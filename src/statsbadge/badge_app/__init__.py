"""The launcher's entry point: importing this starts the app.

`launch()` imports the app directory and then looks `on_exit` up on the module it
imported, so there is no `__main__` here to guard on. Everything the app is lives in
app.py, which can be imported without any of it running.
"""

import os
import sys

APP_DIR = "/system/apps/stats"
try:
    os.chdir(APP_DIR)
except OSError:
    # Running from a mounted checkout. Locate the app by this file
    # and not by cwd, which under `mpremote mount` is the mount root and not the
    # app directory - so `pages/` would be looked for in the wrong place.
    here = globals().get("__file__")
    APP_DIR = here.rsplit("/", 1)[0] if here and "/" in here else os.getcwd()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app  # noqa: E402  the path above is what makes this importable

# Bound before main() blocks, since HOME quits through it.
on_exit = app.on_exit

app.main(APP_DIR)
