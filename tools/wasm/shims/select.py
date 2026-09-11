"""`select.poll`, over the sockets in socket.py."""

from socket import POLLERR, POLLHUP, POLLIN, POLLOUT  # noqa: F401


class poll:
    def __init__(self):
        self._watching = []

    def register(self, sock, wanted=POLLIN | POLLOUT):
        self._watching.append((sock, wanted))

    def unregister(self, sock):
        self._watching = [held for held in self._watching if held[0] is not sock]

    def poll(self, _timeout=0):
        """Return whatever is ready now."""
        ready = []
        for sock, wanted in self._watching:
            flags = sock.poll_flags(wanted)
            if flags:
                ready.append((sock, flags))
        return ready
