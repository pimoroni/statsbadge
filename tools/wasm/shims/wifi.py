"""Enough of the firmware's `wifi` for app.py to import. See socket.py, staged beside it.

There is no radio here. `is_connected` answers, since a page can be drawn while the badge
is off the network and that is worth drawing; connecting raises.
"""


def is_connected():
    return False


def status():
    return (0, "no radio under the WASM port")


def connect():
    raise OSError("no radio under the WASM port")
