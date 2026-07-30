"""The HTTP server the badge talks to, and the config UI beside it.

Every response goes out as a single `write()` with TCP_NODELAY set. That is not a
micro-optimisation: `http.server` flushing headers and body separately costs a badge
247ms per request against 7ms for one write, because Nagle holds the body until lwIP
gets round to acknowledging the headers. NETWORKING.md has the measurements.

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
import threading
import traceback

from . import auth, commands, extensions, layout
from .collect import Collector

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")

REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
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
        self.collector = Collector(interval=interval, config=source_config or {})
        self.started = threading.Event()

    def start(self):
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

    def capabilities(self):
        caps = self.collector.capabilities()
        caps["commands"] = commands.names()
        caps["themes"] = list(layout.THEMES)
        caps["kinds"] = list(layout.KINDS)
        caps["extension_pages"] = extensions.badge_pages(self.collector.extensions)
        return caps


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
                "host": service.collector.latest().get("sys", {}).get("host"),
                "pairing": service.badges.pairing_active(),
                "layout_rev": service.config.rev,
                "interval_ms": service.config.snapshot().get("interval_ms", 1000),
            })

        if path == "/v1/pair" and method == "POST":
            payload = json.loads(body or b"{}")
            badge_id = str(payload.get("badge_id") or "").strip()
            if not badge_id:
                return self._fail(400, "badge_id required")
            secret = service.badges.claim(
                str(payload.get("code") or ""), badge_id, payload.get("name"))
            return self._json(200, {"secret": secret, "badge_id": badge_id})

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
                    rev = service.config.replace(
                        json.loads(body or b"{}"), service.extension_kinds())
                except ValueError as exc:
                    return self._fail(400, str(exc))
                return self._json(200, {"rev": rev})

        if path == "/api/stats" and method == "GET":
            return self._json(200, service.collector.latest())

        if path == "/api/preview" and method == "GET":
            # What the badge would be sent, so the UI can show pruning.
            return self._json(200, service.config.for_badge(service.capabilities()))

        if path == "/api/pair" and method == "POST":
            code = service.badges.begin_pairing()
            return self._json(200, {"code": code, "expires_in": 300,
                                    "hosts": _local_addresses(),
                                    "port": self.server.server_address[1]})

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
