"""The HTTP server the badge talks to, and the config UI beside it.

Every response goes out as a single `write()` with TCP_NODELAY set. Flushing headers and
body separately costs a badge 247ms a request against 7ms, Nagle holding the body until
lwIP acknowledges the headers. DEVELOPMENT.md has the measurements.

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
import time
import traceback

from . import (auth, commands, derive, extensions, identity, install, layout, library,
               push, pushed, themes, tooling)
from .collect import Collector

# Normalised and absolute: `_static` compares a normalised target against this, so a `..`
# left in here would refuse everything instead.
STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))

REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    413: "Payload Too Large", 429: "Too Many Requests",
    500: "Internal Server Error",
}

# Lines of install progress kept for the UI to poll.
INSTALL_LOG = 400

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
    # The faces the badge draws with, so the preview matches the device.
    ".ttf": "font/ttf",
    ".woff2": "font/woff2",
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
        self.collector = Collector(interval=interval, config=source_config,
                                   state_dir=os.path.join(config_dir, "extensions"))
        self.started = threading.Event()
        self._installing = threading.Lock()
        self._usb = threading.Lock()
        self._job = None

    def start(self):
        self.announce_pages()
        self.collector.start()
        self.started.set()

    def stop(self):
        self.collector.stop()

    def reload_extensions(self):
        """Take up whatever is installed now, and tell it about the pages already stored."""
        names = self.collector.reload_extensions()
        self.announce_pages()
        return names

    def extension_catalogue(self):
        """What the UI offers, and whether this install can act on it."""
        listed = extensions.describe(tooling.read_disabled(self.config_dir))
        where = library.current(self.config_dir)
        for record in listed:
            # Only what the library built can be uninstalled from here. The rest is the
            # environment's, and the most this can do about one is stop loading it.
            record["managed"] = bool(where and library.holds(where, record["name"]))
        return {
            "offered": extensions.offered(
                installed=listed,
                wanted=tooling.read_wanted(self.config_dir),
                disabled=tooling.read_disabled(self.config_dir)),
            # Extensions go beside the config, so any layout can manage them. All this
            # needs is something to install with.
            "manageable": library.installer() is not None,
            "prefix": sys.prefix,
        }

    def switch_extensions(self, verb, asking):
        """Stop loading an extension, or start again. Takes effect where it stands."""
        changed = tooling.switch(self.config_dir, asking, off=verb == "disable")
        self.collector.config["disabled_extensions"] = tooling.read_disabled(
            self.config_dir)
        return {"ok": True, "changed": changed, "loaded": self.reload_extensions(),
                "why": None, "stuck": [], "shadowed": [], "unpinned": [],
                "already": [], "restored": [], "absent": [], "unknown": None,
                "nothing": not changed, "needs_usb": [], "restart": []}

    def change_extensions(self, verb, asking):
        """Install or remove, then take up the result without a restart.

        One at a time. Two rebuilds at once would race over the same environment, and the
        second would be resolved from a list the first had already replaced.
        """
        with self._installing:
            done = tooling.apply(self.config_dir, verb, asking,
                                 {record["name"] for record in extensions.describe()})
            answer = {key: done[key] for key in
                      ("ok", "why", "already", "restored", "absent", "unknown", "stuck",
                       "unpinned", "shadowed", "nothing")}
            answer["changed"] = [tooling.short_name(r) for r in done["changed"]]
            if done["unknown"]:
                answer["why"] = f"nothing on PyPI is called {done['unknown']}"
            if done["ok"]:
                answer["loaded"] = self.reload_extensions()
                # Only what the badge cannot be given over the wire: /v1 carries readings
                # and a layout, never code.
                answer["needs_usb"] = sorted({
                    name for name, _path in extensions.badge_modules(
                        self.collector.extensions)})
                # Already imported code stays imported, so a newer release of something
                # running is on disk and not yet in this process.
                answer["restart"] = sorted(
                    set(answer["changed"]) & set(answer["loaded"])
                ) if verb == "upgrade" else []
            return answer

    def extension_kinds(self):
        """Page kinds that only the installed extensions can draw."""
        return tuple(
            page["kind"]
            for page in extensions.badge_pages(self.collector.extensions)
            if page.get("kind")
        )

    def extension_settings(self):
        """What each installed extension can be told, for the UI and the validator."""
        return extensions.settings_schema(self.collector.extensions)

    def extension_page_settings(self):
        """What an extension's pages can be told, keyed by page kind."""
        return extensions.page_settings_schema(self.collector.extensions)

    def announce_pages(self):
        """Tell the sources about the pages already stored, at startup.

        Every badge's, since a source doing per-page work fetches for all of them at once.
        replace_config covers a later save; without this one does nothing until someone
        presses Save.
        """
        extensions.configure_pages(self.collector.extensions, self.config.all_pages())

    def capabilities(self):
        caps = self.collector.capabilities()
        # With each one's heading and label, so the picker can group them without knowing which
        # is which.
        caps["commands"] = commands.records()
        # With each one's label, mode and whether it takes an accent, so the picker groups
        # them and offers the swatches without holding a list.
        caps["themes"] = layout.theme_records()
        # What a button can be bound to that the badge does itself, labelled: the UI offers
        # these in the same list as the commands.
        caps["local_actions"] = [{"action": action, "label": label}
                                 for action, label in layout.LOCAL_ACTIONS]
        # The colours too, so the UI's swatches cannot drift from the badge's tables.
        # Four families of twelve, the same for every page. The picker shows them as tabs.
        caps["accents"] = {family: [list(accent) for accent in derive.accents(family)]
                           for family in derive.ACCENT_FAMILIES}
        caps["accent_family"] = derive.DEFAULT_FAMILY
        # How a second accent can be picked, so the HTML holds no copy of the list.
        caps["accent_b_rules"] = list(layout.ACCENT_B_RULES)
        caps["kinds"] = list(layout.KINDS)
        # Every discovered extension, not only those with something to be told: one that failed
        # to import is reported here instead of showing up as a page that never draws.
        caps["extensions"] = extensions.describe()
        caps["extension_pages"] = extensions.badge_pages(self.collector.extensions)
        caps["extension_settings"] = self.extension_settings()
        caps["extension_page_settings"] = self.extension_page_settings()
        return caps

    def replace_config(self, incoming, badge_id=None):
        """Store a layout from the UI and hand the new settings to the sources.

        Applied here and not at the next restart, which gets a location typed in the
        browser into the next sample. `badge_id` says whose layout this is; without one
        it is the default.
        """
        rev = self.config.replace(incoming, self.extension_kinds(),
                                 self.extension_settings(),
                                 self.extension_page_settings(), badge_id)
        # One answer per machine, so settings are read back from the store and not from a badge.
        extensions.configure(self.collector.extensions,
                             self.config.snapshot().get("settings"))
        # Pages too, so a source doing per-page work sees new ones without a restart. Every
        # badge's, since it fetches for all of them at once.
        extensions.configure_pages(self.collector.extensions, self.config.all_pages())
        return rev


    # -- over USB -----------------------------------------------------------

    def badge_modules(self):
        return extensions.badge_modules(self.collector.extensions)

    def app_state(self, badge_id):
        """Whether a badge is behind what an install would put on it, or None."""
        return pushed.behind(self.config_dir, badge_id, self.badge_modules())

    def install_options(self, asked, http_port):
        """What the browser is allowed to set, with the rest from this host."""
        options = {}
        for key in ("name", "ssid", "password", "region", "port_dev"):
            if asked.get(key) is not None:
                options[key] = str(asked[key])
        for key in ("force_secrets", "force_app", "new_secret"):
            options[key] = bool(asked.get(key))
        if asked.get("timezone") is not None:
            options["timezone"] = int(asked["timezone"])
        options["config_dir"] = self.config_dir
        options["host"] = (_local_addresses() or ["127.0.0.1"])[0]
        options["port"] = http_port
        # The browser asked for this, having been told what it costs. There is nobody
        # here to prompt.
        options["yes"] = True
        return options

    def install_state(self):
        """The install running now, or the last one, and whether a badge is plugged in."""
        job = self._job
        return {
            "running": bool(job and job["running"]),
            "started": job["started"] if job else None,
            "log": list(job["log"]) if job else [],
            "result": job["result"] if job else None,
            "ports": install.find_ports(),
        }

    def start_install(self, options):
        """Push to a connected badge on a thread. False where one is already running."""
        with self._usb:
            if self._job and self._job["running"]:
                return False
            self._job = {"running": True, "started": time.time(), "log": [],
                         "result": None}
            job = self._job
        threading.Thread(target=self._install, args=(job, options),
                         name="statsbadge-install", daemon=True).start()
        return True

    def _install(self, job, options):
        def say(text):
            job["log"].append(text)
            del job["log"][:-INSTALL_LOG]

        try:
            job["result"] = push.push(options, badges=self.badges,
                                      identity=self.identity,
                                      modules=self.badge_modules(), say=say)
        except Exception as exc:
            job["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            job["running"] = False


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
                if path == "/tokens.css":
                    return self._tokens()
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

        # Unauthenticated: a badge has to find the host and learn whether pairing is open before
        # it holds a secret. Neither leaks stats.
        if path == "/v1/hello" and method == "GET":
            return self._json(200, {
                "server": "statsbadge",
                "version": 1,
                "id": service.identity["id"],
                "name": service.identity["name"],
                "host": service.collector.latest().get("sys", {}).get("host"),
                "pairing": service.badges.pairing_active(),
                # The default's: this is the one call a badge makes before it can prove who it
                # is. It watches for changes on the revision in a signed stats frame instead.
                "layout_rev": service.config.rev,
                "interval_ms": service.config.layout_for().get("interval_ms", 1000),
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

        # Everything past here is signed over `self.path` and not the routing path, the query
        # string changing the response.
        badge_id = service.badges.verify(method, self.path, _lower(self.headers), body)

        if path == "/v1/stats" and method == "GET":
            frame = dict(service.collector.latest())
            # This badge's revision. It refetches when the number moves, so a save for one badge
            # must not send the others after a layout nothing changed.
            frame["layout_rev"] = service.config.rev_for(badge_id)
            # A badge names the slow readings it holds, so a domain's traffic is not sent sixty
            # times a minute. The state travels in the query, leaving the host one frame for all
            # badges.
            #
            # Asking also marks the badge as able to read the answer; an older app gets every group
            # inline.
            held = self._query().get("have")
            if held is not None:
                slow = service.collector.slow_groups()
                for group in slow:
                    frame.pop(group, None)
                if frame.get("peaks"):
                    # A new dict: the one in the frame is the collector's, shared with every
                    # other badge.
                    frame["peaks"] = {ref: value
                                      for ref, value in frame["peaks"].items()
                                      if ref.split(".")[0] not in slow}
                if held != str(frame.get("slow_rev")):
                    # Under one key, so the badge can keep what it is handed without knowing
                    # which groups are the slow ones.
                    frame["slow"] = service.collector.slow_part()
            return self._json(200, frame)

        if path == "/v1/layout" and method == "GET":
            return self._json(200, service.config.for_badge(service.capabilities(),
                                                           badge_id))

        if path == "/v1/history" and method == "GET":
            query = self._query()
            keys = [k for k in (query.get("keys") or "").split(",") if k]
            points = max(1, min(160, int(query.get("points") or 48)))
            # v=2 carries the spacing of the points and the age of the newest, which puts them
            # on a time axis. v=3 adds `spacing` for each ring a source answers for itself,
            # those being on their own clock.
            #
            # Asked for and not assumed: an older app would hand the wrapper straight to a graph
            # and animate an hourly series as though it arrived every second.
            version = query.get("v")
            if version in ("2", "3"):
                return self._json(200, service.collector.history_at(
                    keys or None, points, spacing=version == "3"))
            return self._json(200, service.collector.history(keys or None, points))

        if path == "/v1/command" and method == "POST":
            payload = json.loads(body or b"{}")
            name = str(payload.get("cmd") or "")
            # Bound on this badge's layout: the buttons are configured per badge.
            allowed = {
                v for v in service.config.layout_for(badge_id).get("buttons", {}).values()
                if v
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

        # The palette a theme draws with, for the UI to preview. A tinted theme is derived here
        # and not in the browser, so the preview cannot drift from what reaches the badge.
        if path == "/api/theme" and method == "GET":
            query = self._query()
            # A retired name resolves here as it does on the way in, so a preview of one
            # shows what it now draws with rather than refusing it.
            theme, aliased = layout.resolve_theme(query.get("theme") or themes.DEFAULT, None)
            if theme not in layout.THEMES:
                return self._fail(400, f"unknown theme: {theme!r}")
            tint = aliased or layout.DEFAULT_CONFIG["tint"]
            if query.get("accent"):
                try:
                    wanted = [int(part) for part in query["accent"].split(",")[:3]]
                except ValueError:
                    return self._fail(400, "accent must be three numbers")
                tint = layout.tint_accent(wanted, tint)
            second = query.get("second") or "same"
            if second not in layout.ACCENT_B_RULES:
                return self._fail(400, f"unknown second accent rule: {second!r}")
            palette = layout.palette_for(theme, tint, second)
            # The two graph series resolved here too, by the badge's rule, so the preview
            # draws them without the browser carrying it.
            return self._json(200, {"theme": theme, "tint": tint, "second": second,
                                    "palette": palette,
                                    "series": layout.series_colours(palette)})

        # One layout per badge, and a default for a badge with nothing saved yet.
        # `?badge=` says whose; without it, the default.
        if path == "/api/config":
            whose = self._query().get("badge") or None
            if method == "GET":
                return self._json(200, service.config.layout_for(whose))
            if method == "PUT":
                if whose and whose not in service.badges.list_badges():
                    return self._fail(404, f"no badge {whose!r} is paired here")
                try:
                    rev = service.replace_config(json.loads(body or b"{}"), whose)
                except ValueError as exc:
                    return self._fail(400, str(exc))
                return self._json(200, {"rev": rev, "badge": whose})

        if path == "/api/stats" and method == "GET":
            return self._json(200, service.collector.latest())

        # The readings behind the preview's graph. The badge asks for its own over /v1; this
        # is the same rings, for a UI that is already loopback-only.
        if path == "/api/history" and method == "GET":
            query = self._query()
            keys = [key for key in (query.get("keys") or "").split(",") if key]
            points = max(1, min(160, int(query.get("points") or 48)))
            return self._json(200, service.collector.history(keys or None, points))

        if path == "/api/preview" and method == "GET":
            # What the badge would be sent, for the UI to show pruning.
            return self._json(200, service.config.for_badge(
                service.capabilities(), self._query().get("badge") or None))

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
            # With whether each has a layout of its own, which the badge picker has to say,
            # and the few things off that layout the list shows without opening it. The
            # layout is the merged one, so a badge on the default reports what it draws.
            configured = set(service.config.configured())
            listed = {}
            for badge_id, record in service.badges.list_badges().items():
                block = service.config.layout_for(badge_id)
                listed[badge_id] = dict(
                    record,
                    configured=badge_id in configured,
                    pages=len(block.get("pages") or ()),
                    theme=block.get("theme"),
                    interval_ms=block.get("interval_ms"),
                    # What was last seen on it against what an install would put there.
                    # None where nothing has been recorded for this badge yet.
                    app=service.app_state(badge_id),
                )
            return self._json(200, listed)

        if path.startswith("/api/badges/") and method == "PUT":
            badge_id = path[len("/api/badges/"):]
            name = str(json.loads(body or b"{}").get("name") or "").strip()
            if not service.badges.rename(badge_id, name):
                return self._fail(404, f"no badge {badge_id!r} is paired here")
            return self._json(200, {"name": name or badge_id})

        if path.startswith("/api/badges/") and method == "DELETE":
            badge_id = path[len("/api/badges/"):]
            # Its layout too, or it sits in the file naming an unreachable badge and is handed
            # to whatever next holds that id. Same for what it was last seen holding.
            service.config.forget(badge_id)
            pushed.forget(service.config_dir, badge_id)
            return self._json(200, {"forgotten": service.badges.forget(badge_id)})

        if path == "/api/extensions" and method == "GET":
            return self._json(200, service.extension_catalogue())

        # Asks an index, so it is its own request: the tab draws without waiting on it.
        if path == "/api/extensions/outdated" and method == "GET":
            return self._json(200, {"outdated": library.outdated(service.config_dir)})

        if path == "/api/extensions" and method == "POST":
            payload = json.loads(body or b"{}")
            verb = next((v for v in ("remove", "upgrade", "disable", "enable")
                         if v in payload), "add")
            asking = [str(name).strip()
                      for name in (payload.get(verb) or []) if str(name).strip()]
            # Naming nothing upgrades all of them; the others need a name.
            if not asking and verb != "upgrade":
                return self._fail(400, "name an extension to act on")
            if verb in ("disable", "enable"):
                return self._json(200, service.switch_extensions(verb, asking))
            # Minutes, in the worst case: uv resolves and downloads the whole environment.
            # One of the pool's threads waits on it; the badge keeps being served on
            # another.
            return self._json(200, service.change_extensions(verb, asking))

        # Polled while an install runs, and to tell whether a badge is plugged in.
        if path == "/api/install" and method == "GET":
            return self._json(200, service.install_state())

        # Writes WiFi credentials and can mint a pairing secret, both of which the
        # loopback-only rule above is what stands between this and the network.
        if path == "/api/install" and method == "POST":
            options = service.install_options(json.loads(body or b"{}"),
                                              self.server.server_address[1])
            if not service.start_install(options):
                return self._fail(409, "an install is already running")
            return self._json(200, service.install_state())

        return self._fail(404, "no such endpoint")

    # -- static -------------------------------------------------------------

    def _tokens(self):
        """The UI's accent and ramp, generated from the dark theme.

        The stylesheet used to carry these as hex typed in by hand, where a palette moving
        left them stale. The greys around them belong to the UI and stay there.
        """
        dark = themes.written()[themes.DEFAULT]

        def hexed(colour):
            red, green, blue = colour
            return f"#{red:02x}{green:02x}{blue:02x}"

        lines = [":root {", f"  --accent: {hexed(dark['accent'])};"]
        for at, colour in enumerate(rgb for _pos, rgb in dark["ramp"]):
            lines.append(f"  --ramp-{at}: {hexed(colour)};")
        lines.append("}")
        return self._send(200, ("\n".join(lines) + "\n").encode(), TYPES[".css"])

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
