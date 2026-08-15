"""Enough of `socket` for net.py to import under the WASM port, which carries none.

Nothing here reaches a network, and everything that would raises. A test that believes
it polled a host fails, where a stand-in that answered would have it pass.

Last on sys.path, so a port that grows a real socket module answers instead.
"""

AF_INET = 2
SOCK_STREAM = 1
SOCK_DGRAM = 2
SOL_SOCKET = 1
SO_REUSEADDR = 2

WHY = "no socket under the WASM port"


def getaddrinfo(host, port, *_rest):
    raise OSError(f"{WHY}: {host}:{port}")


def socket(*_args):
    raise OSError(WHY)
