"""Broadcast where the server is, so a badge need not be told an IP address.

Typing a dotted quad on six buttons is miserable. The server sends a small JSON packet
to the broadcast address every couple of seconds, and the badge listens for it during
setup. Nothing sensitive is in it, and it stays on the local segment.
"""

import json
import socket
import threading

PORT = 8421
INTERVAL = 2.0


class Beacon:
    def __init__(self, http_port, name, server_id=None, port=PORT, interval=INTERVAL):
        self.http_port = http_port
        self.name = name
        self.server_id = server_id
        self.port = port
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="statsbadge-beacon")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        payload = json.dumps({
            "statsbadge": 1,
            "port": self.http_port,
            "host": self.name,
            # The badge keys its credentials on this, not on the address.
            "id": self.server_id,
        }).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                for address in _broadcast_addresses():
                    try:
                        sock.sendto(payload, (address, self.port))
                    except OSError:
                        continue
                self._stop.wait(self.interval)
        finally:
            sock.close()


def _broadcast_addresses():
    """Where to send. The global broadcast plus each interface's own, because some
    networks drop 255.255.255.255 but pass a subnet broadcast."""
    addresses = ["255.255.255.255"]
    try:
        import psutil
        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                if entry.family == socket.AF_INET and entry.broadcast:
                    if entry.broadcast not in addresses:
                        addresses.append(entry.broadcast)
    except Exception:
        pass
    return addresses
