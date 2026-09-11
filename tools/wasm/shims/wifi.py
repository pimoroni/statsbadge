"""Enough of the firmware's `wifi` for app.py to import. See socket.py, staged beside it."""


def is_connected():
    return False


def status():
    return (0, "no radio under the WASM port")


def connect():
    raise OSError("no radio under the WASM port")
