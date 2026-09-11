"""The collector, the HTTP server and the beacon, started and stopped together."""

import errno
import json
import threading
import urllib.request

from . import beacon, server


class AddressInUse(OSError):
    """The port is taken. `by` is the other server's hello, where one answered."""

    def __init__(self, port, by=None):
        self.port = port
        self.by = by
        super().__init__(f"port {port} is already in use")


class Stack:
    def __init__(self, service, httpd, announcer, host, port):
        self.service = service
        self.httpd = httpd
        self.announcer = announcer
        self.host = host
        self.port = port
        self._thread = None
        self._stopped = False

    @classmethod
    def start(cls, service, host="0.0.0.0", port=8420, verbose=False, announce=True):
        service.start()
        try:
            httpd = server.make_server(service, host, port, verbose)
        except OSError as exc:
            service.stop()
            if exc.errno == errno.EADDRINUSE:
                raise AddressInUse(port, already_serving(port)) from exc
            raise
        announcer = None
        if announce:
            announcer = beacon.Beacon(port, service.identity["name"],
                                      service.identity["id"])
            announcer.start()
        return cls(service, httpd, announcer, host, port)

    def serve_forever(self):
        """Block here, as `serve` and `pair` do, until Ctrl-C."""
        self.httpd.serve_forever()

    def serve_in_background(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                        name="statsbadge-http")
        self._thread.start()

    def stop(self):
        """Wind down in the order the CLI has always used, the thread first."""
        if self._stopped:
            return
        self._stopped = True
        if self._thread:
            self.httpd.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self.httpd.server_close()
        if self.announcer:
            self.announcer.stop()
        self.service.stop()

    def addresses(self):
        return server._local_addresses()

    def status(self):
        """Return what the tray shows, read fresh each time."""
        badges = self.service.badges
        return {
            "port": self.port,
            "addresses": self.addresses(),
            "badges": badges.list_badges(),
            "pending": badges.pending_enrolments(),
            "pairing": badges.pairing_state(),
        }


def already_serving(port, host="127.0.0.1", timeout=0.5):
    """Return another statsbadge on this port, as its /v1/hello, or None."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/v1/hello",
                                    timeout=timeout) as response:
            found = json.loads(response.read(4096))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and found.get("server") == "statsbadge" else None
