"""`select.poll`, over the sockets in socket.py.

The runtime has a select of its own, and this shadows it: its poll works on streams the
runtime opened, and the ones here live in node. Nothing else in the app polls, so there
is nothing else to shadow.
"""

from socket import POLLERR, POLLHUP, POLLIN, POLLOUT  # noqa: F401  net.py reads them here


class poll:
    def __init__(self):
        self._watching = []

    def register(self, sock, wanted=POLLIN | POLLOUT):
        self._watching.append((sock, wanted))

    def unregister(self, sock):
        self._watching = [held for held in self._watching if held[0] is not sock]

    def poll(self, _timeout=0):
        """Whatever is ready now. The caller yields between calls, and node fills its
        buffers while it does."""
        ready = []
        for sock, wanted in self._watching:
            flags = sock.poll_flags(wanted)
            if flags:
                ready.append((sock, flags))
        return ready
