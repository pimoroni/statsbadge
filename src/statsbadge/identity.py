"""A stable name for this server, independent of its address.

An IP address is the wrong key for a pairing: a DHCP lease moves, and the badge would be
looking for a host at a number nothing answers on.

Each server mints an id once and puts it in the discovery beacon and `/v1/hello`. The
badge keys its credentials on that, so it recognises the host wherever it turns up.

The id is not a secret. It says which secret to sign with; the signature does the proving.
"""

import json
import os
import secrets
import socket


def load(config_dir):
    """This server's identity, minting and saving one if there is not one yet."""
    path = os.path.join(config_dir, "server.json")
    try:
        with open(path) as handle:
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
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def _hostname():
    return socket.gethostname().split(".")[0]
