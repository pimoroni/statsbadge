"""A stable name for this server, independent of its address."""

import json
import os
import secrets
import socket

from . import state


def load(config_dir):
    """This server's identity, minting and saving one if there is not one yet."""
    path = os.path.join(config_dir, "server.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("id"):
            # The friendly name follows the hostname, which may have changed.
            name = _hostname()
            if data.get("name") != name:
                data["name"] = name
                _write(path, data)
            return data
    except (OSError, ValueError):
        pass

    data = {"id": secrets.token_hex(8), "name": _hostname()}
    _write(path, data)
    return data


def _write(path, data):
    state.write(path, json.dumps(data, indent=2))


def _hostname():
    return socket.gethostname().split(".")[0]
