"""The HTTP server the badge talks to, and the config UI beside it.

Every response goes out as a single `write()` with TCP_NODELAY set. That is not a
micro-optimisation: `http.server` flushing headers and body separately costs a badge
247ms per request against 7ms for one write, because Nagle holds the body until lwIP
gets round to acknowledging the headers. DEVELOPMENT.md has the measurements.

Two audiences on one port:

  /v1/*    the badge. Every request HMAC-signed, see auth.py.
  /api/*   the config UI. Loopback only, because it can mint pairing secrets.
"""

import http.server
import ipaddress
import json
import os
import socket
import socketserver
import sys
import threading
import traceback

from . import auth, commands, extensions, identity, layout
from .collect import Collector

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")

REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
}

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


class Service:
    """Everything the request handlers need, in one place."""

    def __init__(self, config_dir, interval=1.0, source_config=None):
        self.config_dir = config_dir
        self.config = layout.Config(os.path.join(config_dir, "layout.json"))
        self.badges = auth.Store(os.path.join(config_dir, "badges.json"))
        self.identity = identity.load(config_dir)
        # Stored settings reach the sources as they are constructed, so an extension
        # configured in the browser works from the next start with no flags to remember.
        source_config = dict(source_config or {})
        source_config["extensions"] = layout.merge_settings(
            source_config.get("extensions"), self.config.snapshot().get("settings"))
        self.collector = Collector(interval=interval, config=source_config)
        self.started = threading.Event()

    def start(self):
        self.announce_pages()
        self.collector.start()
        self.started.set()

    def stop(self):
        self.collector.stop()

    def extension_kinds(self):
        """Page kinds only the installed extensions know how to draw."""
        return tuple(
            page["kind"]
            for page in extensions.badge_pages(self.collector.extensions)
            if page.get("kind")
        )

    def extension_settings(self):
        """What each installed extension can be told, for the UI and the validator."""
        return extensions.settings_schema(self.collector.extensions)

    def extension_page_settings(self):
        """What an extension's own pages can be told, keyed by page kind."""
        return extensions.page_settings_schema(self.collector.extensions)

    def announce_pages(self):
        """Tell the sources about the pages already stored, at startup.

        replace_config covers a later save; without this a source doing per-page work
        does nothing until someone presses Save.
        """
        extensions.configure_pages(self.collector.extensions,
                                   self.config.snapshot().get("pages"))

    def capabilities(self):
        caps = self.collector.capabilities()
        caps["commands"] = commands.names()
        caps["themes"] = list(layout.THEMES)
        caps["kinds"] = list(layout.KINDS)
        caps["extension_pages"] = extensions.badge_pages(self.collector.extensions)
        caps["extension_settings"] = self.extension_settings()
        caps["extension_page_settings"] = self.extension_page_settings()
        return caps

    def replace_config(self, incoming):
        """Store a config from the UI and hand the new settings to the sources.

        Applied here rather than at the next restart, so a location typed in the browser
        takes effect on the next sample.
        """
        rev = self.config.replace(incoming, self.extension_kinds(),
                                 self.extension_settings(),
                                 self.extension_page_settings())
        stored = self.config.snapshot()
        extensions.configure(self.collector.extensions, stored.get("settings"))
        # Pages too, so a source doing per-page work sees the new ones without waiting
        # for a restart.
        extensions.configure_pages(self.collector.extensions, stored.get("pages"))
        return rev


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Off, plus the single write below: this is the whole 30x.
    disable_nagle_algorithm = True
    server_version = "statsbadge"
    sys_version = ""

    service = None      # set by make_server

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send(self, status, body, content_type="application/json", extra=None):
        """One write, always with Content-Length, never chunked."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        body = body or b""
        lines = [
            f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: keep-alive",
            "Cache-Control: no-store",
        ]
        for key, value in (extra or {}).items():
            lines.append(f"{key}: {value}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        try:
            self.wfile.write(head + body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload, separators=(",", ":")),
                   "application/json", extra)

    def _fail(self, status, reason):
        self._json(status, {"error": reason})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > 1 << 20:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def _is_local(self):
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return addr.is_loopback

    def _path(self):
        return self.path.split("?", 1)[0]

    def _query(self):
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        path = self._path()
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._fail(413, str(exc))

        try:
            if path.startswith("/v1/"):
                return self._badge_api(method, path, body)
            if path.startswith("/api/"):
                if not self._is_local():
                    return self._fail(403, "config API is loopback only")
                return self._config_api(method, path, body)
            if method == "GET":
                return self._static(path)
            return self._fail(405, "method not allowed")
        except auth.AuthError as exc:
            payload = {"error": exc.reason}
            payload.update(exc.detail)
            return self._json(exc.status, payload)
        except Exception as exc:
            if self.server.verbose:
                traceback.print_exc()
            return self._fail(500, f"{type(exc).__name__}: {exc}")

    # -- the badge ----------------------------------------------------------

    def _badge_api(self, method, path, body):
        service = self.service

        # Unauthenticated, on purpose: a badge needs to find the host and learn
        # whether pairing is open before it holds a secret. Neither leaks stats.
        if path == "/v1/hello" and method == "GET":
            return self._json(200, {
                "server": "statsbadge",
                "version": 1,
                "id": service.identity["id"],
                "name": service.identity["name"],
                "host": service.collector.latest().get("sys", {}).get("host"),
                "pairing": service.badges.pairing_active(),
                "layout_rev": service.config.rev,
                "interval_ms": service.config.snapshot().get("interval_ms", 1000),
            })

        # Unauthenticated: the badge has no secret yet. Gated on a human approving it.
        if path == "/v1/enrol" and method == "POST":
            payload = json.loads(body or b"{}")
            badge_id = str(payload.get("badge_id") or "").strip()
            if not badge_id:
                return self._fail(400, "badge_id required")
            asked = service.badges.request_enrolment(badge_id, payload.get("name"))
            return self._json(200, {
                "request_id": asked["request_id"],
                "code": asked["code"],
                "id": service.identity["id"],
                "name": service.identity["name"],
            })

        # Polled by the badge. The request id collects the secret, once.
        if path.startswith("/v1/enrol/") and method == "GET":
            outcome = service.badges.enrolment(path[len("/v1/enrol/"):])
            if outcome.get("status") == "approved":
                outcome["id"] = service.identity["id"]
                outcome["name"] = service.identity["name"]
            return self._json(200, outcome)

        # Everything past here is signed. Over `self.path`, not the routing path:
        # the query string changes the response, so it has to be covered too.
        badge_id = service.badges.verify(method, self.path, _lower(self.headers), body)

        if path == "/v1/stats" and method == "GET":
            frame = dict(service.collector.latest())
            frame["layout_rev"] = service.config.rev
            return self._json(200, frame)

        if path == "/v1/layout" and method == "GET":
            return self._json(200, service.config.for_badge(service.capabilities()))

        if path == "/v1/history" and method == "GET":
            query = self._query()
            keys = [k for k in (query.get("keys") or "").split(",") if k]
            points = max(1, min(160, int(query.get("points") or 48)))
            return self._json(200, service.collector.history(keys or None, points))

        if path == "/v1/command" and method == "POST":
            payload = json.loads(body or b"{}")
            name = str(payload.get("cmd") or "")
            allowed = {
                v for v in service.config.snapshot().get("buttons", {}).values() if v
            }
            if name not in allowed:
                return self._fail(403, f"command {name!r} is not bound to a button")
            try:
                return self._json(200, {"cmd": name, "result": commands.run(name)})
            except commands.CommandError as exc:
                return self._fail(400, str(exc))

        return self._fail(404, "no such endpoint")

    # -- the config UI ------------------------------------------------------

    def _config_api(self, method, path, body):
        service = self.service

        if path == "/api/capabilities" and method == "GET":
            return self._json(200, service.capabilities())

        if path == "/api/config":
            if method == "GET":
                return self._json(200, service.config.snapshot())
            if method == "PUT":
                try:
                    rev = service.replace_config(json.loads(body or b"{}"))
                except ValueError as exc:
                    return self._fail(400, str(exc))
                return self._json(200, {"rev": rev})

        if path == "/api/stats" and method == "GET":
            return self._json(200, service.collector.latest())

        if path == "/api/preview" and method == "GET":
            # What the badge would be sent, so the UI can show pruning.
            return self._json(200, service.config.for_badge(service.capabilities()))

        if path == "/api/pair" and method == "GET":
            state = service.badges.pairing_state()
            state["hosts"] = _local_addresses()
            state["port"] = self.server.server_address[1]
            return self._json(200, state)

        if path == "/api/pair" and method == "POST":
            service.badges.begin_pairing()
            return self._json(200, {"active": True, "expires_in": 300,
                                    "hosts": _local_addresses(),
                                    "port": self.server.server_address[1]})

        if path == "/api/enrol" and method == "GET":
            return self._json(200, {"pending": service.badges.pending_enrolments()})

        if path.startswith("/api/enrol/") and method == "POST":
            rest = path[len("/api/enrol/"):]
            request_id, _, action = rest.partition("/")
            if action == "approve":
                badge_id = service.badges.approve_enrolment(request_id)
                return self._json(200, {"approved": badge_id})
            if action == "deny":
                return self._json(200, {"denied": service.badges.deny_enrolment(request_id)})
            return self._fail(400, "expected /approve or /deny")

        if path == "/api/pair" and method == "DELETE":
            service.badges.cancel_pairing()
            return self._json(200, {"pairing": False})

        if path == "/api/badges" and method == "GET":
            return self._json(200, service.badges.list_badges())

        if path.startswith("/api/badges/") and method == "DELETE":
            badge_id = path[len("/api/badges/"):]
            return self._json(200, {"forgotten": service.badges.forget(badge_id)})

        return self._fail(404, "no such endpoint")

    # -- static -------------------------------------------------------------

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            return self._fail(404, "not found")
        with open(target, "rb") as handle:
            body = handle.read()
        ext = os.path.splitext(target)[1]
        return self._send(200, body, TYPES.get(ext, "application/octet-stream"))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        """A client dropping a pooled connection is not a fault.

        Keep-alive parks a thread in readline waiting for the next request. A peer that
        closes without shutting down resets the socket instead of ending it cleanly, so
        that read fails with nothing in flight to lose.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            if self.verbose:
                print(f"{client_address[0]} dropped the connection: "
                      f"{type(exc).__name__}", file=sys.stderr)
            return
        super().handle_error(request, client_address)


def make_server(service, host="0.0.0.0", port=8420, verbose=False):
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = Server((host, port), handler)
    server.verbose = verbose
    return server


def _lower(headers):
    return {key.lower(): value for key, value in headers.items()}


def _local_addresses():
    """Addresses a badge could reach this host on, best guess first."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 1))     # a reserved address; no traffic leaves
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    return found
