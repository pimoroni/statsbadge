"""Enough of `select` for net.py to import. See socket.py, staged beside this.

The flags are distinct bits and nothing else: net.py only ever tests what its own poller
handed back, and there is no poller here.
"""

POLLIN = 1
POLLOUT = 4
POLLERR = 8
POLLHUP = 16


def poll():
    raise OSError("no select under the WASM port")
