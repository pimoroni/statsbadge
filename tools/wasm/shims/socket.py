"""`socket`, over node's, for the WASM port - which carries no networking at all.

The runner opens the connections with `node:net` and holds them; this side asks after
them. Node fills its buffers on its own event loop, and the JSPI build yields to that
loop as the VM runs, so a poll here sees what has arrived since the last one.

Bytes cross as base64, which is the one encoding both sides agree on without argument.

TCP only. A datagram raises: the beacon is the badge's discovery, and answering it here
would mean a stand-in for a host that is not running.
"""

import binascii

import js

AF_INET = 2
SOCK_STREAM = 1
SOCK_DGRAM = 2
SOL_SOCKET = 1
SO_REUSEADDR = 2

# What `state` reports, and what net.py's errno table expects back.
CONNECTED, READABLE, ENDED, FAILED = 1, 2, 4, 8
EINPROGRESS, ECONNABORTED = 115, 103


def getaddrinfo(host, port, _family=0, _kind=SOCK_STREAM, *_rest):
    """The one shape net.py reads: family, type, proto, canonname, address."""
    return [(AF_INET, SOCK_STREAM, 0, "", (host, int(port)))]


class socket:
    def __init__(self, family=AF_INET, kind=SOCK_STREAM, _proto=0):
        if kind != SOCK_STREAM:
            raise OSError("no datagrams under the WASM port")
        self.family = family
        self._handle = None
        self._blocking = True
        self._held = b""

    # -- what net.py drives -------------------------------------------------
    def setblocking(self, blocking):
        self._blocking = bool(blocking)

    def setsockopt(self, *_args):
        pass

    def connect(self, address):
        host, port = address
        self._handle = int(js.sb_connect(host, int(port)))
        if not self._blocking:
            # The handshake has only been started, which net.py expects to hear.
            raise OSError(EINPROGRESS)
        while not (self._state() & (CONNECTED | FAILED)):
            pass
        if self._state() & FAILED:
            raise OSError(ECONNABORTED)

    def write(self, data):
        if not isinstance(data, bytes):
            data = bytes(data)
        js.sb_send(self._handle, binascii.b2a_base64(data).decode().strip())
        return len(data)

    send = write

    def readline(self):
        """One line, or None while the rest of it is still coming."""
        self._fill()
        at = self._held.find(b"\n")
        if at < 0:
            return None
        line, self._held = self._held[:at + 1], self._held[at + 1:]
        return line

    def readinto(self, view):
        self._fill()
        if not self._held:
            return None
        room = len(view)
        taken, self._held = self._held[:room], self._held[room:]
        view[:len(taken)] = taken
        return len(taken)

    def recv(self, count):
        self._fill()
        got, self._held = self._held[:count], self._held[count:]
        return got

    def close(self):
        if self._handle is not None:
            js.sb_close(self._handle)
            self._handle = None

    # -- what select.poll asks ----------------------------------------------
    def _state(self):
        return int(js.sb_state(self._handle)) if self._handle is not None else FAILED

    def poll_flags(self, wanted):
        """The flags `wanted` asks about, as select.poll would report them."""
        state = self._state()
        flags = 0
        if state & FAILED:
            flags |= POLLERR
        if state & ENDED and not (state & READABLE) and not self._held:
            flags |= POLLHUP
        if state & CONNECTED:
            flags |= POLLOUT & wanted
            if self._held or state & READABLE:
                flags |= POLLIN & wanted
        return flags

    def _fill(self):
        if self._handle is None:
            return
        arrived = str(js.sb_recv(self._handle))
        if arrived:
            self._held += binascii.a2b_base64(arrived.encode())


# select.poll works on streams the runtime knows about, and these are not among them,
# so the flags live here and select.py drives them.
POLLIN, POLLOUT, POLLERR, POLLHUP = 1, 4, 8, 16
