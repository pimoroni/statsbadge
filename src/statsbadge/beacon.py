"""Broadcast where the server is, so a badge need not be told an IP address."""

import ipaddress
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

    def payload(self):
        """Return what goes out. Small: it travels in one UDP packet under 256 bytes."""
        return {
            "statsbadge": 1,
            "port": self.http_port,
            "host": self.name,
            # The badge keys its credentials on this, not on the address.
            "id": self.server_id,
            # The beacon interval, so a scan can be made longer than the gap between two.
            "every_ms": int(self.interval * 1000),
        }

    def _run(self):
        payload = json.dumps(self.payload()).encode("utf-8")
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
    """Return where to send: the global broadcast plus each interface's own."""
    addresses = ["255.255.255.255"]
    try:
        import psutil
        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                if entry.family != socket.AF_INET:
                    continue
                found = entry.broadcast or _subnet_broadcast(entry.address, entry.netmask)
                if found and found not in addresses:
                    addresses.append(found)
    except Exception:
        pass
    return addresses


def _subnet_broadcast(address, netmask):
    """Return the broadcast address for an interface, or None where it has none."""
    if not address or not netmask:
        return None
    try:
        network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
    except ValueError:
        return None
    # A /31 or /32 is a point to point link, and has no broadcast address to speak of.
    if network.prefixlen >= 31:
        return None
    return str(network.broadcast_address)
