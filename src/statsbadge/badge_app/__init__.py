"""The launcher's entry point: importing this starts the app."""

import os
import sys

APP_DIR = "/system/apps/stats"
try:
    os.chdir(APP_DIR)
except OSError:
    # Running from a mounted checkout. Located by this file and not cwd, which under
    # `mpremote mount` is the mount root.
    here = globals().get("__file__")
    APP_DIR = here.rsplit("/", 1)[0] if here and "/" in here else os.getcwd()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app  # noqa: E402

# Bound before main() blocks, since HOME quits through it.
on_exit = app.on_exit

app.main(APP_DIR)
