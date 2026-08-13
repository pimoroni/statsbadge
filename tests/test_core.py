"""End-to-end checks for the server. Framing, auth, replay, config and pruning.

Run with `python3 -m pytest` from `server/`, or `python3 tests/test_core.py` for a
plain run with no pytest installed.
"""

import builtins
import contextlib
import html.parser
import importlib
import io
import json
import os
import struct
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# noqa: E402  the path above has to be set before these import
from statsbadge import (auth, extensions, identity, install, layout, library,
                        model, server, themes)


class FakeColour:
    """Enough of the badge's `color` for the app modules to be imported here.

    A theme holds `color` objects, so `look` cannot be imported without one. Only what the
    app calls of it, and only far enough to be compared: what the firmware does is measured
    on the badge, not here.
    """

    def __init__(self, r, g, b, a=255):
        self.r, self.g, self.b, self.a = int(r) & 255, int(g) & 255, int(b) & 255, a

    @classmethod
    def rgb(cls, r, g, b, a=255):
        return cls(r, g, b, a)

    def mix(self, other, t):
        part = t / 255.0
        return FakeColour(*(a + (b - a) * part
                            for a, b in ((self.r, other.r), (self.g, other.g),
                                         (self.b, other.b), (self.a, other.a))))

    def with_alpha(self, a):
        return FakeColour(self.r, self.g, self.b, a)

    def lighten(self, n):
        clamp = lambda v: max(0, min(255, v))  # noqa: E731
        return FakeColour(clamp(self.r + n), clamp(self.g + n), clamp(self.b + n), self.a)

    def darken(self, n):
        return self.lighten(-n)

    def to_oklch(self):
        # The app converts a palette's stops so the ramp interpolates perceptually. Only
        # a colour coming back matters here, so this stands in for the transform, which is
        # measured on the badge.
        return self

    def to_rgb(self):
        return self

    @staticmethod
    def ramp(stops, count):
        """`color.ramp`: count colours sampled across the stops, endpoints included."""
        out = []
        for step in range(count):
            fraction = step / (count - 1.0) if count > 1 else 0.0
            if fraction <= stops[0][0]:
                out.append(stops[0][1])
                continue
            for index in range(1, len(stops)):
                position, colour = stops[index]
                if fraction <= position:
                    previous, before = stops[index - 1]
                    span = position - previous
                    t = 0.0 if span <= 0 else (fraction - previous) / span
                    out.append(before.mix(colour, int(t * 255 + 0.5)))
                    break
            else:
                out.append(stops[-1][1])
        return out

    def over(self, background):
        return self.with_alpha(255).mix(background, 255 - self.a)

    def difference(self, other):
        """Near enough to order two candidates: sRGB distance scaled so black to white is 100.

        The firmware's is perceptual, and what it reports for a given pair is measured on the
        badge. This only has to put a clear difference above a threshold and a near match
        below it.
        """
        gap = sum((a - b) ** 2 for a, b in ((self.r, other.r), (self.g, other.g),
                                            (self.b, other.b))) ** 0.5
        return 100.0 * gap / (3 * 255 ** 2) ** 0.5

    def __eq__(self, other):
        return isinstance(other, FakeColour) and self.parts() == other.parts()

    def __hash__(self):
        return hash(self.parts())

    def parts(self):
        return (self.r, self.g, self.b, self.a)

    def __repr__(self):
        return "color.rgb({}, {}, {}, {})".format(*self.parts())


class FakeOutline:
    """Stands in for a shape, so the functions that build one can be called here. What it
    rasterises to is the badge's business; this only has to be handed around."""

    def __init__(self, points):
        self.points = list(points)

    def stroke(self, _weight, _flags=0, _miter_limit=4.0):
        return self


class FakeShape:
    """The stroke flags draw.py names at import, and enough of the rest to build a shape.
    Mirrors picovector's `stroke_flags_t`, though the values are free to change."""

    PATH_OPEN = 1 << 2
    ALIGN_CENTER = 2
    JOIN_MITER = 0
    CAP_BUTT = 0

    @staticmethod
    def custom(*contours):
        return FakeOutline(contours[0] if contours else ())


class FakeBrush:
    """`brush.gradient` far enough to see what a gauge was built from. What the firmware makes
    of the stops is measured on the badge; here they only have to be readable back."""

    CONICAL = "conical"
    LINEAR = "linear"
    RADIAL = "radial"

    def __init__(self, kind, points, stops):
        self.kind, self.points, self.stops = kind, points, list(stops)

    @classmethod
    def gradient(cls, kind, x1, y1, x2, y2, stops):
        return cls(kind, (x1, y1, x2, y2), stops)

    @staticmethod
    def erase():
        return "erase"


class FakeVec2:
    """A point, far enough to be built and read back. The rasterising is the badge's."""

    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)

    def __repr__(self):
        return f"vec2({self.x}, {self.y})"


# Where the badge finds them, and before anything imports look or draw.
builtins.color = FakeColour
builtins.shape = FakeShape
builtins.brush = FakeBrush
builtins.vec2 = FakeVec2

# MicroPython's tick helpers, which the app uses for every interval it measures. ticks_ms
# wraps on the badge and ticks_diff covers that; here the clocks are handed in by
# the tests, so subtraction covers it.
if not hasattr(time, "ticks_diff"):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b


class Harness:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="statsbadge-test-")
        self.service = server.Service(self.dir, interval=0.2)
        self.service.start()
        self.httpd = server.make_server(self.service, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.badge_id = "testbadge0001"
        self.secret = self.service.badges.provision(self.badge_id, "test")
        self.seq = 1000

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.service.stop()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def raw(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.url(path), data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def signed(self, method, path, payload=None, seq=None, secret=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        use_seq = self.seq if seq is None else seq
        if seq is None:
            self.seq += 1
        signature = auth.sign(secret or self.secret, method, path, use_seq, body)
        return self.raw(method, path, body or None, {
            auth.SIGNED_HEADER_ID: self.badge_id,
            auth.SIGNED_HEADER_SEQ: str(use_seq),
            auth.SIGNED_HEADER_SIG: signature,
            "Content-Type": "application/json",
        })


def _clear_pending(h):
    """Waiting requests count against the cap, so a test clears up after itself."""
    for request in h.service.badges.pending_enrolments():
        h.service.badges.deny_enrolment(request["request_id"])
    h.service.badges.cancel_pairing()


def _headers(badge_id, seq, secret, method="GET", path="/v1/stats", body=b""):
    return {
        auth.SIGNED_HEADER_ID: badge_id,
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: auth.sign(secret, method, path, seq, body),
    }


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def test_hello_is_open(h):
    """A badge must be able to find the host before it has a secret."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200, status
    assert body["server"] == "statsbadge"
    assert "layout_rev" in body


@check
def test_stats_needs_a_signature(h):
    status, body = h.raw("GET", "/v1/stats")
    assert status == 401, (status, body)
    assert "unsigned" in body["error"]


@check
def test_signed_stats(h):
    status, body = h.signed("GET", "/v1/stats")
    assert status == 200, (status, body)
    assert body["cpu"]["pct"] is not None
    assert body["sys"]["host"]
    assert "layout_rev" in body


@check
def test_bad_signature_is_refused(h):
    bogus = "00" * 32
    status, body = h.signed("GET", "/v1/stats", secret=bogus)
    assert status == 401, (status, body)
    assert "signature" in body["error"]


@check
def test_replay_is_refused(h):
    seq = h.seq
    h.seq += 1
    status, _ = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 200, status
    status, body = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 401, (status, body)
    assert "replay" in body["error"]


@check
def test_sequence_cannot_run_away(h):
    status, body = h.signed("GET", "/v1/stats", seq=h.seq + auth.SEQ_WINDOW + 10)
    assert status == 401, (status, body)
    assert "ahead" in body["error"]


@check
def test_unknown_badge_is_refused(h):
    saved = h.badge_id
    h.badge_id = "nosuchbadge"
    try:
        status, body = h.signed("GET", "/v1/stats")
        assert status == 403, (status, body)
    finally:
        h.badge_id = saved


@check
def test_path_is_signed(h):
    """A signature for one path is refused on another."""
    seq = h.seq
    h.seq += 1
    signature = auth.sign(h.secret, "GET", "/v1/stats", seq, b"")
    status, body = h.raw("GET", "/v1/layout", None, {
        auth.SIGNED_HEADER_ID: h.badge_id,
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: signature,
    })
    assert status == 401, (status, body)


@check
def test_layout_and_history(h):
    status, body = h.signed("GET", "/v1/layout")
    assert status == 200, status
    assert body["pages"], "layout should have pages"
    assert body["theme"] in layout.THEMES
    time.sleep(0.5)
    status, body = h.signed("GET", "/v1/history?keys=cpu.pct&points=8")
    assert status == 200, status
    assert "cpu.pct" in body, body

    # v=2 says where the points sit in time: their spacing, and how old the newest is. Without
    # it the old shape comes back, so an app copy older than this host is unaffected.
    status, aged = h.signed("GET", "/v1/history?keys=cpu.pct&points=8&v=2")
    assert status == 200, status
    assert aged["every_ms"] == 200, aged["every_ms"]
    assert 0 <= aged["age_ms"] <= 2000, aged["age_ms"]
    # The two are read a moment apart while the ring is still filling and the collector is
    # sampling every 200ms, so the second can hold one more point than the first. Any more than
    # that and the two rings differ.
    grew = len(aged["series"]["cpu.pct"]) - len(body["cpu.pct"])
    assert 0 <= grew <= 1, ("the same ring, said twice", grew)

    # Every ring gains a point per sample, whenever it started - a rate goes blank
    # on the first one - so positions counted back from the newest mean the same time in all of
    # them. A field that drops out gets a None, which keeps
    # that true and what a plot draws a gap for.
    before = {key: len(ring) for key, ring in h.service.collector.history(None, 160).items()}
    time.sleep(0.5)
    after = {key: len(ring) for key, ring in h.service.collector.history(None, 160).items()}
    grew = {after[key] - length for key, length in before.items()}
    assert len(grew) == 1, (before, after)


@check
def test_unbound_command_is_refused(h):
    status, body = h.signed("POST", "/v1/command", {"cmd": "lock"})
    assert status == 403, (status, body)
    assert "not bound" in body["error"]


@check
def test_config_api_roundtrip(h):
    status, config = h.raw("GET", "/api/config")
    assert status == 200, status
    before = config["rev"]
    config["theme"] = "mono"
    config["pages"] = [
        {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
         "readouts": ["cpu.freq"]},
    ]
    status, body = h.raw("PUT", "/api/config", json.dumps(config).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200, (status, body)
    assert body["rev"] == before + 1
    status, config = h.raw("GET", "/api/config")
    assert config["theme"] == "mono"
    assert len(config["pages"]) == 1


@check
def test_bad_config_is_rejected(h):
    status, body = h.raw("PUT", "/api/config",
                         json.dumps({"pages": [{"kind": "nonsense"}]}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 400, (status, body)
    assert "kind" in body["error"]


@check
def test_layout_rev_moves_on_change(h):
    _, before = h.signed("GET", "/v1/layout")
    status, config = h.raw("GET", "/api/config")
    config["theme"] = "vapor"
    h.raw("PUT", "/api/config", json.dumps(config).encode(),
          {"Content-Type": "application/json"})
    _, after = h.signed("GET", "/v1/layout")
    assert after["rev"] > before["rev"], (before["rev"], after["rev"])
    assert after["theme"] == "vapor"


@check
def test_response_is_one_write(h):
    """Why the framing exists: headers and body in a single segment.

    Reads with a short timeout after the first recv, so a body that arrives in a
    later segment shows up as a short read.
    """
    sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
    sock.sendall(b"GET /v1/hello HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
    first = sock.recv(65536)
    sock.close()
    assert b"\r\n\r\n" in first, "no header terminator in the first segment"
    head, _, body = first.partition(b"\r\n\r\n")
    length = int(dict(
        line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line
    )[b"Content-Length"])
    assert len(body) == length, (
        f"body split across segments: got {len(body)} of {length} bytes "
        "in the first read")


@check
def test_a_dropped_connection_is_not_reported(h):
    """SO_LINGER at 0 resets and does not close, matching the badge.

    The handler thread is parked in readline waiting for a following request, so the
    reset surfaces there with nothing in flight. Only a real fault gets a traceback.
    """
    sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
    sock.sendall(b"GET /v1/hello HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
    sock.recv(65536)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    caught = io.StringIO()
    stderr, sys.stderr = sys.stderr, caught
    try:
        sock.close()
        time.sleep(0.4)
    finally:
        sys.stderr = stderr
    assert caught.getvalue() == "", f"reported a dropped connection: {caught.getvalue()}"

    try:
        raise ValueError("a real handler fault")
    except ValueError:
        caught = io.StringIO()
        stderr, sys.stderr = sys.stderr, caught
        try:
            h.httpd.handle_error(None, ("127.0.0.1", 1))
        finally:
            sys.stderr = stderr
    assert "a real handler fault" in caught.getvalue(), "swallowed a real fault"


@check
def test_nodelay_is_set(_h):
    assert server.Handler.disable_nagle_algorithm is True


@check
def test_pruning_drops_absent_groups(_h):
    caps = {"available": {"cpu": ["pct"], "sys": ["host"]}}
    pages = layout.prune(layout.DEFAULT_PAGES, caps)
    ids = [p["id"] for p in pages]
    assert "cpu" in ids
    assert "gpu" not in ids, ids
    cpu = next(p for p in pages if p["id"] == "cpu")
    assert cpu["readouts"] == [], cpu


@check
def test_a_stale_precompile_is_not_what_gets_installed(_h):
    """Bytecode built before an edit loads fine and is the older program.

    Which looks like a change that did nothing, so the sources win instead.
    """
    import hashlib

    app = pathlib.Path(install.app_source_dir())
    digest = hashlib.sha256((app / "look.py").read_bytes()).hexdigest()
    built = pathlib.Path(tempfile.mkdtemp(prefix="statsbadge-mpy-"))
    (built / "look.mpy").write_bytes(b"M\x06\x00\x03")

    def bundled():
        return str(built)

    original = install.packaged_mpy_dir
    install.packaged_mpy_dir = bundled
    try:
        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": digest}}),
                                          encoding="utf-8")
        assert install._stale_modules(built) == []
        source, _note = install.choose_app_source(None, False, None)
        assert source == str(built), source

        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": "0" * 64}}),
                                          encoding="utf-8")
        assert install._stale_modules(built) == ["look.py"]
        source, note = install.choose_app_source(None, False, None)
        assert source is None, source
        assert "look.py" in note, note
    finally:
        install.packaged_mpy_dir = original


@check
def test_badge_provisioned_by_another_process_is_accepted(h):
    """`statsbadge install` writes badges.json while the server is already up.

    The running server holds a copy in memory, so without a reload the badge it
    just provisioned would get 403 "unknown badge" forever.
    """
    other = auth.Store(os.path.join(h.dir, "badges.json"))
    secret = other.provision("latecomer0001", "written by the CLI")
    seq = 500
    signature = auth.sign(secret, "GET", "/v1/stats", seq, b"")
    status, body = h.raw("GET", "/v1/stats", None, {
        auth.SIGNED_HEADER_ID: "latecomer0001",
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: signature,
    })
    assert status == 200, (status, body)


@check
def test_rotated_secret_is_picked_up(h):
    """`install --new-secret` replaces the secret of an already-known badge.

    The server keeps a copy in memory, so it re-reads on every verify. Uses a badge id of
    its own, leaving the shared harness counter alone.
    """
    who = "rotator00001"
    other = auth.Store(os.path.join(h.dir, "badges.json"))
    first = other.provision(who, "before")
    assert h.raw("GET", "/v1/stats", None, _headers(who, 10, first))[0] == 200

    time.sleep(0.01)
    other = auth.Store(os.path.join(h.dir, "badges.json"))
    second = other.provision(who, "after")
    assert second != first
    # The old secret must stop working, and the new one must start.
    status, _ = h.raw("GET", "/v1/stats", None, _headers(who, 11, first))
    assert status == 401, status
    status, body = h.raw("GET", "/v1/stats", None, _headers(who, 11, second))
    assert status == 200, (status, body)


@check
def test_counter_refusal_offers_a_resync(h):
    """A refusal over the counter must say what to use next.

    The badge cannot guess: too low is refused as a replay, too high as out of window. The
    signature is verified before this check, so telling the caller is safe.
    """
    who = "resyncer00001"
    other = auth.Store(os.path.join(h.dir, "badges.json"))
    time.sleep(0.01)
    secret = other.provision(who, "resync")
    assert h.raw("GET", "/v1/stats", None, _headers(who, 700, secret))[0] == 200

    # Too low: a replay, with advice.
    status, body = h.raw("GET", "/v1/stats", None, _headers(who, 5, secret))
    assert status == 401 and "replay" in body["error"], (status, body)
    assert body.get("next_seq") == 701, body

    # Too high: outside the window, with the same advice.
    ahead = 700 + auth.SEQ_WINDOW + 50
    status, body = h.raw("GET", "/v1/stats", None, _headers(who, ahead, secret))
    assert status == 401 and "ahead" in body["error"], (status, body)
    assert body.get("next_seq") == 701, body

    # Following the advice works.
    status, body = h.raw("GET", "/v1/stats", None,
                         _headers(who, body["next_seq"], secret))
    assert status == 200, (status, body)


@check
def test_reload_never_lowers_a_counter(h):
    """A stale file leaves the counter where it is, or a replay would get through."""
    seq = h.seq
    h.seq += 1
    assert h.signed("GET", "/v1/stats", seq=seq)[0] == 200
    # Rewrite the file with seq=0 for this badge, as a stale copy would have it.
    path = os.path.join(h.dir, "badges.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    data["badges"][h.badge_id]["seq"] = 0
    time.sleep(0.01)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    h.service.badges._reload_if_changed()
    status, body = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 401, (status, body)
    assert "replay" in body["error"], body


@check
def test_counter_is_persisted(h):
    """The counter must reach disk, or a restart makes old requests replayable.

    Batched, a badge polling once a second, so this drives it past the
    threshold and checks the file caught up.
    """
    who = "persister0001"
    other = auth.Store(os.path.join(h.dir, "badges.json"))
    time.sleep(0.01)
    secret = other.provision(who, "persist")
    path = os.path.join(h.dir, "badges.json")

    seq = 10
    for _ in range(auth.SEQ_PERSIST_EVERY + 2):
        seq += 1
        assert h.raw("GET", "/v1/stats", None, _headers(who, seq, secret))[0] == 200

    # The guarantee is that disk stays within the threshold of memory.
    with open(path, encoding="utf-8") as handle:
        on_disk = json.load(handle)["badges"][who]["seq"]
    assert on_disk > 0, "the counter never reached disk"
    assert on_disk >= seq - auth.SEQ_PERSIST_EVERY, (on_disk, seq)

    # A fresh Store, standing in for a restarted server, must refuse a used counter.
    restarted = server.Service(h.dir, interval=0.2)
    try:
        assert restarted.badges.badges[who]["seq"] == on_disk
        try:
            restarted.badges.verify(
                "GET", "/v1/stats",
                {k.lower(): v for k, v in _headers(who, 11, secret).items()}, b"")
        except auth.AuthError as exc:
            assert "replay" in exc.reason, exc.reason
        else:
            raise AssertionError("a restarted server accepted a used counter")
    finally:
        restarted.collector.stop()


@check
def test_server_identity_is_stable(h):
    """A badge keys credentials on this, so it must survive a restart."""
    first = identity.load(h.dir)
    again = identity.load(h.dir)
    assert first["id"] == again["id"], "the id changed between loads"
    assert len(first["id"]) >= 16
    assert first["name"]


@check
def test_pairing_is_off_until_asked_for(h):
    """A server leaves pairing mode closed: it is opened on request and closed
    again, from the UI or by running out of time."""
    h.service.badges.cancel_pairing()
    state = h.raw("GET", "/api/pair")[1]
    assert state["active"] is False, state
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is False

    # A badge cannot ask while it is shut.
    status, body = h.raw("POST", "/v1/enrol",
                         json.dumps({"badge_id": "early"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 403 and "not open" in body["error"], body

    opened = h.raw("POST", "/api/pair", b"")[1]
    assert opened["active"] is True
    state = h.raw("GET", "/api/pair")[1]
    assert state["active"] is True
    assert 0 < state["expires_in"] <= 300
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is True

    # Closing it early is the other half of the control.
    h.raw("DELETE", "/api/pair")
    assert h.raw("GET", "/api/pair")[1]["active"] is False
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is False


@check
def test_hello_carries_the_identity(h):
    """The badge keys credentials on this."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200
    assert body["id"] == h.service.identity["id"], body
    assert body["name"] == h.service.identity["name"], body


@check
def test_enrolment_needs_an_open_window(h):
    h.service.badges.cancel_pairing()
    status, body = h.raw("POST", "/v1/enrol",
                         json.dumps({"badge_id": "asker0001"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 403 and "not open" in body["error"], body


@check
def test_enrolment_needs_a_human(h):
    """A request alone pairs nothing; approving it does."""
    h.service.badges.begin_pairing(ttl=60)
    status, asked = h.raw("POST", "/v1/enrol",
                          json.dumps({"badge_id": "asker0002", "name": "tufty"}).encode(),
                          {"Content-Type": "application/json"})
    assert status == 200, (status, asked)
    assert len(asked["code"]) == auth.ENROL_CODE_HEX, asked
    assert asked["id"] == h.service.identity["id"]

    # Still empty.
    status, outcome = h.raw("GET", f"/v1/enrol/{asked['request_id']}")
    assert status == 200 and outcome["status"] == "pending", outcome
    assert "asker0002" not in h.service.badges.list_badges()

    # It shows up for a human, with the code the badge is displaying.
    pending = h.raw("GET", "/api/enrol")[1]["pending"]
    mine = [p for p in pending if p["badge_id"] == "asker0002"]
    assert len(mine) == 1 and mine[0]["code"] == asked["code"], pending

    h.raw("POST", f"/api/enrol/{asked['request_id']}/approve", b"")
    status, outcome = h.raw("GET", f"/v1/enrol/{asked['request_id']}")
    assert status == 200 and outcome["status"] == "approved", outcome
    assert len(outcome["secret"]) == 64
    assert "asker0002" in h.service.badges.list_badges()

    # The secret is handed over once.
    assert h.raw("GET", f"/v1/enrol/{asked['request_id']}")[1]["status"] == "gone"

    # It actually works.
    seq = 50
    signature = auth.sign(outcome["secret"], "GET", "/v1/stats", seq, b"")
    status, _ = h.raw("GET", "/v1/stats", None, {
        auth.SIGNED_HEADER_ID: "asker0002",
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: signature,
    })
    assert status == 200, status


@check
def test_denied_enrolment_pairs_nothing(h):
    h.service.badges.begin_pairing(ttl=60)
    asked = h.raw("POST", "/v1/enrol",
                  json.dumps({"badge_id": "asker0003"}).encode(),
                  {"Content-Type": "application/json"})[1]
    h.raw("POST", f"/api/enrol/{asked['request_id']}/deny", b"")
    assert h.raw("GET", f"/v1/enrol/{asked['request_id']}")[1]["status"] == "gone"
    assert "asker0003" not in h.service.badges.list_badges()


@check
def test_codes_are_unique_per_request(h):
    """Two badges waiting must be distinguishable, or approving is a coin toss."""
    h.service.badges.begin_pairing(ttl=120)
    codes = set()
    for i in range(3):
        h.service.badges.pairing["not_before"] = 0.0
        asked = h.raw("POST", "/v1/enrol",
                      json.dumps({"badge_id": f"unique{i}"}).encode(),
                      {"Content-Type": "application/json"})[1]
        codes.add(asked["code"])
    assert len(codes) == 3, codes
    # Minted per request, the badge id being public.
    for i, code in enumerate(codes):
        assert f"unique{i}" not in code.lower()
    _clear_pending(h)


@check
def test_enrolment_is_rate_limited(h):
    h.service.badges.begin_pairing(ttl=120)
    body = json.dumps({"badge_id": "flooder"}).encode()
    headers = {"Content-Type": "application/json"}
    first = h.raw("POST", "/v1/enrol", body, headers)
    assert first[0] == 200, first
    # The same badge asking again gets the request it already has.
    again = h.raw("POST", "/v1/enrol", body, headers)
    assert again[1]["request_id"] == first[1]["request_id"], (first, again)
    # A different badge, straight away, is throttled.
    status, throttled = h.raw("POST", "/v1/enrol",
                              json.dumps({"badge_id": "flooder2"}).encode(), headers)
    assert status == 429, (status, throttled)
    assert throttled.get("retry_after") is not None, throttled
    _clear_pending(h)


@check
def test_pending_requests_are_capped(h):
    _clear_pending(h)
    h.service.badges.begin_pairing(ttl=300)
    headers = {"Content-Type": "application/json"}
    accepted = 0
    for i in range(auth.MAX_PENDING + 3):
        h.service.badges.pairing["not_before"] = 0.0
        status, _ = h.raw("POST", "/v1/enrol",
                          json.dumps({"badge_id": f"crowd{i}"}).encode(), headers)
        if status == 200:
            accepted += 1
    assert accepted == auth.MAX_PENDING, accepted
    _clear_pending(h)


@check
def test_config_api_is_loopback_only(_h):
    """The config API can mint secrets, so it answers on loopback alone.

    Checked at the handler level: binding a second address to prove it is awkward,
    but the guard is the part under test.
    """
    assert "loopback" in _source_of(server.Handler._dispatch)


@check
def test_write_secrets_keeps_the_rest_of_the_file(_h):
    """Setting WiFi details leaves the other settings and their comments as they were."""
    import tempfile

    from statsbadge import install

    template = ('WIFI_SSID = ""\n'
                'WIFI_PASSWORD = ""\n'
                'REGION = "eu"  # Options are us, cuba, eu, moldova\n'
                'TIMEZONE = 0  # Offset from GMT as number of hours\n')
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        path = os.path.join(volume, "system", "secrets.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(template)

        assert install.secrets_file(volume) == path
        assert not install.wifi_configured(volume)

        # A backslash and quotes in the password: a naive regex replacement writes these
        # back out as escapes and the file stops being valid Python.
        password = 'p@ss "w0rd"\\'
        install.write_secrets(volume, "Some Network", password, region="us")

        with open(path, encoding="utf-8") as handle:
            after = handle.read()
        values = {}
        exec(compile(after, "secrets.py", "exec"), values)
        assert values["WIFI_SSID"] == "Some Network"
        assert values["WIFI_PASSWORD"] == password
        assert values["REGION"] == "us"
        assert values["TIMEZONE"] == 0, "an untouched setting was lost"
        assert "Options are us" in after, "REGION's comment was dropped"
        assert install.wifi_configured(volume)

        # Writing again replaces, and does not append a second WIFI_SSID.
        install.write_secrets(volume, "Other", "pw")
        with open(path, encoding="utf-8") as handle:
            after = handle.read()
        assert after.count("WIFI_SSID") == 1
        values = {}
        exec(compile(after, "secrets.py", "exec"), values)
        assert values["WIFI_SSID"] == "Other"
        assert values["TIMEZONE"] == 0

        # A key the file lacks is appended.
        install.write_secrets(volume, "Third", "pw", timezone=-7)
        with open(path, encoding="utf-8") as handle:
            values = {}
            exec(compile(handle.read(), "secrets.py", "exec"), values)
        assert values["TIMEZONE"] == -7


@check
def test_app_files_and_pruning(_h):
    """What goes on the badge, and what an update has to take off it."""
    import tempfile

    from statsbadge import install

    with tempfile.TemporaryDirectory() as work:
        source = os.path.join(work, "built")
        os.makedirs(os.path.join(source, "mpy"))
        for name in ("__init__.mpy", "net.mpy", "icon.png", "MPY_VERSION",
                     "BUILD_INFO", ".hidden"):
            with open(os.path.join(source, name), "w", encoding="utf-8") as handle:
                handle.write(name)
        with open(os.path.join(source, "mpy", "stowaway.mpy"), "w", encoding="utf-8") as handle:
            handle.write("not this one either")
        plugin = os.path.join(work, "clockface.py")
        with open(plugin, "w", encoding="utf-8") as handle:
            handle.write("# a badge-side extension module")

        names = dict(install.app_files(source, [("clock", plugin)]))
        assert sorted(names) == ["__init__.mpy", "ext/clockface.py", "icon.png",
                                 "net.mpy"], names

        # A stale .py beside an .mpy is the one that matters: it wins the import and
        # silently undoes the precompile.
        target = os.path.join(work, "stats")
        os.makedirs(os.path.join(target, "ext"))
        for name in ("__init__.mpy", "net.mpy", "net.py", "icon.png", "notes.txt"):
            with open(os.path.join(target, name), "w", encoding="utf-8") as handle:
                handle.write("old")
        for name in ("clockface.py", "gone.py"):
            with open(os.path.join(target, "ext", name), "w", encoding="utf-8") as handle:
                handle.write("old")

        removed = install.prune_app(target, set(names))
        assert removed == ["ext/gone.py", "net.py"], removed
        assert os.path.exists(os.path.join(target, "notes.txt")), \
            "pruning took a file the installer does not own"
        assert os.path.exists(os.path.join(target, "net.mpy"))

        installed = {"__init__.mpy": "aaa", "net.mpy": "bbb", "net.py": "ccc",
                     "icon.png": "ddd", "notes.txt": "eee"}
        desired = {"__init__.mpy": "aaa", "net.mpy": "CHANGED", "icon.png": "ddd",
                   "ext/clockface.py": "fff"}
        added, changed, gone = install.app_changes(installed, desired)
        assert added == ["ext/clockface.py"], added
        assert changed == ["net.mpy"], changed
        assert gone == ["net.py"], f"{gone}, and notes.txt must not force a reset"


@check
def test_unreadable_badge_store_is_not_treated_as_empty(_h):
    """An unreadable store raises, and is left where it is."""
    import tempfile

    from statsbadge import auth

    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "badges.json")
        store = auth.Store(path)
        store.provision("badge-1", "test")
        assert store.list_badges()

        os.chmod(path, 0o000)
        try:
            reopened = auth.Store(path)
            if reopened.unreadable is None:
                # Root, or Windows, where a mode is only a read-only bit and the file is
                # readable whatever it says.
                return
            assert reopened.list_badges() == {}
            try:
                reopened.save()
            except PermissionError:
                pass
            else:
                raise AssertionError("saved over a store it could not read")
        finally:
            os.chmod(path, 0o600)
        # The real records are still there.
        assert auth.Store(path).list_badges()


@check
def test_extensions_describe_finds_the_clock(h):
    from statsbadge import extensions

    found = {record["name"]: record for record in extensions.describe()}
    clock = found.get("clock")
    if clock is None:
        return              # the extension is not pip installed in this environment
    assert clock["loaded"], clock
    assert clock["badge_module"] == "clockface.py", clock
    assert "clock" in clock["provides"], clock

    # The UI gets all of them, not only the ones with settings: an extension that asks to be
    # told nothing had no box on the page, and a failed import went unreported.
    _status, caps = h.raw("GET", "/api/capabilities")
    described = {record["name"] for record in caps["extensions"]}
    assert described == set(found), (described, set(found))
    web = pathlib.Path("src/statsbadge/web")
    assert 'id="extensions"' in (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    assert "caps.extensions" in script, "the UI still lists only what has settings"
    assert "extensionBox" in script, "an extension is not a box of its own"


@check
def test_extension_settings_are_declared_stored_and_applied(h):
    """The UI can only offer what an extension declares, and a save has to reach it."""
    status, caps = h.raw("GET", "/api/capabilities")
    assert status == 200, status
    schema = caps.get("extension_settings") or {}
    if "clock" not in schema:
        return              # the clock extension is not pip installed here
    keys = {entry["key"] for entry in schema["clock"]}
    assert {"latitude", "longitude"} <= keys, keys

    _status, config = h.raw("GET", "/api/config")
    config["settings"] = {"clock": {"latitude": "52.4", "longitude": "-1.9",
                                    "units": "fahrenheit"}}
    status, _body = h.raw("PUT", "/api/config", json.dumps(config).encode(),
                          {"Content-Type": "application/json"})
    assert status == 200, status

    # Coerced to the declared type, not stored as the strings a form sends
    _status, stored = h.raw("GET", "/api/config")
    assert stored["settings"]["clock"]["latitude"] == 52.4, stored["settings"]
    assert stored["settings"]["clock"]["units"] == "fahrenheit", stored["settings"]

    # and handed to the running source, not left for the next restart
    clock = next(s for s in h.service.collector.extensions if s.name == "clock")
    assert clock.latitude == 52.4, clock.latitude
    assert clock.units == "fahrenheit", clock.units

    # Host-side only: a location is no business of the badge's
    _status, sent = h.raw("GET", "/api/preview")
    assert "settings" not in sent, sorted(sent)


@check
def test_undeclared_settings_are_dropped_but_absent_extensions_are_kept(_h):
    """A key nobody asked for goes; a whole block for an extension still to load
    stays, or disabling one would be what deletes its configuration."""
    schema = {"clock": [{"key": "latitude", "type": "number"}]}
    incoming = {**layout.DEFAULT_CONFIG, "settings": {
        "clock": {"latitude": "1.5", "sneaky": "no"},
        "notloaded": {"token": "keep me"},
    }}
    stored = layout.validate(incoming, (), schema)["settings"]
    assert stored["clock"] == {"latitude": 1.5}, stored
    assert stored["notloaded"] == {"token": "keep me"}, stored

    # An empty field clears a setting, and comes through as unset
    incoming["settings"]["clock"] = {"latitude": ""}
    cleared = layout.validate(incoming, (), schema)["settings"]
    assert cleared["clock"]["latitude"] is None, cleared


@check
def test_an_extension_page_survives_without_fields(_h):
    """A map page draws from its extension's group and declares no fields, so there is
    nothing in the host's field list to confirm it by.

    Pruned on that list alone it stopped at the host, and the UI said the host reported
    no data for it - while the same page added from the browser, which carries
    `from_extension`, was sent. A page is sent once its extension is installed.
    """
    capabilities = {"available": {"cpu": ["pct"]},
                    "extension_pages": [{"kind": "quakemap", "from_extension": "quakes"}]}
    pages = [{"id": "cpu", "kind": "dial", "field": "cpu.pct"},
             {"id": "quakes", "kind": "quakemap", "fields": []},
             {"id": "uninstalled", "kind": "othermap", "fields": []}]
    kept = [page["id"] for page in layout.prune(pages, capabilities)]
    assert kept == ["cpu", "quakes"], kept


@check
def test_a_declared_group_is_offered_kept_and_recorded(h):
    """What an extension declares has to reach the pickers, the rings and the peaks.

    A group that arrives with a pip install is in none of the model's tables, so without
    this an extension's readings cannot be chosen in the UI and a page drawing one is
    pruned before it reaches the badge.
    """
    from statsbadge.sources.base import Source

    class Site(Source):
        name = "site"
        provides = ("site",)
        groups = {"site": {"label": "Example.com", "fields": {
            "hits": {"label": "Hits a minute", "unit": "/min", "graphed": True,
                     "peak": True, "peak_floor": 10.0},
            "cached_pct": {"label": "Cached %", "unit": "%", "percent": True},
        }}}

        def sample(self, frame, _dt):
            frame["site"] = {"hits": 40.0, "cached_pct": 62.0}

    collector = h.service.collector
    collector.extensions.append(Site({}))
    collector.sample_once()
    caps = collector.capabilities()

    assert caps["available"]["site"] == ["cached_pct", "hits"], caps["available"]
    assert caps["group_labels"]["site"] == "Example.com"
    assert caps["field_labels"]["site"]["hits"] == "Hits a minute"
    assert "cached_pct" in caps["percent_fields"], caps["percent_fields"]
    assert "site.hits" in caps["graphed"], caps["graphed"]

    # A ring, so a graph of it plots something, and a peak, so a gauge has a top end
    assert collector.history(["site.hits"])["site.hits"][-1] == 40.0
    assert collector.latest()["peaks"]["site.hits"] == 40, collector.latest()["peaks"]

    # and the page survives pruning, which reads the same list the pickers do
    page = {"id": "s", "kind": "dial", "field": "site.hits", "readouts": []}
    assert layout.prune([page], caps) == [page]


@check
def test_a_slow_group_travels_only_when_it_changes(h):
    """A reading fetched once a minute should not be sent sixty times.

    Six domains took a frame from 832 bytes to 4.7KB, all of it standing still between the
    host's fetches. The badge says which revision it holds and the host leaves those
    groups out; asking at all marks it as able to read them, so an app too old
    to ask still gets every group inline.
    """
    from statsbadge.sources.base import Source

    class Feed(Source):
        name = "feed"
        groups = {"feed": {"label": "A feed", "slow": True, "fields": {
            "hits": {"label": "Hits", "peak": True, "peak_floor": 1.0}}}}

        def __init__(self, config):
            super().__init__(config)
            self.hits = 10.0

        def sample(self, frame, _dt):
            frame["feed"] = {"hits": self.hits}

    feed = Feed({})
    collector = h.service.collector
    collector.extensions.append(feed)
    collector.sample_once()
    assert "feed" in collector.slow_groups(), collector.slow_groups()

    def stats(query=""):
        status, body = h.signed("GET", f"/v1/stats{query}")
        assert status == 200, (status, body)
        return body

    # An app that does not ask gets it inline, exactly as before any of this
    assert "feed" in stats(), "an app that cannot merge was sent a split frame"

    # Asking, and behind: the group arrives under one key, so the badge keeps what it is
    # handed without having to know which of the frame's groups are the slow ones
    first = stats("?have=-1")
    rev = first["slow_rev"]
    assert "feed" not in first, sorted(first)
    assert first["slow"]["feed"] == {"hits": 10.0}, first["slow"]
    # The peak scales the reading, so it travels with it, on the slow half
    assert first["slow"]["peaks"] == {"feed.hits": 10}, first["slow"]

    # Asking, and up to date, so the group and its peak both stay
    collector.sample_once()
    lean = stats(f"?have={rev}")
    assert "slow" not in lean and "feed" not in lean, sorted(lean)
    assert "feed.hits" not in (lean.get("peaks") or {}), lean.get("peaks")
    assert lean["slow_rev"] == rev, "a reading that did not move revised itself"

    # When the reading moves, the revision does, and the next poll carries it
    feed.hits = 40.0
    collector.sample_once()
    moved = stats(f"?have={rev}")
    assert moved["slow_rev"] == rev + 1, (moved["slow_rev"], rev)
    assert moved["slow"]["feed"] == {"hits": 40.0}, moved["slow"]

    # The badge's side: what it holds goes back into every frame after the one that
    # carried it, and `peaks` merges into the fast ones, keeping both.
    sys.path.insert(0, install.app_source_dir())
    import pages

    held = moved.pop("slow")
    later = stats(f"?have={moved['slow_rev']}")
    fast_peaks = dict(later.get("peaks") or {})
    pages.merge_slow(later, held)
    assert later["feed"] == {"hits": 40.0}, later.get("feed")
    assert later["peaks"]["feed.hits"] == 40, later["peaks"]
    for ref, value in fast_peaks.items():
        assert later["peaks"][ref] == value, f"{ref} was lost to the merge"

    collector.extensions.remove(feed)


@check
def test_a_declared_group_is_named_on_the_badge_too(_h):
    """A badge names a reading after its field, and after its group where one page draws the
    same field from several. That comes out CF_GADGETOID_COM for a group named after a domain:
    the badge has only the key, and the dots cannot be put back. So the host's name for it
    travels with the layout, where a name somebody chose belongs."""
    sys.path.insert(0, install.app_source_dir())
    import pages

    caps = {"available": {"cf_a_com": ["requests"], "cf_b_com": ["requests"],
                          "cpu": ["pct"]},
            "group_source": {"cf_a_com": "Cloudflare", "cf_b_com": "Cloudflare"},
            "group_labels": {"cf_a_com": "a.com", "cf_b_com": "b.com",
                             "cpu": "Processor"}}
    page = {"id": "s", "kind": "spark",
            "fields": ["cf_a_com.requests", "cf_b_com.requests"]}
    labels = layout.group_labels([page], caps)
    assert labels == {"cf_a_com": "a.com", "cf_b_com": "b.com"}, labels

    # The model's groups are left out: "Processor" is read at a desk, the badge says CPU.
    assert layout.group_labels([{"kind": "dial", "field": "cpu.pct"}], caps) == {}

    was = pages.LABELS
    try:
        pages.LABELS = labels
        # Two readings of the same field, told apart by the group as the reader named it
        assert pages.names_for(page["fields"]) == ["a.com", "b.com"]
    finally:
        pages.LABELS = was
    # Absent that, the key in the case the rest of the furniture is in
    assert pages.names_for(page["fields"]) == ["CF_A_COM", "CF_B_COM"]


@check
def test_a_bar_can_be_named_by_whoever_sent_it(h):
    """Bars number their lanes, which suits a core and is no use for a domain.

    A source sends the names beside the values, the way `peaks` are sent beside the readings
    they scale. Nothing has to declare the companion: the picker offers the list field, and
    the names ride along in the frame where only the renderer looks for them.
    """
    from statsbadge.sources.base import Source

    class Domains(Source):
        name = "domains"
        provides = ("edge",)
        groups = {"edge": {"label": "Edge", "fields": {
            "cached": {"label": "Cached % per domain", "percent": True, "list": True}}}}

        def sample(self, frame, _dt):
            frame["edge"] = {"cached": [87.0, 74.5],
                             "cached_names": ["a.com", "b.com"]}

    source = Domains({})
    collector = h.service.collector
    collector.extensions.append(source)
    try:
        collector.sample_once()
        caps = collector.capabilities()
        assert "cached" in caps["list_fields"], caps["list_fields"]
        assert "cached_names" not in caps["list_fields"], caps["list_fields"]
        assert "edge.cached_names" not in caps["available"], "the names were offered as a field"

        sys.path.insert(0, install.app_source_dir())
        import pages

        frame = collector.latest()
        assert pages.value_of(frame, "edge.cached") == [87.0, 74.5]
        assert pages.value_of(frame, "edge.cached" + pages.LANE_NAMES) == ["a.com", "b.com"]
    finally:
        collector.extensions.remove(source)

    source_text = pathlib.Path(install.app_source_dir(), "pages.py").read_text(encoding="utf-8")
    body = source_text[source_text.index("def _bars"):source_text.index("def behind_at")]
    assert "LANE_NAMES" in body, "_bars numbers its lanes whatever the source sent"


@check
def test_stored_settings_beat_the_command_line(_h):
    merged = layout.merge_settings({"clock": {"latitude": 1.0, "units": "celsius"}},
                                   {"clock": {"latitude": 52.4}})
    assert merged["clock"] == {"latitude": 52.4, "units": "celsius"}, merged


@check
def test_every_field_has_a_name_for_the_ui(_h):
    """The pickers show these, so a field with none shows a column name instead."""
    from statsbadge import model

    described = model.describe()
    for group, fields in model.GROUPS.items():
        assert group in described["group_labels"], group
        for field in fields:
            assert described["field_labels"].get(group, {}).get(field), (group, field)


@check
def test_a_dials_page_takes_up_to_four_fields(_h):
    base = dict(layout.DEFAULT_CONFIG)
    refs = ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct", "cpu.temp"]

    def kept(count):
        page = {"id": "g", "kind": "dials", "title": "Load", "fields": refs[:count]}
        return layout.validate({**base, "pages": [page]})["pages"][0]["fields"]

    for count in (1, 2, 3, 4):
        assert len(kept(count)) == count, count
    assert len(kept(5)) == 4, "a fifth gauge has nowhere to go"

    try:
        layout.validate({**base, "pages": [{"id": "g", "kind": "dials", "fields": []}]})
        raise AssertionError("a page with no fields should be refused")
    except ValueError:
        pass

    # Pruned like any multi-field page: what the host cannot answer goes, and the page
    # stays for what is left
    caps = {"available": {"cpu": ["pct"], "mem": ["pct"]}}
    page = {"id": "g", "kind": "dials",
            "fields": ["cpu.pct", "gpu.pct", "mem.pct"]}
    assert layout.prune([page], caps)[0]["fields"] == ["cpu.pct", "mem.pct"]


@check
def test_every_kind_has_a_badge_layout_and_a_ui_shape(_h):
    """A kind the server accepts has to be drawable and configurable, or it is a page
    that validates, reaches the badge and shows a message saying it cannot be drawn."""
    app = pathlib.Path(install.app_source_dir())
    pages_source = (app / "pages.py").read_text(encoding="utf-8")
    ui_source = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
                 / "app.js").read_text(encoding="utf-8")
    markup = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
              / "index.html").read_text(encoding="utf-8")
    for kind in layout.KINDS:
        assert f'"{kind}": _' in pages_source, f"{kind} has no renderer"
        assert f"  {kind}: {{" in ui_source, f"{kind} has no shape in the UI"
        assert f'value="{kind}"' in markup, f"{kind} is not in the kind picker"


@check
def test_a_full_scale_is_offered_where_it_is_read(_h):
    """A page carries where full is, and the UI has to offer that for exactly the kinds whose
    renderer looks at it. Offered too widely it is a control that does nothing; too narrowly
    and a page of counts is stuck being scaled by the busiest reading the host has seen."""
    app = pathlib.Path(install.app_source_dir())
    pages_source = (app / "pages.py").read_text(encoding="utf-8")
    ui_source = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
                 / "app.js").read_text(encoding="utf-8")

    # fraction_of reads the page's max for its caller, so those kinds count too.
    reads = set()
    for kind in layout.KINDS:
        start = pages_source.find(f"def _{kind}(")
        if start < 0:
            continue
        end = pages_source.find("\ndef ", start + 1)
        body = pages_source[start:end if end > 0 else len(pages_source)]
        if 'page.get("max")' in body or 'page["max"]' in body:
            reads.add(kind)
        elif "fraction_of(ref, value, page, frame)" in body:
            reads.add(kind)

    offered = ui_source[ui_source.index("const SCALED"):]
    offered = set(re.findall(r'"([a-z]+)"', offered[:offered.index(")")]))
    assert offered == reads, f"UI offers {sorted(offered)}, renderers read {sorted(reads)}"

    def scaled(value):
        stored = layout.validate({**layout.DEFAULT_CONFIG,
                                  "pages": [{"id": "b", "kind": "bars",
                                             "field": "cpu.cores", "max": value}]})
        return stored["pages"][0].get("max")

    assert scaled(1200) == 1200.0
    assert scaled("250") == 250.0
    assert scaled(0) is None and scaled(-5) is None, "a full scale of nothing was stored"
    assert scaled("nonsense") is None and scaled(None) is None


@check
def test_caselights_take_a_field_or_a_flag(_h):
    """Three settings in one value: off, the backlight's level, or a reading to follow."""
    base = dict(layout.DEFAULT_CONFIG)

    def stored(value):
        return layout.validate({**base, "caselights": value})["caselights"]

    # Offered as following the backlight now, though the flag is still stored as it was.
    page = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    assert "Follow the Backlight" in page and "Follow the Theme" not in page

    assert stored("cpu.pct") == "cpu.pct"
    assert stored(True) is True
    assert stored(False) is False
    # Anything other than a "group.field" falls back to a flag, and stops before the
    # badge as a reference it cannot look up.
    assert stored("bogus") is True
    assert stored("too.many.dots") is True
    assert stored(None) is False


@check
def test_an_extension_page_can_be_added_and_reaches_the_badge(h):
    """The UI's kind picker is built from this, and the config it PUTs has to validate.

    Without it the page an extension offers is unreachable. The server carries it, the
    badge can draw it, and nothing offers it to a reader.
    """
    status, caps = h.raw("GET", "/api/capabilities")
    assert status == 200, status
    offered = caps.get("extension_pages") or []
    if not offered:
        return              # no extension is pip installed in this environment
    page = offered[0]
    assert page.get("kind") and page.get("title"), page
    assert page["kind"] not in layout.KINDS, page

    _status, config = h.raw("GET", "/api/config")
    config["pages"].append({**page, "id": page["kind"] + "test"})
    status, saved = h.raw("PUT", "/api/config", json.dumps(config).encode(),
                          {"Content-Type": "application/json"})
    assert status == 200, (status, saved)

    _status, sent = h.raw("GET", "/api/preview")
    kinds = [p["kind"] for p in sent["pages"]]
    assert page["kind"] in kinds, kinds


@check
def test_icon_font_corpus_and_packing(_h):
    """The icon font tool's parsing and packing, which need no font libraries."""
    import sys
    import tempfile

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import make_icon_font as tool

    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "icons.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# a comment\n"
                         "\n"
                         "sunny e81a\n"
                         "rainy f176 i    # trailing comment\n")
        assert tool.read_corpus(path) == [("sunny", 0xE81A, None),
                                          ("rainy", 0xF176, ord("i"))]

        for bad, why in (("sunny\n", "one field"),
                         ("sunny nothex\n", "bad codepoint"),
                         ("sunny e81a ab\n", "two-character remap")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(bad)
            try:
                tool.read_corpus(path)
            except SystemExit:
                continue
            raise AssertionError(f"accepted a line with a {why}")

    # Points and the advance are signed bytes, so anything outside is clamped and the
    # caller gets told which glyphs were affected.
    glyph = tool.Glyph(ord("a"))
    glyph.contours = [[(0, 0), (200, -50), (10, -300)]]
    assert tool.out_of_range([glyph]) == [ord("a")]

    ok = tool.Glyph(ord("b"))
    ok.contours = [[(0, 0), (90, 0), (90, -90), (0, -90), (0, 0)]]
    ok.bbox_w = ok.bbox_h = ok.advance = 90
    assert tool.out_of_range([ok]) == []

    blob = tool.pack([ok])
    assert blob[:4] == b"af!?"
    flags, glyphs, contours, points = struct.unpack(">HHHH", blob[4:12])
    assert (glyphs, contours, points) == (1, 1, 5), (glyphs, contours, points)
    codepoint, bx, by, bw, bh, advance, ncontours = struct.unpack(">HbbBBBB", blob[12:20])
    assert (codepoint, advance, ncontours) == (ord("b"), 90, 1)
    # Header, glyph table, one contour length, then the points.
    assert len(blob) == 12 + 8 + 2 + 5 * 2, len(blob)

    # The format stores codepoints in a u16, so a Material Symbol above that has to be
    # remapped, and reported where it cannot be.
    high = tool.Glyph(0x1FFF0)
    high.contours = [[(0, 0), (10, 0), (10, -10), (0, 0)]]
    try:
        tool.pack([high])
    except SystemExit:
        pass
    else:
        raise AssertionError("packed a codepoint that does not fit")


@check
def test_a_source_keeps_what_it_worked_out(_h):
    """Settings are what a source is told; a store holds what it found out.

    A resolved location, a token, a high-water mark. Namespaced by the entry point name and
    written by the host, so an extension asks for a value and the filename stays here."""
    from statsbadge import state

    directory = tempfile.mkdtemp(prefix="statsbadge-state-")
    store = state.for_source(directory, "clock")
    assert store.path == os.path.join(directory, "clock.json")
    store.set("geocoded", {"sheffield": [53.38, -1.47, "Sheffield, GB"]})
    store.update({"kept": 1, "dropped": 2})
    store.forget("dropped")

    again = state.for_source(directory, "clock")
    assert again.get("geocoded")["sheffield"][2] == "Sheffield, GB"
    assert again.get("kept") == 1 and again.get("dropped") is None
    assert again.all() == store.all()
    # A different source cannot see it, or read it by accident.
    assert state.for_source(directory, "other").all() == {}

    # Nowhere to write is a store all the same, so a source needs no special case: `install`
    # loads every extension only to ask what it ships, so what it learns can go.
    memory = state.for_source(None, "clock")
    memory.set("geocoded", {})
    assert memory.get("geocoded") == {} and memory.path is None

    # Refused before the store changes, so what is in memory always matches what is on disk.
    try:
        store.set("no", object())
    except TypeError:
        pass
    else:
        raise AssertionError("stored something that cannot be written")
    assert "no" not in state.for_source(directory, "clock").all()
    assert store.get("no") is None, "the store kept a value the file did not"

    # A cache keyed by something a user types grows by one on every typo.
    for index in range(state.MAX_KEYS + 8):
        store.set(f"key{index}", index)
    assert len(store.all()) == state.MAX_KEYS
    assert store.get(f"key{state.MAX_KEYS + 7}") == state.MAX_KEYS + 7, "dropped the newest"

    # Setting a key again keeps it, so the one dropped at the cap is the longest untouched
    # and a place looked up every launch outlives a typo made once.
    kept, dropped = f"key{state.MAX_KEYS + 4}", f"key{state.MAX_KEYS + 5}"
    store.set(kept, "still wanted")
    for index in range(state.MAX_KEYS - 1):
        store.set(f"later{index}", index)
    assert store.get(kept) == "still wanted", "evicted a key that was being used"
    assert store.get(dropped) is None, "the cap is not being reached"

    # A name that would be a bad filename is still made into one: this ends up as a path.
    assert state.for_source(directory, "../etc/passwd").path == os.path.join(
        directory, "___etc_passwd.json")

    # Every source has one from the start, in memory until the host hands over a better.
    from statsbadge.sources import base

    class Nothing(base.Source):
        def sample(self, frame, dt):
            pass

    assert Nothing({}).store.path is None
    shutil.rmtree(directory, ignore_errors=True)


@check
def test_a_slow_lookup_does_not_hold_up_a_frame(_h):
    """Sources share the collector's thread and the first sample is taken while the server
    is starting up.

    A weather lookup on that thread stalled the launch for as long as the geocoder took to
    answer, which on a flaky connection outlasts urlopen's timeout, since that leaves name
    resolution uncovered."""
    try:
        import statsbadge_clock as clock
    except ImportError:
        return              # the extension is not pip installed in this environment

    source = clock.Clock({"place": "Sheffield"})
    asked = []

    def slow(_where, **_named):
        asked.append(time.monotonic())
        time.sleep(0.4)
        return {"temp": 11.0, "place": "Sheffield", "utc_offset": 0}

    source._fetch = slow
    source._geocode = lambda _place: (53.38, -1.47, "Sheffield, GB")
    source.pages([{"id": "clock1", "kind": "clockface", "place": "Sheffield"}])
    source.start()
    try:
        frame = {}
        started = time.monotonic()
        source.sample(frame, 1.0)
        assert time.monotonic() - started < 0.1, "sampling waited on the fetch"
        assert frame["clock"]["time"], "no clock in the frame"
        # What it brings back does reach a frame, once it has.
        for _ in range(40):
            time.sleep(0.1)
            source.sample(frame, 1.0)
            if frame["weather"] and frame["places"]:
                break
        assert frame["weather"]["temp"] == 11.0, frame["weather"]
        assert frame["places"]["clock1"]["temp"] == 11.0, frame["places"]
    finally:
        source.stop()
    assert asked, "nothing was ever fetched"

    # A refused lookup is tried again, and does not give up until the next save: the timer used
    # to be set before the attempt, so one failure at startup left the page with no weather.
    refused = clock.Clock({"place": "Sheffield"})
    tries = []

    def failing(place):
        tries.append(place)
        raise OSError("rate limited")

    refused._geocode = failing
    assert refused._where() is None and refused.faults == 1
    assert refused._where() is None and len(tries) == 1, "hammered a rate limited geocoder"
    refused._retry_at = 0.0
    assert refused._where() is None and len(tries) == 2, "never tried again"

    # A town stays put, so a name is looked up once ever, once a launch being wasteful: with the
    # coordinates in the store, a badge comes up knowing where it is looking even if the
    # geocoder is refusing everyone.
    from statsbadge import extensions, state

    directory = tempfile.mkdtemp(prefix="statsbadge-clock-")
    kept = clock.Clock({"place": "Sheffield"})
    kept.store = state.for_source(directory, "clock")
    calls = []
    kept._fetch = lambda where, **_named: calls.append(where) or {"temp": 9.0}
    real_urlopen = clock.urllib.request.urlopen
    clock.urllib.request.urlopen = lambda *_args, **_named: (_ for _ in ()).throw(
        AssertionError("asked the geocoder for a place it had already resolved"))
    try:
        state.for_source(directory, "clock").set(
            clock.GEOCODED, {"sheffield": [53.38, -1.47, "Sheffield, GB"]})
        kept.store = state.for_source(directory, "clock")
        assert kept._where() == (53.38, -1.47, "Sheffield, GB")
    finally:
        clock.urllib.request.urlopen = real_urlopen

    # The host settles where that file goes, one per extension name.
    loaded = extensions.load({"extensions": {}}, directory)
    for source in loaded:
        assert source.store.path == os.path.join(directory, f"{source.name}.json"), (
            source.name, source.store.path)
    assert any(source.name == "clock" for source in loaded), "the clock was not loaded"
    shutil.rmtree(directory, ignore_errors=True)


@check
def test_clock_weather_units_and_icons(_h):
    """Units travel with the readings, and each condition has a symbol."""
    try:
        import statsbadge_clock as clock
    except ImportError:
        return              # the extension is not pip installed in this environment

    assert "wind_units" in [setting["key"] for setting in clock.Clock.settings]

    source = clock.Clock({"units": "fahrenheit", "wind_units": "mph"})
    assert (source.units, source.wind_units) == ("fahrenheit", "mph")
    # An unknown unit would be passed straight to Open-Meteo, which rejects the request.
    assert clock.Clock({"wind_units": "furlongs"}).wind_units == "kmh"
    assert clock.Clock({}).wind_units == "kmh"

    # Every condition the code table can produce needs a symbol, or the page draws a
    # blank where the weather should be.
    for condition in set(clock.CONDITIONS.values()):
        assert condition in clock.ICONS, f"no icon for {condition!r}"
    letters = set(clock.ICONS.values()) | set(clock.NIGHT_ICONS.values())
    corpus = os.path.join(os.path.dirname(clock.__file__), "..", "..", "icons.txt")
    if os.path.exists(corpus):
        packed = set()
        with open(corpus, encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#", 1)[0].split()
                if len(line) == 3:
                    packed.add(line[2])
        missing = letters - packed
        assert not missing, f"icons.txt does not pack {sorted(missing)}"


@check
def test_the_reported_disk_is_the_one_with_your_files_on(_h):
    """On macOS "/" is a sealed system volume, and reporting it reads far too empty."""
    import platform

    import psutil

    from statsbadge.sources.portable import default_disk

    path = default_disk()
    if platform.system() == "Darwin":
        assert path == "/System/Volumes/Data", path
        # Both volumes share the container, so free space matches and only `used`
        # differs: the sealed root claims a fraction of what is actually in use.
        root = psutil.disk_usage("/")
        data = psutil.disk_usage(path)
        assert data.used > root.used
        assert data.percent > root.percent
    else:
        assert path == "/"


@check
def test_a_reading_carries_its_unit(_h):
    """A grid or a sparkline row has one slot, so the unit has to be in the text."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw

    assert draw.reading(9.2, "pct") == "9.2%"
    assert draw.reading(85.7, "pct") == "85.7%"
    assert draw.reading(71.0, "temp") == "71.0\u00b0C"
    assert draw.reading(None, "pct") == "--"
    assert draw.reading("workshop-pc", "host") == "workshop-pc"

    # A byte figure carries its prefix on the number and its base in the unit, so one
    # unit serves every size the reading grows to.
    assert draw.reading(800, "read_bps") == "800B/s"
    assert draw.reading(819200, "read_bps") == "800KB/s"
    assert draw.reading(52428800, "read_bps") == "50.0MB/s"
    assert draw.reading(3 * 1024 ** 3, "read_bps") == "3.0GB/s"
    assert draw.reading(512, "used_mb") == "512MB"
    assert draw.reading(12600, "used_mb") == "12.3GB"
    assert draw.reading(3 * 1024 ** 2, "total_mb") == "3.0TB"

    # A field can arrive as a list - a load average, per-core loads - and a list has no
    # hash, so it stops before the table keyed on a value. A load average prints as its
    # three figures, bare.
    assert draw.reading([1.52, 1.18, 0.94], "load") == "1.5 1.2 0.9"
    # Sixteen per-core loads overflow a slot, and three of the sixteen misreport it.
    assert draw.reading([31.0] * 16, "cores") == "16 values"
    assert draw.reading([], "load") == "--"

    import pages

    assert pages.fraction_of("cpu.load", [1.5, 1.2, 0.9]) is None, (
        "a list cannot sit on a gauge, and asking must not raise")


@check
def test_a_page_carries_only_what_its_kind_declared(_h):
    """Page-scoped settings, so two clock pages can show two cities.

    An extension page could not carry anything but fields before this: validate dropped
    every other key, so there was nowhere for a per-page place to live.
    """
    schema = {"clockface": [{"key": "place", "label": "Place", "type": "text"},
                            {"key": "big", "label": "Big", "type": "bool"}]}
    config = {"pages": [{"id": "a", "kind": "clockface", "title": "Tokyo",
                         "fields": [], "place": "Tokyo", "big": "yes",
                         "smuggled": "nope"}]}
    page = layout.validate(config, extra_kinds=("clockface",),
                           page_settings_schema=schema)["pages"][0]
    assert page["place"] == "Tokyo"
    assert page["big"] is True, "declared type not applied"
    assert "smuggled" not in page, "an undeclared key reached the badge"

    # Without a schema an extension page keeps its fields alone, as before.
    plain = layout.validate(config, extra_kinds=("clockface",))["pages"][0]
    assert "place" not in plain


@check
def test_an_extension_sees_only_its_own_pages(_h):
    """So a source can fetch per page without knowing about the rest of the layout."""
    from statsbadge import extensions

    seen = []

    class Fake:
        name = "fake"
        page_settings = ({"key": "place", "type": "text"},)
        badge_page = {"kind": "faceplate"}

        def pages(self, instances):
            seen.append([page.get("place") for page in instances])

    source = Fake()
    assert extensions.page_settings_schema([source]) == {
        "faceplate": [{"key": "place", "type": "text"}]}
    extensions.configure_pages([source], [
        {"kind": "faceplate", "place": "Tokyo"},
        {"kind": "dial", "field": "cpu.pct"},
        {"kind": "faceplate", "place": "Oslo"},
    ])
    assert seen == [["Tokyo", "Oslo"]], seen

    # A source that raises leaves the others still being told.
    class Angry(Fake):
        name = "angry"

        def pages(self, _instances):
            raise RuntimeError("no")

    extensions.configure_pages([Angry(), source], [{"kind": "faceplate", "place": "Rome"}])
    assert seen[-1] == ["Rome"]


@check
def test_the_build_script_defaults_where_the_installer_looks(_h):
    """Otherwise "rebuild it with ci/build-mpy.sh" is advice that changes nothing.

    The default output used to be build/mpy while the installer reads the copy inside the
    package, so a bare rebuild left the stale bytecode exactly where it was.
    """
    script = (pathlib.Path(__file__).parent.parent / "ci" / "build-mpy.sh").read_text(encoding="utf-8")
    default = [line for line in script.splitlines() if line.startswith("OUT_DIR=")]
    assert default, "no OUT_DIR default in the build script"
    assert "src/statsbadge/badge_app/mpy" in default[0], default[0]

    # Both CI workflows still name the packaged copy.
    for workflow in ("ci.yml", "publish.yml"):
        text = (pathlib.Path(__file__).parent.parent / ".github" / "workflows"
                / workflow).read_text(encoding="utf-8")
        if "build-mpy.sh" in text:
            assert "src/statsbadge/badge_app/mpy" in text, workflow


@check
def test_the_field_picker_offers_each_reading_once(_h):
    """numericRefs is a subset of availableRefs, so concatenating them listed every
    number twice - once qualified by its group and once again below it."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    # Joining the two lists is fine, so long as the result is deduplicated where it is
    # joined. Checked per line so this cannot pass by matching the fix itself.
    for line in ui.splitlines():
        if "concat(availableRefs())" in line:
            assert "new Set(" in line, f"undeduplicated: {line.strip()}"
    assert "function preferredRefs()" in ui
    # RefSelect deduplicates whatever it is handed, so no caller can bring it back.
    assert "new Set(refs)" in ui


@check
def test_every_kind_picks_from_a_pool_that_suits_it(_h):
    """A gauge offered uptime drew an empty ring, and a grid offered cpu.cores printed a
    list. Each slot now draws from a pool, and every kind has to name one."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    shape = ui[ui.index("const SHAPE = {"):ui.index("async function api(")]
    pools = ui[ui.index("const POOLS = {"):]
    pools = pools[:pools.index("}")]
    named = {name for name in ("gauge", "series", "list", "notify", "any")
             if name in pools}

    for kind in layout.KINDS:
        # An entry may be wrapped over two lines, so take it up to its closing brace
        # and the whole of it.
        start = shape.find(f"  {kind}: {{")
        assert start != -1, f"{kind} has no shape"
        entry = shape[start:shape.index("},", start)]
        if 'one: "' not in entry and 'many: "' not in entry:
            # A kind with no slots has an empty pool: the badge page reads the
            # badge, so there is no field to offer.
            assert "max: 0" in entry, f"{kind} has no slots but a field maximum"
            continue
        for slot, key in (("one", "pool"), ("many", "manyPool")):
            if f'{slot}: "' in entry:
                assert f"{key}:" in entry, f"{kind} has a {slot} slot with no {key}"
        for pool in named:
            if f'"{pool}"' in entry:
                break
        else:
            raise AssertionError(f"{kind} names no pool from {sorted(named)}: {entry}")


@check
def test_the_ui_is_told_what_a_gauge_can_scale(_h):
    """It cannot filter uptime out of a gauge without knowing what has a top end."""
    described = model.describe()
    assert "full_scale" in described and described["full_scale"], described.keys()
    assert "temp" in described["full_scale"]
    assert "uptime_s" not in described["full_scale"]
    assert "uptime_s" not in described["percent_fields"]
    # Which fields are a list, so only the kinds that draw lanes are offered them.
    assert set(described["list_fields"]) >= {"cores", "load"}


@check
def test_a_rate_is_scaled_by_what_it_has_reached(_h):
    """Throughput has no full scale, and a fixed one showed as pegged.

    12.5MB/s was assumed, so anything over that filled the ring: a 40MB/s transfer and a
    200MB/s one looked the same. The collector tracks what each rate has reached instead.
    """
    from statsbadge.collect import PEAK_FLOOR, PEAK_HALF_LIFE_S, Collector

    def run(rates, interval):
        """The peak after each rate in turn, sampled `interval` seconds apart."""
        collector = Collector(interval=interval, config={"sources": []})
        for rate in rates:
            collector._push_peaks({"net": {"down_bps": rate}}, interval)  # noqa: SLF001
        return collector._peaks["net.down_bps"]  # noqa: SLF001

    peak = run([40e6] * 5, 1.0)
    assert peak == 40e6
    # A trickle afterwards is a small part of the ring, not an eighth of a ring that was
    # already full.
    assert (1.5e6 / peak) < 0.05

    # The peak comes down again, so one busy night does not flatten it for good, and it
    # halves in the same wall-clock time whatever the sample interval is set to.
    halved = run([40e6, *[1.0] * int(PEAK_HALF_LIFE_S)], 1.0)
    slower = run([40e6, *[1.0] * int(PEAK_HALF_LIFE_S / 4)], 4.0)
    assert abs(halved - 20e6) < 1e5, halved
    assert abs(halved - slower) < 1e5, (halved, slower)

    # The floor keeps a quiet link from scaling a trickle up to a full ring.
    assert run([1.0] * 5, 1.0) == PEAK_FLOOR


@check
def test_setup_waves_through_a_server_already_paired(_h):
    """Setup is offered after a few failed polls, not only when unpaired, so it is easy to
    reach with nothing wrong. Asking to pair again then failed for a server the badge was
    already paired with, because the host was not in pairing mode."""
    source = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text(encoding="utf-8")
    assert "_already_paired" in source, "no already-paired path in setup"
    # Reached before anything is asked of the host.
    ask = source[source.index("def _ask_to_join"):]
    assert ask.index("_already_paired") < ask.index("net.enrol("), (
        "the badge asks to enrol before noticing it is already paired")
    # A host that has refused this badge is the case where pairing again is the point, so
    # that has to ask again.
    guard = source[source.index("def _already_paired"):source.index("def _ask_to_join")]
    assert "rejected" in guard, "a refused badge would be waved through with dead credentials"


@check
def test_a_file_that_did_not_write_is_not_left_on_the_badge(_h):
    """A volume that has only just mounted can refuse the first write, and what it leaves
    is an empty file rather than an error. An app whose __init__.py is zero bytes starts
    and does nothing at all."""
    import shutil as shutil_module

    with tempfile.TemporaryDirectory() as work:
        source = os.path.join(work, "app.py")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("print('hello')\n")
        destination = os.path.join(work, "copy.py")

        real = shutil_module.copy2
        tries = []

        def flaky(src, dst, **kwargs):
            tries.append(dst)
            if len(tries) == 1:
                # What macOS reports when the volume is not ready: the file is created
                # and nothing is written to it.
                with open(dst, "w", encoding="utf-8"):
                    pass
                raise OSError(6, "Device not configured")
            return real(src, dst, **kwargs)

        was_wait = install.COPY_WAIT
        install.COPY_WAIT = 0
        shutil_module.copy2 = flaky
        try:
            install._copy(source, destination)
            assert len(tries) == 2, tries
            assert os.path.getsize(destination) == os.path.getsize(source)

            # One that never writes in full is an error, not a short file left behind.
            tries.clear()
            def short(_src, dst, **_kwargs):
                open(dst, "w", encoding="utf-8").close()

            shutil_module.copy2 = short
            try:
                install._copy(source, destination)
            except install.InstallError as exc:
                assert "short" in str(exc), exc
            else:
                raise AssertionError("a short copy was called a good one")
        finally:
            shutil_module.copy2 = real
            install.COPY_WAIT = was_wait


@check
def test_a_beacon_goes_out_on_every_interface_it_can_name(_h):
    """Windows reports no broadcast address of its own, so it is worked out here.

    Without one the only packet leaving is the global broadcast, and on a machine with a
    virtual switch that leaves by whichever interface holds the default route. The badge
    finds hosts by beacon alone, so such a host is invisible to it.
    """
    from statsbadge import beacon

    assert beacon._subnet_broadcast("10.10.1.155", "255.255.255.0") == "10.10.1.255"
    assert beacon._subnet_broadcast("192.168.0.5", "255.255.0.0") == "192.168.255.255"
    # A point to point link has no broadcast address, and nothing to send to.
    assert beacon._subnet_broadcast("10.0.0.1", "255.255.255.254") is None
    assert beacon._subnet_broadcast("10.0.0.1", None) is None

    import psutil
    entry = type("Address", (), {"family": socket.AF_INET, "address": "10.10.1.155",
                                 "netmask": "255.255.255.0", "broadcast": None})
    was = psutil.net_if_addrs
    psutil.net_if_addrs = lambda: {"Ethernet": [entry]}
    try:
        addresses = beacon._broadcast_addresses()
    finally:
        psutil.net_if_addrs = was
    assert addresses == ["255.255.255.255", "10.10.1.255"], addresses


@check
def test_the_badge_never_waits_on_a_socket_the_screen_is_behind(_h):
    """A blocking connect to a host that is not there holds the draw loop for as long as
    lwIP takes to give up. The screen stops and HOME is never sampled, so the badge sits
    on "Connecting" with no way into the hosts menu."""
    source = (pathlib.Path(install.app_source_dir()) / "net.py").read_text(encoding="utf-8")
    connect = source[source.index("def _connect"):source.index("def _connecting")]
    assert "setblocking(False)" in connect, "the connect blocks the loop"
    assert "EINPROGRESS" in connect, "a connect that only started is an error to this"
    assert "yield from self._connecting()" in source, "nothing waits for the connect"

    # The badge reports POLLOUT and POLLHUP together for an address with nothing at it,
    # measured on the board, so the failure has to be looked at first.
    # From the loop over what the poll returned, the register call above naming POLLOUT
    # for what it is watching.
    waiting = source[source.index("for _sock, flags in"):source.index("# -- requests")]
    assert waiting.index("POLLERR") < waiting.index("POLLOUT"), \
        "a failed connect would read as a connected one"


@check
def test_the_badge_scans_for_longer_than_the_host_waits(_h):
    """The badge listens for a beacon and the host sends one, and neither imports the other.

    A scan shorter than the gap between two beacons misses a host that has just broadcast,
    and comes back saying nothing is there.
    """
    from statsbadge import beacon

    source = (pathlib.Path(install.app_source_dir()) / "net.py").read_text(encoding="utf-8")
    assert f"BEACON_PORT = {beacon.PORT}" in source, "the badge listens on another port"
    assert f"BEACON_EVERY_MS = {int(beacon.INTERVAL * 1000)}" in source, (
        "the badge assumes a different interval")
    assert "DISCOVER_MS = 2 * BEACON_EVERY_MS" in source

    # The host's own figure travels, so a server started with a different interval is
    # scanned for long enough without the badge being rebuilt.
    sent = beacon.Beacon(8420, "here", server_id="abc", interval=5.0).payload()
    assert sent["every_ms"] == 5000, sent
    assert len(json.dumps(sent)) < 256, "the packet is read into a 256 byte buffer"
    assert 'beacon.get("every_ms"' in source, "the badge drops what it is told"

    # Every scan covers an interval. Setup's countdown does it by repeating a short one
    # until the six seconds are up, so its own scans are allowed to be brief.
    menu = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text(encoding="utf-8")
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    for short in re.findall(r"discover\(timeout_ms=(\d+)\)", app):
        raise AssertionError(f"a {short}ms scan outside the countdown")
    assert "deadline" in menu, "the countdown is what makes setup's short scans add up"


@check
def test_every_cache_that_holds_a_colour_is_dropped_on_a_theme_change(_h):
    """A cache added to draw.py and left out of clear_cache survives a theme switch.

    It shows as one widget in the palette before last, which looks like a rendering
    fault and is a missing line. Registering at the point of definition is what makes
    leaving one out hard.
    """
    import ast

    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    # Registered, or named here as holding no colour and why.
    exempt = {"_fonts", "_weights", "_CLEARS"}
    loose = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if not name.startswith("_") or name in exempt:
                continue
            if isinstance(node.value, (ast.Dict, ast.Set)) or (
                    isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) in ("dict", "set")):
                loose.append(name)
    assert not loose, f"caches in draw.py outside _CLEARS: {loose}"

    body = source[source.index("def clear_cache"):]
    body = body[:body.index("\ndef ", 1)]
    assert "for empty in _CLEARS" in body, "clear_cache is enumerating by hand again"

    # The waterfall's scroll buffer and worldmap's pens are state, not one container.
    assert "@clears\ndef waterfall_reset" in source
    world = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    assert "@draw.clears\ndef forget" in world, "the map keeps the old theme's pens"


@check
def test_the_theme_the_badge_boots_with_is_the_host_s_dark(_h):
    """look.THEMES holds one palette, for the frames before the first layout arrives.

    The same colours are in themes.PALETTES, and MicroPython cannot import that, so the
    badge carries a copy. Drifting shows as the page changing colour a second after
    launch, which reads as a rendering fault and is a palette edit made once.
    """
    import ast

    source = (pathlib.Path(install.app_source_dir()) / "look.py").read_text(encoding="utf-8")
    call = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Theme":
            call = node
            break
    assert call is not None, "no Theme( in look.py"

    carried = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    assert ast.literal_eval(call.args[0]) == themes.DEFAULT, "a theme other than the default"
    wanted = themes.written()[themes.DEFAULT]
    assert carried == wanted, (
        f"the badge boots a different {themes.DEFAULT}: "
        f"{ {k: v for k, v in carried.items() if wanted.get(k) != v} }")


@check
def test_the_installer_and_the_app_name_the_same_extension_directory(_h):
    """The installer writes badge modules into it and the app puts it on sys.path.

    Disagreeing is an extension that installs and then draws nothing, with the page kind
    it registers missing and no error anywhere.
    """
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    assert f'EXT_DIR = "{install.EXT_DIR}"' in app, install.EXT_DIR
    # `pages` would be a directory shadowing the app's pages.py on sys.path.
    assert install.EXT_DIR != "pages"


@check
def test_one_writer_owns_the_badge_state_file(_h):
    """The installer writes it over the REPL and the app writes it at runtime.

    Two processes, so the path is a literal at each end and only a check holds them
    together. Inside the app it is net.Config alone, which keeps the page index as well
    as the pairing: two writers each had to merge, and either could have replaced.
    """
    app_dir = pathlib.Path(install.app_source_dir())
    net_source = (app_dir / "net.py").read_text(encoding="utf-8")
    assert f'STATE_FILE = "{install.STATE_FILE}"' in net_source, (
        f"the app does not write {install.STATE_FILE}")

    # The installer merges, so a page index and a pairing with another host both survive.
    assert "data = json.load(open(path))" in (
        pathlib.Path("src/statsbadge/install.py").read_text(encoding="utf-8"))

    app = (app_dir / "__init__.py").read_text(encoding="utf-8")
    assert "State." not in app, "the app is writing state behind Config's back"
    for owned in ("self.config.page = self.page_index", "self.config.save()"):
        assert owned in app, owned


@check
def test_a_row_of_text_and_a_plot_measures_its_columns(_h):
    """A fixed column either leaves a gap after the names or runs the readings into the
    plots, and which of the two it does depends on the fields the page carries."""
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    for widget in ("def bars", "def sparklines", "def graph"):
        body = source[source.index(widget):]
        body = body[:body.index("\ndef ", 1)]
        assert "column_width(" in body, f"{widget} still lays out to a fixed column"

    # The gauge and its column sit in the band on one gap, so no part of the pair can be
    # placed on a number it picked.
    look_source = (pathlib.Path(install.app_source_dir()) / "look.py").read_text(encoding="utf-8")
    for name in ("DIAL_C = (DIAL_GAP", "READOUT_X = DIAL_C[0]",
                 "READOUT_W = W - READOUT_X - DIAL_GAP"):
        assert name in look_source, f"{name} is not derived from the dial's gap"


@check
def test_a_split_page_takes_the_layout_it_is_given(_h):
    """A dial, a ring stack and a clock face all split the band into something round and a
    column, and the pages are paged between: anything choosing a centre or margin
    moves under the reader when they press a button."""
    app = pathlib.Path(install.app_source_dir())

    # Four rings have to fit the same radius a single gauge draws in.
    look_source = (app / "look.py").read_text(encoding="utf-8")
    draw_source = (app / "draw.py").read_text(encoding="utf-8")
    scope = {}
    for line in look_source.splitlines():
        if line.startswith(("DIAL_OUTER", "DIAL_GAP", "READOUT_H", "READOUT_NOTE_H")):
            exec(line, scope)  # noqa: S102  a module in this repo, four constants off the top
    band = int(re.search(r"^RING_BAND = (\d+)", draw_source, re.M).group(1))
    gap = int(re.search(r"^RING_GAP = (\d+)", draw_source, re.M).group(1))
    innermost = scope["DIAL_OUTER"] - 4 * band - 3 * gap
    assert innermost >= 8, f"the fourth ring is {innermost} across; it would be dropped"

    # The clock takes both from the app, restating neither, and puts its column
    # where every other split page puts it.
    clock = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge")
             / "clockface.py").read_text(encoding="utf-8")
    assert "CENTRE = look.DIAL_C" in clock, "the clock face has a centre of its own"
    assert "RADIUS = look.DIAL_OUTER" in clock, "the clock face has a radius of its own"
    assert "look.READOUT_X" in clock and "draw.column_lines" in clock, (
        "the clock face lays its column out by hand")


@check
def test_a_gauge_can_sweep_to_its_reading(_h):
    """A reading lands once a second and the gauge may ease to it instead of stepping.

    The needle has to leave from where it *is*: a second reading arriving mid-sweep must
    carry on from the drawn position, not jump to the one it was heading for. And the frames
    only come while something is moving, or a sweeping page would redraw all second."""
    import sys

    config = layout.validate({"animate": True, "pages": layout.DEFAULT_PAGES})
    assert config["animate"] is True
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["animate"] is False, (
        "off by default")

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="animate"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert 'bindCheck("animate", "animate")' in (web / "app.js").read_text(encoding="utf-8"), \
        "the control is not bound"

    sys.path.insert(0, install.app_source_dir())
    import pages

    # A stand-in for the firmware's tween, driven by hand: the easing is picovector's, so
    # what is worth checking here is which endpoints each sweep is given.
    class FakeTween:
        CUBIC_OUT = "cubic_out"
        made = []

        def __init__(self, start, end, duration, _easing):
            self.from_, self.to, self.duration = start, end, duration
            self.progress = 0.0
            FakeTween.made.append((start, end))

        def start(self):
            return self

        @property
        def now(self):
            return self.from_ + (self.to - self.from_) * self.progress

        @property
        def done(self):
            return self.progress >= 1.0

    pages.__dict__["tween"] = FakeTween
    was = pages.ANIMATE
    try:
        pages.ANIMATE = False
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 40.0) == 0.4, "stepping when the setting is off"
        assert not FakeTween.made, "a sweep was started with the setting off"

        pages.ANIMATE = True
        # The first reading is drawn where it is: there is nowhere to come from.
        assert pages.fraction_of("cpu.pct", 40.0) == 0.4
        assert FakeTween.made == [(0.4, 0.4)], FakeTween.made
        assert not pages.moving, "a gauge with nowhere to go asked for another frame"

        # A new reading sweeps from the last, and asks for frames until it lands.
        assert pages.fraction_of("cpu.pct", 80.0) == 0.4, "the needle jumped to the reading"
        assert FakeTween.made[-1] == (0.4, 0.8), FakeTween.made
        assert pages.moving

        # Interrupted half way: from the drawn position, not from 0.8.
        pages._sweeps["cpu.pct"].progress = 0.5
        assert abs(pages.fraction_of("cpu.pct", 20.0) - 0.6) < 1e-9
        started, heading = FakeTween.made[-1]
        assert abs(started - 0.6) < 1e-9 and heading == 0.2, FakeTween.made

        # The same reading again continues the sweep in flight.
        made = len(FakeTween.made)
        pages.fraction_of("cpu.pct", 20.0)
        assert len(FakeTween.made) == made, "an unchanged reading restarted the sweep"

        # A page turn drops where everything stood, a turn not being a change in the
        # machine.
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 20.0) == 0.2
        assert FakeTween.made[-1] == (0.2, 0.2)
    finally:
        pages.ANIMATE = was
        pages.sweep_reset()
        pages.__dict__.pop("tween", None)

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    assert "pages_module.sweep_reset()" in app[app.index("def turn"):], (
        "a page turn keeps the last page's needle positions")
    assert "pages_module.moving" in app, "nothing asks for a frame while a gauge is moving"


@check
def test_a_page_can_slide_on_like_a_card(_h):
    """A window of the screen carries an origin, so a page drawn into one lands shifted and
    clipped: that is the card, and the rasteriser costs the window alone.
    `over` leaves the outgoing page standing under it; `deck` moves both, which needs a copy
    of the page that is leaving because a window cannot start at a negative origin."""
    for style in layout.SLIDE_STYLES:
        assert layout.validate({"slide": style,
                                "pages": layout.DEFAULT_PAGES})["slide"] == style
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["slide"] == "off", (
        "immediate by default")
    assert layout.validate({"slide": "sideways",
                            "pages": layout.DEFAULT_PAGES})["slide"] == "off"
    # A bool still works, from before there was a choice of styles.
    assert layout.validate({"slide": True, "pages": layout.DEFAULT_PAGES})["slide"] == "over"
    assert layout.validate({"slide": False, "pages": layout.DEFAULT_PAGES})["slide"] == "off"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="slide"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.slide" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    for style in layout.SLIDE_STYLES:
        assert f'value="{style}"' in (web / "index.html").read_text(encoding="utf-8"), style

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    sliding = app[app.index("def render_sliding"):]
    sliding = sliding[:sliding.index("\n    def ", 1)]
    # Both cards are a rect out of an image, which makes the direction free: a window
    # cannot start at a negative origin, so a page cannot be drawn part way off the left.
    assert "self.arriving.window(" in sliding and "self.leaving.window(" in sliding
    assert "self.slide_back" in sliding, "both directions look the same"

    into = app[app.index("def draw_page_into"):]
    into = into[:into.index("\n    def ", 1)]
    # Rebound, in place of passing it. An extension's renderer draws through the same
    # builtin, and
    # would otherwise put its page on the screen while the app drew into the image.
    assert "builtins.screen = target" in into and "builtins.screen = was" in into
    # From whatever screen is now: badge.mode replaces it, and a copy taken at import time is
    # the 160x120 screen the app started with - a 320-wide page drawn into that wraps.
    assert "was = screen" in into
    assert "target.font" in into, "an image starts with no font, and label() restores it"

    # The turn only starts one when the layout asks, keeping the screen only for a deck.
    turn = app[app.index("def turn"):]
    turn = turn[:turn.index("\n    def ", 1)]
    assert 'setting("slide")' in turn and "delta < 0" in turn
    # A press schedules the movement, so a burst is one slide onto the page it landed on
    # and one only, several fighting over the screen being the fault.
    assert "SLIDE_WAIT_MS" in turn
    due = app[app.index("def slide_due"):]
    due = due[:due.index("\n    def ", 1)]
    assert "self.sliding is not None" in due, "a second slide can start over a running one"

    # The title and the pip answer every press, including presses that land during a slide,
    # so paging through five pages moves the pip five times and slides once. That takes two
    # things together, and either alone is a bug that shipped:
    #
    #   - the wait is drawn ahead of a running slide, or a press mid-slide leaves the pip
    #     stuck until the movement finishes;
    #   - a press abandons the slide it lands in, clearing whatever the wait
    #     is now suppressing. Queueing behind it instead gave a slide per press, each late.
    body = app[app.index("    def render(self):"):]
    body = body[:body.index("\n    def ", 1)]
    assert body.index("self._slide_at") < body.index("self.sliding is not None"), (
        "a press during a slide cannot move the pip")
    assert "self.sliding = None" in turn, "a press queues behind the slide it lands in"
    assert "draw.furniture(" in body, "the press does not answer until the body catches up"
    # The body is withheld on a deadline, the flag alone being unsafe, so it can
    # hold it back for longer than the wait however the state is arrived at.
    assert "time.ticks_diff(self._slide_at" in body, (
        "the body can be withheld for longer than the wait")
    start = app[app.index("def start_slide"):]
    assert 'style == "deck"' in start[:start.index("\n    def ", 1)]


@check
def test_smooth_graphs_are_a_setting_that_reaches_the_badge(_h):
    """A drawing switch, so it is one setting covering every graph on the badge."""
    config = layout.validate({"smooth": False, "pages": layout.DEFAULT_PAGES})
    assert config["smooth"] is False
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["smooth"] is True, "on by default"
    # Anything truthy, since the UI sends a checkbox and a command line sends a string.
    assert layout.validate({"smooth": "yes", "pages": layout.DEFAULT_PAGES})["smooth"] is True

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="smooth"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.smooth" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    # The badge applies it where it applies the rest of the layout.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.SMOOTH" in applied[:applied.index("\n    def ", 1)]


@check
def test_a_theme_travels_as_its_colours(_h):
    """A theme is a table of colours, so it is config: the badge is sent the palette as
    well as the name, and an unfamiliar one draws as well as one it ships with."""
    import sys

    from statsbadge import themes

    sys.path.insert(0, install.app_source_dir())
    import look

    # Every palette is complete, ordered and usable by the badge's builder.
    assert layout.DEFAULT_CONFIG["theme"] == themes.DEFAULT
    for name, palette in themes.written().items():
        assert name in layout.THEMES, f"{name} is not offered"
        built = look.from_palette(name, palette)
        assert built is not None, f"the badge cannot build {name}"
        assert built.name == name, built.name
        stops = built.ramp
        assert stops[0][0] == 0.0 and stops[-1][0] == 1.0, name
        assert [pos for pos, _pen in stops] == sorted(pos for pos, _pen in stops), name
        # A theme is built out of palette data and holds `color` objects, so a pen takes
        # what it is handed, every colour arriving ready to draw with.
        for fraction in (0.0, 0.5, 1.0):
            assert isinstance(built.at(fraction), builtins.color), (name, fraction)
        assert built.at(0.0) == stops[0][1] and built.at(1.0) == stops[-1][1], name

    # The one the app carries to boot with agrees with the host's copy of it, or the
    # first frame is drawn in colours the config left behind.
    assert list(look.THEMES) == [themes.DEFAULT], list(look.THEMES)
    booted, sent = look.THEMES[themes.DEFAULT], themes.written()[themes.DEFAULT]
    for key in ("bg", "panel", "ink", "dim", "accent", "grid"):
        assert getattr(booted, key) == builtins.color.rgb(*sent[key]), key
    assert booted.ramp == tuple((pos, builtins.color.rgb(*rgb))
                                for pos, rgb in sent["ramp"])

    # The colours are on the payload the badge fetches, keyed to the theme it chose.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({"theme": "eva01", "pages": layout.DEFAULT_PAGES})
    sent = config.for_badge()
    assert sent["theme"] == "eva01"
    stored = themes.written()["eva01"]
    assert {key: sent["palette"][key] for key in stored} == stored, sent["palette"]
    assert look.from_palette(sent["theme"], sent["palette"]).accent == \
        builtins.color.rgb(143, 212, 0)

    # Plus the greys a picture is drawn in, derived from the accent's hue as `stripe` is.
    #
    # Their lightnesses are fixed and the same on every theme, which is the guarantee. The
    # host dithers a photograph to a position on this ramp with no say in which theme draws
    # it, so index 2 of four has to mean the same brightness everywhere.
    from statsbadge import derive

    wanted = None
    for name in layout.THEMES:
        greys = layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])["image"]
        assert sorted(greys) == ["4", "8"], sorted(greys)
        for count, ramp in greys.items():
            assert len(ramp) == int(count), (name, count)
        lightnesses = [derive.oklch(tuple(rgb))[0] for rgb in greys["4"]]
        assert lightnesses == sorted(lightnesses), (name, lightnesses)
        if wanted is None:
            wanted = lightnesses
        # Within a rounding of each other. The drift is where a colour lands on whole
        # bytes: at a saturated theme's chroma a channel step is worth more lightness.
        #
        # Measured across every theme and both level counts, at most 0.013.
        adrift = max(abs(one - other) for one, other in zip(lightnesses, wanted, strict=True))
        assert adrift <= 0.015, f"{name} draws a picture {adrift:.3f} off the levels"

    # How colourful it is comes off the theme: the same share of what the hue can hold
    # that the accent takes of its. A grey accent gives a grey picture, which is the
    # convention `mono` exists for, where a fixed tint gave it a coloured one.
    for name, coloured in (("mono", False), ("luminescence", True), ("eva01", True)):
        shades = layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])["image"]["8"]
        chroma = max(derive.oklch(tuple(rgb))[1] for rgb in shades)
        assert (chroma > 0.05) is coloured, f"{name} midtone chroma {chroma:.3f}"
    # The badge builds them keyed by how many, matching an indexed image's
    # table length says.
    built = look.from_palette("eva01", sent["palette"])
    assert sorted(built.image) == [4, 8], sorted(built.image)
    assert all(isinstance(pen, builtins.color) for pen in built.image[4])
    # A host too old to send them leaves a theme that draws no pictures, not one that fails
    assert look.from_palette("old", {k: v for k, v in sent["palette"].items()
                                     if k != "image"}).image == {}

    # Nonsense off the network is refused before it is drawn: a bad palette would otherwise
    # be a crash on every frame where a page in the boot theme is wanted.
    for bad in (None, {}, {"bg": "red"}, {"bg": (1, 2, 3), "ramp": ()}):
        assert look.from_palette("bad", bad) is None, bad


@check
def test_a_palette_can_carry_a_second_accent(h):
    """One more colour, used sparingly, a graph's second series being all of it. It is
    where the app used to hunt through the ramp for something that would show. A palette that
    names none gets the accent again, matching every palette without one."""
    import sys

    from statsbadge import derive, themes

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    for rule in layout.ACCENT_B_RULES:
        assert layout.validate({"accent_b": rule,
                                "pages": layout.DEFAULT_PAGES})["accent_b"] == rule
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["accent_b"] == "same"
    assert layout.validate({"accent_b": "clashing",
                            "pages": layout.DEFAULT_PAGES})["accent_b"] == "same"

    # Each rule keeps the accent's family: the same lightness and the same share of what
    # its hue can hold - so the two look like one palette's two colours.
    accent = derive.accents("normal")[6]
    assert tuple(derive.second_accent(accent, "same")) == tuple(accent)
    lightness, chroma, hue = derive.oklch(accent)
    for rule in ("complementary", "triadic", "contrasting"):
        other = derive.second_accent(accent, rule)
        second = derive.oklch(other)
        assert abs(second[0] - lightness) < 0.03, (rule, second[0], lightness)
        assert derive.apart(accent, other) > 10.0, (rule, derive.apart(accent, other))
    # Complementary is the wheel's opposite; contrasting is whichever offered hue lands
    # furthest away once lightness and chroma are counted, which is the maximum.
    opposite = derive.second_accent(accent, "complementary")
    furthest = derive.second_accent(accent, "contrasting")
    assert derive.apart(accent, furthest) >= derive.apart(accent, opposite)
    turn = derive.oklch(opposite)[2] - hue
    assert abs((turn - 180.0 + 180.0) % 360.0 - 180.0) < 2.0, turn

    # It reaches the badge in the palette, where a second series takes it.
    palette = layout.palette_for("tinted-dark", accent, "contrasting")
    theme = look.from_palette("tinted", palette)
    assert theme is not None
    assert theme.accent_b == FakeColour.rgb(*palette["accent_b"])
    assert draw._series_colour(theme, 1) == theme.accent_b
    assert draw._series_colour(theme, 0) == theme.accent
    # A palette with none: the accent again, and the ramp still answers for the second series.
    plain = look.from_palette("dark", themes.written()["dark"])
    assert plain.accent_b == plain.accent
    assert draw._series_colour(plain, 1) != plain.accent

    # The one written-down palette that needed it: a page that pink shows its green nowhere
    # else, the ramp's cold end being a reading that goes unused.
    melon = look.from_palette("watermelon-light", themes.written()["watermelon-light"])
    assert melon.accent_b != melon.accent
    written = themes.written()["watermelon-light"]
    assert derive.apart(written["accent_b"], written["accent"]) > 20.0

    # Where it shows: the chrome takes it, so the first accent is left for what a reading is
    # drawn in. A palette with none has the two the same colour, and it holds.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    header = source[source.index("def furniture("):]
    header = header[:header.index("\ndef ", 1)]
    assert "screen.pen = theme.accent_b" in header, "the header rule is not the second accent"
    pips = source[source.index("def _pips("):]
    pips = pips[:pips.index("\ndef ", 1)]
    assert "theme.accent_b if i == index" in pips, "the current pip is not the second accent"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="accentb"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.accent_b" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    status, shown = h.raw("GET", "/api/theme?theme=tinted-dark&second=triadic")
    assert status == 200 and shown["palette"]["accent_b"] != shown["palette"]["accent"]
    status, _bad = h.raw("GET", "/api/theme?theme=tinted-dark&second=nonesuch")
    assert status == 400, status


@check
def test_the_single_hue_themes_are_the_bold_variant_now(_h):
    """Red, green, cyan, amber and blueprint were five palettes doing one thing.

    Everything in one hue, the accent as saturated as sRGB allows, and a ramp sweeping
    lightness inside that hue while holding off red.

    Measured, each sat within 0.003 of its hue's chroma limit, and `red` differed from the
    derived palette at the same hue by 8 counts in the accent alone. So they are the bold
    variant with an accent, and the names still resolve."""
    from statsbadge import derive, themes

    for retired in themes.ALIASES:
        assert retired not in themes.written(), f"{retired} is still a written-down palette"
        name, accent = layout.resolve_theme(retired, None)
        assert themes.THEMES[name]["derived"].get("bold"), (retired, name)
        assert tuple(accent) in derive.offered(), (retired, accent)

    # A stored name keeps drawing: resolved once when the file is read, so nothing
    # downstream has to know it ever existed.
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-alias-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 3, "theme": "amber", "pages": layout.DEFAULT_PAGES,
                   "badges": {"badgeone": {"rev": 4, "theme": "cyan",
                                           "pages": layout.DEFAULT_PAGES}}}, handle)
    stored = layout.Config(path)
    assert stored.layout_for()["theme"] == "tinted-bold-dark"
    assert stored.layout_for("badgeone")["theme"] == "tinted-bold-dark"
    # Each brings the colour it named, not whatever tint was stored beside it.
    amber = derive.oklch(stored.layout_for()["tint"])[2]
    cyan = derive.oklch(stored.layout_for("badgeone")["tint"])[2]
    assert abs(amber - 60.0) < 1.0 and abs(cyan - 210.0) < 1.0, (amber, cyan)
    assert stored.for_badge()["palette"]["ramp"][-1][1] != list(
        themes.written()["dark"]["ramp"][-1][1])
    # A PUT carrying an old name is taken as well, an open browser being older than the host.
    assert layout.validate({"theme": "red", "pages": layout.DEFAULT_PAGES})["theme"] == (
        "tinted-bold-dark")
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # The saturated family covers the retired ones: at the limit, and different per hue.
    lightness = derive.ACCENT_FAMILIES["saturated"][0]
    for accent in derive.accents("saturated"):
        _l, chroma, hue = derive.oklch(accent)
        assert chroma >= derive.max_chroma(lightness, hue) * 0.9, (hue, chroma)
    spread = [round(derive.oklch(a)[1], 3) for a in derive.accents("saturated")]
    assert max(spread) - min(spread) > 0.1, spread
    # A bold ramp stays in the accent's hue, where the even variant's travels to red.
    for accent in derive.accents("saturated"):
        ramp = derive.palette(accent, "dark", True)["ramp"]
        hues = [derive.oklch(rgb)[2] for _pos, rgb in ramp]
        span = max(abs((hue - hues[0] + 180.0) % 360.0 - 180.0) for hue in hues)
        assert span < 20.0, (accent, hues)


@check
def test_a_theme_with_a_counterpart_has_one_in_the_other_mode(_h):
    """Four of the hand-written themes come as a pair, one for a lit room and one for a dark
    one. Each is placed against the background it goes on and measured here; inverting
    channel by channel lands ink on a white page at the wrong lightness."""
    from statsbadge import derive, themes

    modes = {record["name"]: record["mode"] for record in layout.theme_records()}
    for dark, light in (("mono", "mono-light"), ("watermelon", "watermelon-light"),
                        ("shell", "shell-light"), ("luminescence-dark", "luminescence")):
        assert modes[dark] == "dark" and modes[light] == "light", (dark, light)

    # Every palette clears the bar the shipped ones set: AAA for ink, since it
    # what a reading is drawn in, and a hot end that shows against the page at all.
    for name, palette in themes.written().items():
        ink = derive.contrast(palette["ink"], palette["bg"])
        dim = derive.contrast(palette["dim"], palette["bg"])
        hot = derive.contrast(palette["ramp"][-1][1], palette["bg"])
        assert ink >= 7.0, (name, ink)
        assert dim >= 2.5, (name, dim)
        assert hot >= 1.9, (name, hot)
        cold = palette["ramp"][0][1]
        apart = sum((a - b) ** 2 for a, b in zip(cold, palette["ramp"][-1][1], strict=True))
        assert apart > 1600, (name, apart)


@check
def test_a_badge_s_own_layout_falls_back_to_the_default(_h):
    """A badge's block sits over the default, not instead of it.

    A block saved before a setting existed carries none, and returning it whole handed the
    badge a layout with holes: no tint to build a palette from, and a theme of None that
    drew the boot colours whatever was chosen.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-blocks-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 4, "theme": "sakura", "brightness": 0.8,
                   "badges": {"partial": {"brightness": 0.5},
                              "whole": {"theme": "mono", "tint": layout.DEFAULT_CONFIG["tint"]}}},
                  handle)
    config = layout.Config(path)

    # A block that named no theme keeps none.
    assert "theme" not in config.data["badges"]["partial"]

    partial = config.for_badge(None, "partial")
    assert partial["theme"] == "sakura", "a partial block lost the theme it inherits"
    assert partial["brightness"] == 0.5, "a partial block lost what it does say"
    assert partial["palette"]["bg"] == themes.written()["sakura"]["bg"]

    whole = config.for_badge(None, "whole")
    assert whole["theme"] == "mono"
    assert whole["brightness"] == 0.8, "a block should inherit what it does not name"


@check
def test_a_re_tinted_theme_is_a_different_theme_to_a_cache(_h):
    """A derived theme keeps its name when it is built again from another accent, so
    anything baked under the name went on being drawn in the colours it was baked in.

    The clock's face was the visible one: re-tinting left the old dial behind.
    """
    import sys

    from statsbadge import derive

    sys.path.insert(0, install.app_source_dir())
    import look

    accents = derive.accents("saturated")
    one = layout.palette_for("tinted-dark", accents[0])
    other = layout.palette_for("tinted-dark", accents[6])
    first = look.from_palette("tinted-dark", one)
    second = look.from_palette("tinted-dark", other)

    assert first.name == second.name, "the names were expected to match"
    assert first.key != second.key, "a cache cannot tell the two apart"
    # The same accent twice is one key, or every poll would throw the caches away.
    assert first.key == look.from_palette("tinted-dark", one).key

    # Every cache that holds something baked in a theme's colours keys on that.
    app = pathlib.Path(install.app_source_dir())
    clock = pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge/clockface.py")
    for source in [app / "draw.py", app / "worldmap.py", app / "__init__.py", clock]:
        body = source.read_text(encoding="utf-8")
        assert "theme.name" not in body, f"{source.name} still keys a cache on the name"


@check
def test_the_case_lights_follow_the_backlight(_h):
    """A case light is one brightness and not a colour, so a theme leaves it alone.

    They track the panel instead. A dark room otherwise dims the screen and leaves four
    lights burning at the level a palette happened to name."""
    import ast
    import sys

    sys.path.insert(0, install.app_source_dir())
    import look

    # The palette, the theme and the wire have all dropped it.
    assert not hasattr(look.THEMES[look.DEFAULT], "case")
    assert look.from_palette("d", {**_palette_of("dark"), "case": 0.9}).__dict__.get("case") is None

    source = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies = {node.name: node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef)}

    lights = ast.dump(bodies["apply_caselights"])
    assert "wanted_brightness" in lights, "the case lights do not follow the backlight"
    # The attribute and not the word; the docstring still uses it.
    assert "attr='case'" not in lights, "the case lights still read the theme"
    assert "CASELIGHT_FLOOR" in lights, "a followed reading lost its floor"
    # Reapplied wherever the brightness moves, and not only when a layout lands: a button
    # press would otherwise dim the panel alone.
    assert "apply_caselights" in ast.dump(bodies["apply_backlight"])


def _palette_of(name):
    """A theme's palette as it reaches the badge, for a from_palette check."""
    return layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])


@check
def test_the_themes_are_a_data_file(_h):
    """A theme is data, so it lives in a data file, where every entry is the same shape.

    A special case was what put a second half-description of it in layout.py before."""
    import tomllib
    from importlib import resources

    from statsbadge import derive, themes

    raw = tomllib.loads(resources.files("statsbadge").joinpath("themes.toml").read_text(encoding="utf-8"))
    raw.pop("aliases")
    assert set(raw) == set(themes.THEMES) and len(raw) == 22, len(raw)

    for name, record in raw.items():
        spec = record.get("derived")
        named = [role for role in themes.ROLES if role in record]
        assert bool(spec) != bool(named), f"{name} is both written down and derived"
        if spec:
            assert spec["shape"] in derive.SHAPES, (name, spec)
            continue
        assert set(named) == set(themes.ROLES), f"{name} is missing {set(themes.ROLES) - set(named)}"
        for role in named + (["accent_b"] if "accent_b" in record else []):
            channels = record[role]
            assert len(channels) == 3 and all(0 <= v <= 255 for v in channels), (name, role)
        at = [position for position, _rgb in record["ramp"]]
        assert at[0] == 0.0 and at[-1] == 1.0 and at == sorted(at), (name, at)

    # The retired names still resolve, so a badge showing one carries on showing it.
    for retired, aliased in themes.ALIASES.items():
        assert aliased["theme"] in themes.THEMES, (retired, aliased)

    # A case light is a brightness and not a colour, so no palette carries one.
    assert not any("case" in record for record in raw.values())
    palette = layout.palette_for("tinted-dark", layout.DEFAULT_CONFIG["tint"])
    assert "case" not in palette


@check
def test_a_lit_theme_is_one_hue_throughout(_h):
    """Glow is Tinted Bold with a lit page: monochromatic, soft, a vintage screen.

    Which means it has to take the bold ramp. A ramp that travels to red is not monochrome
    whatever the furniture does, and that is the whole flavour rather than a detail.
    """
    from statsbadge import derive, themes

    def wander(palette):
        """The furthest any colour sits from the rest in hue, ignoring the near-greys."""
        hues = []
        for role in ("bg", "panel", "grid", "dim", "ink", "accent"):
            lightness, chroma, hue = derive.oklch(palette[role])
            if chroma > 0.01:
                hues.append(hue)
        for _at, colour in palette["ramp"]:
            lightness, chroma, hue = derive.oklch(colour)
            if chroma > 0.01:
                hues.append(hue)
        return max(abs((hue - hues[0] + 180.0) % 360.0 - 180.0) for hue in hues)

    for name in ("tinted-glow-dark", "tinted-glow-light"):
        assert themes.THEMES[name]["derived"].get("bold"), f"{name} would travel to red"

    worst = {}
    for name, record in themes.THEMES.items():
        spec = record.get("derived")
        if not spec:
            continue
        for family in derive.ACCENT_FAMILIES:
            for accent in derive.accents(family):
                moved = wander(layout.palette_for(name, accent))
                worst[name] = max(worst.get(name, 0.0), moved)
    # Measured: a signal ramp takes a palette most of the way round the wheel.
    for name in ("tinted-glow-dark", "tinted-glow-light", "tinted-bold-dark"):
        assert worst[name] < 20.0, (name, worst[name])
    assert worst["tinted-dark"] > 100.0, worst["tinted-dark"]
    # The hand-tuned originals it was shaped from, for scale.
    for name in ("luminescence", "luminescence-dark"):
        assert wander(themes.written()[name]) < 20.0, name

    # The other half is the page. A lit one carries more of the hue at less contrast,
    # and both move together or readable_on walks the ink back out.
    for lit, plain in (("glow-dark", "dark"), ("glow-light", "light")):
        assert derive.SHAPES[lit]["ink_ratio"] < derive.INK_RATIO
        for family in derive.ACCENT_FAMILIES:
            for accent in derive.accents(family):
                shaped = derive.palette(accent, lit, bold=True)
                flat = derive.palette(accent, plain)
                assert (derive.contrast(shaped["ink"], shaped["bg"])
                        < derive.contrast(flat["ink"], flat["bg"])), (lit, accent)
                assert (derive.oklch(shaped["bg"])[1] > derive.oklch(flat["bg"])[1]), (lit, accent)


@check
def test_a_graph_s_two_series_read_apart(_h):
    """The second series is the badge's choice, worked out on the host for the preview.

    Whichever end of the ramp sits furthest from the accent and can still be seen, with
    `dim` last. Measured, no theme lands on that last resort."""
    import sys

    from statsbadge import derive, themes

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    assert layout.SERIES_FLOOR == draw.SERIES_FLOOR
    assert layout.SERIES_ALPHA == draw.SERIES_ALPHA
    assert layout.PALE_SUM == look.PALE_SUM

    for name in themes.THEMES:
        for accent in (derive.accents()[6], derive.accents("saturated")[0]):
            palette = layout.palette_for(name, accent)
            first, second = layout.series_colours(palette)
            assert tuple(first) == tuple(palette["accent"])
            # The candidates are the badge's. Which one it lands on is not asserted
            # against `draw._series_colour`: that runs on FakeColour, whose `difference`
            # stands in for the firmware's with sRGB distance, and the two part company
            # on shell-light.
            theme = look.from_palette(name, palette)
            assert draw._series_colour(theme, 0) == theme.accent
            # `dim` is the last resort. Membership and not colour: mono's dim and its
            # cold end are the same grey.
            chosen = {tuple(palette["ramp"][0][1]), tuple(palette["ramp"][-1][1])}
            if "accent_b" in palette:
                chosen.add(tuple(palette["accent_b"]))
            assert tuple(second) in chosen, f"{name} fell back to dim"
            # Seen against the page, which the floor is there to keep true.
            alpha = layout.SERIES_ALPHA[0 if sum(palette["bg"]) >= layout.PALE_SUM else 1]
            shown = tuple(round(pen * alpha / 255.0 + bg * (1 - alpha / 255.0))
                          for pen, bg in zip(second, palette["bg"], strict=True))
            assert derive.apart(palette["bg"], shown) >= layout.SERIES_FLOOR, (name, second)


@check
def test_the_preview_reads_a_number_as_the_badge_does(_h):
    """The preview formats and scales readings itself, pages.py importing draw and draw
    expecting the firmware's globals, so the host cannot answer for it.

    The tables are the half that drifts, so they are compared entry by entry, parsed out of
    the source: this job has no node to run it with.
    """
    import re
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages as pages_module

    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")

    def table(name):
        body = script.split(f"const {name} = {{")[1].split("}")[0]
        return {key.strip('"'): value.strip().strip('"')
                for key, value in re.findall(r'([\w".]+):\s*([^,\n]+)', body)}

    # A subset, since the preview draws four pages: every entry it carries has to agree,
    # and every ref it draws has to be one of them.
    shown = table("NAMES")
    assert shown, "the preview carries no names"
    for ref, name in shown.items():
        assert pages_module.NAMES[ref] == name, (ref, name, pages_module.NAMES.get(ref))
    for group in ("cpu.pct", "cpu.temp", "net.down_bps", "disk.pct", "disk.used_mb"):
        assert group in shown, group
    assert set(re.findall(r'"([\w.]+)"', script.split("const PERCENT_FIELDS = [")[1]
                          .split("]")[0])) == set(pages_module.PERCENT)
    assert set(re.findall(r'"(_\w+)"', script.split("const UNIT_SUFFIXES = [")[1]
                          .split("]")[0])) == set(pages_module.UNIT_SUFFIXES)

    scale = {key: float(value.replace("e6", "e6"))
             for key, value in table("SCALE").items()}
    assert scale == pages_module.SCALE

    # The sizes a page is laid out to are look.py's too.
    import look

    for name, value in (("HEADER_H", look.HEADER_H), ("FOOTER_H", look.FOOTER_H),
                        ("PAD", look.PAD), ("SIZE_TITLE", look.SIZE_TITLE),
                        ("SIZE_HUGE", look.SIZE_HUGE), ("DIAL_OUTER", look.DIAL_OUTER),
                        ("DIAL_INNER", look.DIAL_INNER), ("DIAL_FROM", int(look.DIAL_FROM)),
                        ("DIAL_TO", int(look.DIAL_TO))):
        assert f"const {name} = {value}\n" in script, (name, value)


@check
def test_the_preview_draws_in_the_badge_s_own_faces(_h):
    """Four pages at 320x240, in Lexend and the badge's symbols. The icon font is subset
    from the corpus badge_app/icons.af is built from, so the two cannot differ."""
    import re
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages as pages_module

    web = pathlib.Path("src/statsbadge/web")
    for name in ("lexend-var.ttf", "icons.woff2"):
        assert (web / name).is_file(), f"{name} is not shipped with the UI"
    sheet = (web / "app.css").read_text(encoding="utf-8")
    assert "@font-face" in sheet and "lexend-var.ttf" in sheet and "icons.woff2" in sheet

    corpus = pathlib.Path("ci/badge-icons.txt").read_text(encoding="utf-8")
    rows = [m.groups() for m in
            (re.match(r"^(\w+)\s+([0-9a-f]{4})\s+(\S)\s*$", line)
             for line in corpus.splitlines()) if m]
    assert len(rows) == 18, len(rows)

    # The JS addresses a symbol by the same letter badge-side code does.
    script = (web / "app.js").read_text(encoding="utf-8")
    shown = dict(re.findall(r"(\w): 0x([0-9a-f]{4})", script.split("const ICONS = {")[1]
                            .split("}")[0]))
    assert shown == {letter: point for _name, point, letter in rows}, shown
    drawn = set(pages_module.GROUP_ICONS.values()) | set(pages_module.FIELD_ICONS.values())
    assert drawn <= set(shown), drawn - set(shown)


@check
def test_the_dark_theme_s_colours_are_not_copied_by_hand(h):
    """The UI's accent, its ramp and the mark on its tab all come from the dark theme.

    Generated or checked. A copy typed in goes stale the moment a palette moves."""
    from statsbadge import themes

    dark = themes.written()[themes.DEFAULT]
    def hexed(colour):
        red, green, blue = colour
        return f"#{red:02x}{green:02x}{blue:02x}"

    # Served generated, so the sheet holds no stale copy. Fetched rather than rebuilt,
    # which would check a second copy of the generator instead.
    with urllib.request.urlopen(h.url("/tokens.css"), timeout=5) as response:
        tokens = response.read().decode()
        assert response.headers["Content-Type"].startswith("text/css")
    assert f"--accent: {hexed(dark['accent'])};" in tokens
    for at, colour in enumerate(rgb for _pos, rgb in dark["ramp"]):
        assert f"--ramp-{at}: {hexed(colour)};" in tokens, (at, colour)

    # The splash mark is the badge's, so the tab icon is too. Two copies of it, with
    # icon.svg's comment asking for a test rather than a build step.
    marks = [pathlib.Path("src/statsbadge/web/icon.svg").read_text(encoding="utf-8"),
             pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")]
    for role in ("bg", "grid", "accent", "ink"):
        for mark in marks:
            assert hexed(dark[role]) in mark.lower(), (role, hexed(dark[role]))


@check
def test_the_themes_are_offered_light_and_dark(h):
    """Which of them suits a lit room is the first thing anybody chooses between, so the picker
    groups them by that, read off each palette's background, since a theme that had to
    declare its mode could declare it wrong."""
    records = {record["name"]: record for record in layout.theme_records()}
    assert set(records) == set(layout.THEMES)
    for name, mode in (("dark", "dark"), ("light", "light"), ("frost", "light"),
                       ("sakura", "light"), ("luminescence", "light"), ("shell", "dark"),
                       ("mono", "dark"), ("tinted-light", "light")):
        assert records[name]["mode"] == mode, (name, records[name])
    # The two that came first are named for what they are.
    assert records["dark"]["label"] == "Default Dark"
    assert records["light"]["label"] == "Default Light"
    # Every theme arrives named: the UI holds no rule for turning a slug into a title.
    assert records["sakura"]["label"] == "Sakura"
    assert records["mono-light"]["label"] == "Mono Light"
    assert all(record["label"] for record in records.values()), "a theme arrived unnamed"

    _status, caps = h.raw("GET", "/api/capabilities")
    assert {record["name"] for record in caps["themes"]} == set(layout.THEMES)
    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    assert "optgroup" in script, "the picker is still one flat list"
    assert "record.label" in script
    assert "titleCase(record.name)" not in script, "the UI still titles a theme itself"


@check
def test_a_theme_can_be_derived_from_one_accent(h):
    """The tinted pair are derived: a whole palette from the one accent chosen, so the
    choice is stored and not its result. Restricted to a measured list, every accent on
    offer being checked here, so a pickable one cannot make a page nobody can read."""
    import sys

    from statsbadge import derive

    sys.path.insert(0, install.app_source_dir())
    import look

    derived = {name for name, record in themes.THEMES.items() if "derived" in record}
    assert derived <= set(layout.THEMES)
    assert {themes.THEMES[name]["derived"]["shape"] for name in derived} == set(derive.SHAPES)
    assert len(derive.accents()) == len(derive.ACCENT_HUES) == 12
    assert len(derive.ACCENT_FAMILIES) == 4

    # Every accent of every family, in both modes and both variants. The ramp follows:
    # the even variant travels to red where the accent has somewhere to travel and stays in its
    # accent's hue where it has not, and the bold one always stays in it.
    checked = 0
    for theme in sorted(derived):
      for family in derive.ACCENT_FAMILIES:
        for accent in derive.accents(family):
            palette = layout.palette_for(theme, accent)
            shape = derive.SHAPES[themes.THEMES[theme]["derived"]["shape"]]
            assert (derive.contrast(palette["ink"], palette["bg"])
                    >= shape.get("ink_ratio", derive.INK_RATIO)), (theme, accent)
            assert (derive.contrast(palette["dim"], palette["bg"])
                    >= shape.get("dim_ratio", derive.DIM_RATIO)), (theme, accent)
            # The hot end has to be seen against the page, or a gauge goes blank when it
            # matters most - which is the fault the shipped `cyan` ramp has.
            assert derive.contrast(palette["ramp"][-1][1], palette["bg"]) >= 1.9
            cold, hot = palette["ramp"][0][1], palette["ramp"][-1][1]
            apart = sum((a - b) ** 2 for a, b in zip(cold, hot, strict=True))
            assert apart > 1600, (theme, accent, apart)
            # The badge can build it, as the app does.
            assert look.from_palette("tinted", palette) is not None
            checked += 1
    # Six derived themes, four families, twelve accents.
    assert checked == 288, checked

    reds = [a for a in derive.accents() if derive.ramp_for(a) == "mono"]
    assert reds, "every accent claims it can travel to red"
    assert derive.ramp_for(derive.accents()[6]) == "signal"

    # An unrecognised accent falls back, and the request stands: this arrives from a UI,
    # and a theme is too small a thing to refuse a whole config over.
    kept = layout.validate({"theme": "tinted-dark", "tint": [7, 7, 7],
                            "pages": layout.DEFAULT_PAGES})
    assert tuple(kept["tint"]) in derive.offered()

    # A palette like any other travels, and the badge cannot tell it was derived.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({"theme": "tinted-light", "pages": layout.DEFAULT_PAGES,
                    "tint": list(derive.accents("saturated")[8])})
    sent = config.for_badge()
    assert sent["theme"] == "tinted-light"
    assert set(sent["palette"]) >= {"bg", "panel", "ink", "dim", "accent", "grid", "ramp"}
    assert look.from_palette("tinted", sent["palette"]) is not None

    # One preview path for every theme, tinted or not, so what is shown and what reaches the
    # badge cannot drift apart.
    picked = ",".join(str(part) for part in derive.accents("saturated")[8])
    status, shown = h.raw("GET", f"/api/theme?theme=tinted-light&accent={picked}")
    assert status == 200, (status, shown)
    assert shown["palette"]["bg"] == list(sent["palette"]["bg"]), "the preview would differ"
    status, plain = h.raw("GET", "/api/theme?theme=mono")
    assert status == 200 and plain["palette"]["bg"] == list(themes_bg("mono")), plain
    status, bad = h.raw("GET", "/api/theme?theme=nonesuch")
    assert status == 400, status

    # The UI offers exactly what the host will accept.
    status, caps = h.raw("GET", "/api/capabilities")
    assert {r["name"] for r in caps["themes"] if r["derived"]} == derived
    assert set(caps["accents"]) == set(derive.ACCENT_FAMILIES)
    assert caps["accents"]["saturated"] == [list(a) for a in derive.accents("saturated")]
    web = pathlib.Path("src/statsbadge/web")
    page, script = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    assert "data-tint" in page and 'id="screens"' in page, "no picker or preview in the UI"
    # Which themes take an accent is the host's answer, and the UI holds no list.
    assert "record.derived" in script and "config.tint" in script
    assert "caps.tinted" not in script, "the UI still keeps a list of tinted themes"
    # Clicking along the swatches starts several previews; the last click has to win rather
    # than the last reply, or the panel shows a colour nobody chose.
    assert "previewWanted" in script, "a stale preview reply can win"


@check
def test_a_badge_can_be_given_a_name(h):
    """A badge announces itself by whatever its setup screen was told, which is its id
    until somebody names it - so two badges on one host read the same in the picker."""
    assert h.service.badges.list_badges()[h.badge_id]["name"] == "test"

    status, body = h.raw("PUT", f"/api/badges/{h.badge_id}",
                         json.dumps({"name": "  Desk badge  "}).encode())
    assert status == 200 and body["name"] == "Desk badge", (status, body)
    assert h.service.badges.list_badges()[h.badge_id]["name"] == "Desk badge"

    # Cleared, it goes back to the id, which is at least unique.
    _status, body = h.raw("PUT", f"/api/badges/{h.badge_id}",
                          json.dumps({"name": ""}).encode())
    assert body["name"] == h.badge_id, body

    status, _body = h.raw("PUT", "/api/badges/nobodyhome",
                          json.dumps({"name": "x"}).encode())
    assert status == 404, status

    # `serve` and `status` report the name somebody chose, so a host with two badges says
    # which is which. A badge nobody has named is recorded under its id, and one of those is
    # all there is to print for it.
    assert auth.display_names({}) == []
    assert auth.display_names({
        "e661badge0000001": {"name": "Desk badge"},
        "e661badge0000002": {"name": "e661badge0000002"},
        "e661badge0000003": {},
    }) == ["Desk badge (e661badge0000001)", "e661badge0000002", "e661badge0000003"]


@check
def test_each_badge_has_its_own_layout(h):
    """Everything on the page is configured per badge: two badges on one host draw different
    pages, and a save for one is not a save for the other. A badge that has not been given a
    layout draws the default, which is also what there is to edit before anything is paired."""
    other = "badgetwo00000002"
    other_secret = h.service.badges.provision(other, "second badge")
    try:
        _status, default = h.raw("GET", "/api/config")
        assert "badges" not in default, "the UI is handed every badge's layout at once"

        # The second badge, and only it, is given a layout.
        theirs = dict(default, theme="mono", interval_ms=2000)
        status, saved = h.raw("PUT", f"/api/config?badge={other}",
                              json.dumps(theirs).encode(),
                              {"Content-Type": "application/json"})
        assert status == 200, (status, saved)
        assert saved["badge"] == other and saved["rev"] > default["rev"]

        status, sent = h.raw("GET", "/v1/layout", None,
                             _headers(other, 1, other_secret, path="/v1/layout"))
        assert status == 200, (status, sent)
        assert sent["theme"] == "mono" and sent["interval_ms"] == 2000
        # The table stays behind. It names every other badge paired with this host,
        # which is nothing to do with the one asking.
        assert "badges" not in sent, "a badge is told about every other badge here"

        # The first is still on the default, and its revision has not moved - or every badge
        # would refetch a layout that had not changed.
        _status, mine = h.signed("GET", "/v1/layout")
        assert mine["theme"] == default["theme"], mine["theme"]
        assert mine["rev"] == default["rev"], "a save for one badge moved another's revision"

        # Each watches its layout's revision for a change.
        _status, frame = h.signed("GET", "/v1/stats")
        assert frame["layout_rev"] == default["rev"]
        _status, their_frame = h.raw("GET", "/v1/stats", None,
                                     _headers(other, 2, other_secret))
        assert their_frame["layout_rev"] == saved["rev"]

        # The UI edits one badge at a time, and is told which of them have a layout.
        _status, listing = h.raw("GET", "/api/badges")
        assert listing[other]["configured"] is True
        assert listing[h.badge_id]["configured"] is False

        # The list also carries what each is drawing, so a reader need not open it. Read off
        # the merged layout: a badge on the default reports the default's settings.
        assert listing[other]["theme"] == "mono", listing[other]
        assert listing[other]["interval_ms"] == 2000, listing[other]
        assert listing[h.badge_id]["theme"] == default["theme"], listing[h.badge_id]
        assert listing[h.badge_id]["interval_ms"] == default["interval_ms"]
        assert listing[h.badge_id]["pages"] == len(default["pages"])
        # No secret rides along with any of it.
        assert "secret" not in listing[other], listing[other]
        _status, edited = h.raw("GET", f"/api/config?badge={other}")
        assert edited["theme"] == "mono"

        # A layout cannot be stored against a badge that is not paired here, or a typo in a
        # query string would configure a phantom.
        status, refused = h.raw("PUT", "/api/config?badge=nobody",
                                json.dumps(theirs).encode(),
                                {"Content-Type": "application/json"})
        assert status == 404, (status, refused)

        # An extension doing per-page work is told about every badge's pages: it fetches for
        # all of them at once and keys what it fetched by page id.
        everywhere = {page["id"] for page in h.service.config.all_pages()}
        assert {page["id"] for page in default["pages"]} <= everywhere

        # Forgetting a badge takes its layout with it, or the layout would sit in the file
        # naming an unreachable badge and be handed to whatever next held that id.
        assert h.service.config.configured() == [other]
        h.raw("DELETE", f"/api/badges/{other}")
        assert h.service.config.configured() == []
    finally:
        h.service.badges.forget(other)
        h.service.config.forget(other)

    # A single-badge file is taken as the default, so every badge carries on showing
    # what it showed.
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-layout-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 7, "theme": "mono", "pages": layout.DEFAULT_PAGES}, handle)
    old = layout.Config(path)
    assert old.configured() == []
    assert old.layout_for()["theme"] == "mono"
    assert old.layout_for("anybadge")["theme"] == "mono", "an old file lost its layout"
    assert old.rev_for("anybadge") == 7
    # A revision is always fresh, whichever layout it was last spent on.
    assert old.replace({"pages": layout.DEFAULT_PAGES}, badge_id="anybadge") == 8
    assert old.replace({"pages": layout.DEFAULT_PAGES}) == 9
    assert old.rev_for("anybadge") == 8, "the default's save moved a badge's revision"
    assert old.layout_for("anybadge")["pages"], "a badge's layout was lost"
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # The picker is in the header, where it says what everything below belongs to.
    web = pathlib.Path("src/statsbadge/web")
    page, script = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    header = page[page.index("<header>"):page.index("</header>")]
    for control in ("<label>Badge", 'id="pair"', 'id="save"'):
        assert control in header, control
    # Naming one and forgetting one belong with the badge itself, not beside the picker.
    assert '"Forget"' in script and "function rename(" in script, "no way to forget or name one"
    assert "?badge=" in script, "the UI saves without saying whose layout it is"
    assert "ownIds" in script, "a badge's pages can collide with another's"


def sections_of(page):
    """The config UI's sections, keyed by heading. Only the ones that are a `section`: the
    page list is a column to itself and would otherwise swallow the heading after it."""
    found = {}
    for part in page.split("<h2>")[1:]:
        heading, rest = part.split("</h2>", 1)
        found[heading] = rest.split("</section>")[0]
    return found


@check
def test_the_big_gauge_can_show_the_whole_ramp(_h):
    """A conical gradient follows the arc, so the ramp lays round the gauge with the part
    past the reading left faint, and the scale shows as well as the reading. Only the dial
    page's gauge, which is the one with a page to itself."""
    import sys

    for fill in layout.GAUGE_FILLS:
        assert layout.validate({"gauge_fill": fill,
                                "pages": layout.DEFAULT_PAGES})["gauge_fill"] == fill
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid", (
        "one colour by default")
    assert layout.validate({"gauge_fill": "rainbow",
                            "pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="gaugefill"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.gauge_fill" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.GAUGE_FILL" in applied[:applied.index("\n    def ", 1)]

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    theme = look.get("dark")
    turn = (look.DIAL_TO - look.DIAL_FROM) / 360.0
    fill, track = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)
    assert fill.kind == FakeBrush.CONICAL
    # Fractions of a whole turn, so a 270 degree gauge lays the ramp over three quarters of
    # one, and the ramp's positions in order.
    assert [pos for pos, _ in fill.stops] == [pos * turn for pos, _ in theme.ramp]
    assert [pen for _, pen in fill.stops] == [pen for _, pen in theme.ramp]
    # The track is the same ramp, dimmed by the colours themselves: a gradient brush ignores
    # screen.alpha.
    assert [pos for pos, _ in track.stops] == [pos for pos, _ in fill.stops]
    assert {pen.a for _, pen in track.stops} == {draw.TRACK_ALPHA}
    assert {pen.a for _, pen in fill.stops} == {255}

    # Read backwards for a field whose severity is, so the sweep's end is still the reading's
    # colour it sits at: a battery at 100% is a machine doing well.
    backwards, _ = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, True)
    positions = [pos for pos, _ in backwards.stops]
    assert positions == sorted(positions), positions
    assert backwards.stops[0][1] == theme.ramp[-1][1], "it does not start at the hot end"
    assert backwards.stops[-1][1] == theme.ramp[0][1]
    pages_source = (pathlib.Path(install.app_source_dir()) / "pages.py").read_text(encoding="utf-8")
    assert "backwards=field in GOOD_HIGH" in pages_source, (
        "nothing tells the gradient which way the field is read"
    )

    # Built once a theme: a pair from OKLCH stops is 3.4ms, where moving the geometry is 12us
    # and the arc costs the same to draw either way.
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is fill
    draw.clear_cache()
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is not fill, (
        "a theme change would leave the old ramp round the gauge")

    # The setting settles it, with the solid fill asking for no brush at all.
    seen = {}
    real = draw.gauge
    draw.gauge = lambda *_args, **named: seen.update(named)
    try:
        draw.GAUGE_FILL = "solid"
        draw.dial(theme, 0.5, "50", "%")
        assert seen["swept"] is None
        draw.GAUGE_FILL = "ramp"
        draw.dial(theme, 0.5, "50", "%")
        assert seen["swept"] is not None and len(seen["swept"]) == 2
    finally:
        draw.gauge = real
        draw.GAUGE_FILL = "solid"
        draw.clear_cache()


@check
def test_a_unit_the_badge_cannot_guess_travels_with_the_layout(h):
    """A field named kwh has no suffix to read a unit off, so a graph of it had none.

    The badge answers for the families `fmt` rescales, the suffix pairing with what it
    printed: `_mb` shows as 11.1G, which takes a B and not the MB it arrived in.
    Everything else takes what the host sent.
    """
    import ast

    from statsbadge.sources.base import Source

    class Meter(Source):
        name = "meter"
        provides = ("energy",)
        groups = {"energy": {"label": "Energy", "fields": {
            "kwh": {"label": "Newest half hour", "unit": "kWh"},
            "spend_p": {"label": "Cost", "unit": "p"}}}}

        def sample(self, frame, _dt):
            frame["energy"] = {"kwh": 0.25, "spend_p": 316.8}

    source = Meter({})
    collector = h.service.collector
    collector.extensions.append(source)
    try:
        collector.sample_once()
        caps = collector.capabilities()
        pages = [{"id": "e", "kind": "graph", "title": "Energy",
                  "fields": ["energy.kwh"]},
                 {"id": "m", "kind": "grid", "title": "Mem",
                  "fields": ["mem.used_mb", "sys.uptime_s", "fans.rpm"]}]
        units = layout.field_units(pages, caps)
        assert units.get("kwh") == "kWh", units
        # The model's travel too: a fan's rpm was a bare number on the page.
        assert units.get("rpm") == "rpm", units
        # A field no page draws is not sent.
        assert "spend_p" not in units, units
    finally:
        collector.extensions.remove(source)

    # The badge keeps the rescaled families to itself, or 11.1G would print as 11.1GMB.
    src = pathlib.Path(install.app_source_dir(), "draw.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"fmt", "_several", "_rate", "_size", "_duration", "short_unit", "use_units"}
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in wanted]
    env = {"SEVERAL": 3, "UNITS": {}, "_readings": {}}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "draw", "exec"), env)  # noqa: S102
    env["use_units"]({"used_mb": "MB", "uptime_s": "s", "rpm": "rpm", "kwh": "kWh"})
    shown = {field: env["fmt"](value, field) + env["short_unit"](field)
             for field, value in (("used_mb", 11400.0), ("uptime_s", 273600),
                                  ("rpm", 2200.0), ("kwh", 0.25))}
    assert shown == {"used_mb": "11.1GB", "uptime_s": "3d4h",
                     "rpm": "2200rpm", "kwh": "0.2kWh"}, shown

    # The app takes them where it takes the group names.
    app = pathlib.Path(install.app_source_dir(), "__init__.py").read_text(encoding="utf-8")
    assert 'draw.use_units(self.setting("units"))' in app


@check
def test_a_figure_carries_its_unit_or_is_handed_one(_h):
    """A battery read 86 on the host page, where every other page says 86%.

    `fmt` prints the number and `short_unit` what follows it. A slot with somewhere to put
    the unit passes them separately - a dial has the line under the needle, a trend the
    space beside the big reading. A row that is only a name and a figure has neither, and
    has to ask for the two together."""
    import ast
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import pages

    rows = []
    was = draw.lines
    draw.lines = lambda _theme, entries: rows.extend(entries)
    try:
        pages._text({"fields": ["power.battery_pct", "sys.uptime_s", "sys.host"]},  # noqa: SLF001
                    {"power": {"battery_pct": 86.0},
                     "sys": {"uptime_s": 273600, "host": "workshop-pc"}}, None, None)
    finally:
        draw.lines = was
    # A duration prints its own units and takes none, and a string is not a reading at all.
    assert rows == [("BATTERY", "86.0%"), ("UPTIME", "3d4h"), ("HOST", "workshop-pc")], rows

    # Nowhere else either: a page kind printing a figure has to place the unit somewhere.
    source = pathlib.Path(install.app_source_dir(), "pages.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = {ast.unparse(call.func) for call in ast.walk(node)
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)}
        if "draw.fmt" in calls:
            assert "draw.short_unit" in calls, f"{node.name} prints a figure with no unit"


@check
def test_a_hint_beside_a_secret_leaves_room_for_the_field(_h):
    """An extension's API key sits in a two-column grid whose first column is max-content.

    A hint left in that column sets the track to the width of the paragraph unwrapped, and
    the field beside it collapses: an octopus key with a one-line hint measured 16px. Every
    block of prose in the settings grids spans both columns for this reason.
    """
    import re

    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    block = sheet[sheet.index(".secrets {"):]
    block = block[:block.index("\n}")]
    assert "max-content" in block, "the first column no longer sizes to its content"
    spans = re.search(r"\n\s*p \{([^}]*)\}", block)
    assert spans, "a hint in the secrets block has no rule of its own"
    assert "grid-column: 1 / -1" in spans.group(1), spans.group(1)

    # The block builds a label, a field and a hint per secret, so all three need a column.
    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    built = script[script.index("function secretsBlock"):]
    built = built[:built.index("\n}")]
    assert 'el("p"' in built, "the hint is no longer drawn here"
    for ruled in ("label {", 'input[type="text"] {', "p {", "button {"):
        assert ruled in block, f"{ruled} has no column in .secrets"


@check
def test_the_theme_box_spans_the_panels_beside_it(_h):
    """The theme box stands beside the settings panels, which it does by spanning their
    rows. The span is a count, so it has to match how many there are."""
    import re

    page = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    settings = page.split('aria-label="Settings"')[1].split('<section id="badges">')[0]
    beside = len(re.findall(r"<section(?: class=\"[^\"]*\")?>", settings))
    assert beside == 6, beside

    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    spanned = re.search(r'section\[aria-label="Theme"\] \{ grid-column: 1; grid-row: span (\d+)',
                        sheet)
    assert spanned, "the theme box no longer spans the panels"
    assert int(spanned.group(1)) == beside, (spanned.group(1), beside)

    # Past this a preview stops being a picture of a 320x240 screen and becomes a poster.
    assert "--page: 1280px" in sheet
    assert "max-width: var(--page)" in sheet


@check
def test_the_settings_are_grouped_by_what_they_do(_h):
    """One list of every control was a soup. A setting is grouped under the heading it sits
    under, so the panel can be read by what somebody came to change."""
    page = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    sections = sections_of(page)
    wanted = {
        "Look &amp; Feel": ("theme", "accentb"),
        "Readings": ("interval", "points"),
        "Plots and gauges": ("smooth", "rows", "gaugefill"),
        "Movement": ("plotanim", "animate"),
        "Paging and auto advance": ("slide", "idle", "advance"),
        "Backlight and case lights": ("brightness", "autobright", "caselights"),
    }
    for heading, controls in wanted.items():
        assert heading in sections, heading
        for control in controls:
            assert f'id="{control}"' in sections[heading], (heading, control)
            # In that one only, so a moved control leaves its old place empty.
            for other in wanted:
                assert other == heading or f'id="{control}"' not in sections[other], (
                    control, other)


def themes_bg(name):
    from statsbadge import themes
    return themes.written()[name]["bg"]


@check
def test_the_ui_takes_its_colours_from_the_host(_h):
    """The UI asks for the palette of whatever theme is selected, so there is nowhere for
    a copy to live and drift from the badge's tables."""
    web = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    assert "THEME_COLOURS" not in web, "the UI still carries a palette table"
    assert "/api/theme?" in web, "the UI does not ask the host for a palette"
    # Four pages at 320x240, drawn from what the host sent.
    assert "drawDial" in web and "drawGraph" in web, "the preview does not draw the pages"
    assert "--pv-" not in web + sheet, "the preview still keeps colours in the sheet"
    # The rule for a graph's second series is the badge's, resolved on the host and sent.
    assert "shown.series" in web, "the UI picks the second series itself"
    assert '"series"' in pathlib.Path("src/statsbadge/server.py").read_text(encoding="utf-8")
    # The UI's accent and ramp are generated from the dark theme, not typed in.
    assert "--ramp-0:" not in sheet, "the sheet still declares the ramp by hand"
    assert "--accent:" not in sheet, "the sheet still declares the accent by hand"
    assert "var(--ramp-0)" in sheet, "the sheet stopped using the generated tokens"
    assert "/tokens.css" in pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")


@check
def test_a_full_battery_is_not_an_alarm(_h):
    """The ramp runs calm to alarming and nearly every reading here is a load or a
    temperature, so high is bad. A battery is the other way round and was drawn red at 100%."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages

    assert pages.severity_of("power.battery_pct", 1.0) == 0.0
    assert pages.severity_of("power.battery_pct", 0.1) == 0.9
    # Everything else is coloured by where it actually sits.
    for ref in ("cpu.pct", "cpu.temp", "mem.pct", "disk.pct", "gpu.temp"):
        assert pages.severity_of(ref, 0.9) == 0.9, ref
    assert pages.severity_of("cpu.pct", None) is None

    # It is only the colour: the sweep and the bar are the reading itself.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def gauge("):]
    body = body[:body.index("\ndef ", 1)]
    assert "theme.at(fraction if hot is None else hot)" in body
    assert "shape.arc(middle, inner, outer, start, sweep)" in body, (
        "the sweep is no longer drawn from the reading")


@check
def test_the_badge_dims_to_suit_the_room(_h):
    """Measured on the badge, as raw u16 stepping in sixteens: darkness 48, a partly
    daylit room with the curtains closed 320, a lit room 4500.

    A phone torch and a sunny window sill both read 61400, which is the sensor railed. The
    scale tops out well below that, and everything past it is the same answer."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import look

    dark, curtained, lit, railed = 48, 320, 4500, 61400
    assert look.ambient_fraction(dark) == 0.0
    assert look.ambient_fraction(curtained) < look.ambient_fraction(lit)
    assert look.ambient_fraction(lit) == 1.0
    assert look.ambient_fraction(railed) == look.ambient_fraction(65535) == 1.0
    # The three rooms have to be told apart, or the setting is a switch: a curtained room
    # lands between the two ends.
    assert 0.25 < look.ambient_fraction(curtained) < 0.75, look.ambient_fraction(curtained)
    # Logarithmic: the first doubling is worth as much as the next.
    first = look.ambient_fraction(look.LIGHT_DIM * 2)
    assert 0.4 < first / look.ambient_fraction(look.LIGHT_DIM * 4) < 0.6, first

    # A dim room is dimmer, not dark. Dark is how the setting looks when it is off.
    assert 0.0 < look.LIGHT_FLOOR < 1.0

    # Off by default, since it needs the light sensor and not every board has one.
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["auto_brightness"] is False
    assert layout.validate({"auto_brightness": True,
                            "pages": layout.DEFAULT_PAGES})["auto_brightness"] is True
    assert 'id="autobright"' in pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")


@check
def test_the_badge_pages_on_its_own_when_left_alone(_h):
    """Off by default: a display that moves while somebody is reading it is a nuisance."""
    config = layout.validate({"pages": layout.DEFAULT_PAGES})
    assert config["idle_advance_s"] == 0, config["idle_advance_s"]
    assert config["advance_every_s"] == 10
    clamped = layout.validate({"idle_advance_s": 99999, "advance_every_s": 0,
                               "pages": layout.DEFAULT_PAGES})
    assert clamped["idle_advance_s"] == 3600, clamped
    # A page has to be up for a second to be read at all.
    assert clamped["advance_every_s"] == 1, clamped

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="idle"' in (web / "index.html").read_text(encoding="utf-8")
    assert '"idle_advance_s"' in (web / "app.js").read_text(encoding="utf-8")

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    advance = app[app.index("    def advance_if_idle"):]
    advance = advance[:advance.index("\n    # --", 1)]
    # The turns it makes for itself leave the idle timer alone, or the first
    # one would put it back to sleep.
    assert "_pressed_at" in advance and "self._pressed_at =" not in advance, advance
    assert "len(self.page_list) < 2" in advance, "one page would turn to itself"

    # A press resets it, wherever one is noticed - including HOME, since opening
    # the menu is somebody using the badge.
    for method in ("    def buttons(self):", "    def home(self):"):
        body = app[app.index(method):]
        body = body[:body.index("\n    def ", 1)]
        assert "self._pressed_at = time.ticks_ms()" in body, method
    # Above turn(), which both the buttons and the badge itself go through.
    turn = app[app.index("    def turn(self"):]
    turn = turn[:turn.index("\n    # --", 1)]
    assert "_pressed_at" not in turn, turn


@check
def test_a_button_can_do_something_without_the_host(_h):
    """Paging and the panel are the badge's business: a round trip to change them would
    be slower than the press, and would not work at all with the host away."""
    actions = dict(layout.LOCAL_ACTIONS)
    assert set(actions) == {"badge.prev", "badge.next", "badge.brightness"}, actions

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    press = app[app.index("    def press(self"):]
    press = press[:press.index("\n    def ", 1)]
    assert "LOCAL_PREFIX" in press and "send_command" in press, press
    # Every action the host offers is one the badge answers, or a button does nothing.
    handler = app[app.index("    def local(self"):]
    handler = handler[:handler.index("\n    def ", 1)]
    for action in actions:
        assert f'"{action}"' in handler, action
    # The prefix keeps them off the wire, so it has to match what the host offers.
    for action in actions:
        assert action.startswith("badge."), action


@check
def test_a_press_waits_for_the_poll_rather_than_losing_to_it(_h):
    """One request is in flight at a time and the badge polls every interval, so a press
    that had to find the connection idle mostly found it busy and did nothing."""
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    send = app[app.index("    def send_command(self"):]
    send = send[:send.index("\n    def ", 1)]
    # Held, and served once the connection frees: a press outlives a request in flight.
    assert "self._commands.append" in send, send
    assert "_pending" not in send, send

    poll = app[app.index("    def poll(self"):]
    poll = poll[:poll.index("\n    def ", 1)]
    # Sent ahead of what the badge asks for itself, or the press waits out the interval.
    assert poll.index("if self._commands:") < poll.index("if self._queued is not None:"), poll
    assert poll.index("if self._commands:") < poll.index("self._next_poll"), poll


@check
def test_a_smoothed_graph_still_reads_as_the_data(_h):
    """A curve through the samples, and never near them. This graphs a machine, so a peak
    drawn where there was none, or short of the one there was, misreports it."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw

    values = [0.2, 0.9, 0.3, 0.31, 0.8, 0.1, 0.5]
    dense = draw.curve(values, steps=4)
    assert len(dense) == (len(values) - 1) * 4 + 1, len(dense)
    # Every sample is still on the curve, at the position it was in.
    for index, value in enumerate(values):
        assert abs(dense[index * 4] - value) < 1e-9, (index, dense[index * 4], value)
    # A spline's overshoot is held to the range of the data, or an area fill would run
    # under the baseline where the reading touched zero.
    assert min(dense) >= min(values) and max(dense) <= max(values), (
        min(dense), max(dense))

    # Fewer than three points cannot be interpolated.
    assert draw.curve([0.5, 0.6], steps=4) == [0.5, 0.6]

    # `curve_steps` settles whether to interpolate, answering 1 for "draw it straight".
    # With the switch off, with too few samples, and when the plot is too
    # short for a curve to show - a sparkline is 22px tall and reads the same either way.
    assert draw.curve_steps(250, 150, len(values)) > 1
    assert draw.curve_steps(250, 22, len(values)) == 1
    assert draw.curve_steps(250, 150, 2) == 1
    draw.SMOOTH = False
    try:
        assert draw.curve_steps(250, 150, len(values)) == 1
    finally:
        draw.SMOOTH = True

    # The weights are worked out once: evaluating the polynomial per point cost 265us a
    # point on the badge, which is 50ms for one series.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def curve("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_basis(steps)" in body, "the weights are not taken from the table"

    # An axis with no full scale steps to round numbers, holding off a fit to
    # window, or it creeps on every poll as samples arrive and leave - the plot rescaling
    # slightly each time, which shows as the graph twitching. A byte rate steps in
    # 1024s so the label is a number a reader can place a sample against.
    assert draw.axis_top(900, "down_bps") == 1024
    assert draw.axis_top(6 * 1024 ** 2, "down_bps") == 10 * 1024 ** 2
    assert draw.axis_top(41943040, "down_bps") == 50 * 1024 ** 2
    assert draw.reading(draw.axis_top(41943040, "down_bps"), "down_bps") == "50.0MB/s"
    # Anything else steps in tens, so a temperature plot tops out at 100 and not at 81.6.
    assert draw.axis_top(71.0, "temp") == 100
    assert draw.axis_top(30.0, "temp") == 50
    # It holds still while the busiest sample moves, which is the point.
    for peak in (6.1, 6.5, 7.0, 9.9):
        assert draw.axis_top(peak * 1024 ** 2, "down_bps") == 10 * 1024 ** 2, peak

    # A gap in a ring is a None, and the axis works its top out from the samples. Comparing
    # one against a float is a TypeError, which took the app down mid-slide on a machine
    # with an intermittent sensor.
    axis = source[source.index("def graph("):]
    axis = axis[:axis.index("\ndef ", 1)]
    axis = axis[axis.index("if maximum is None:"):axis.index("    peak_text")]
    assert "is not None" in axis, f"a None in a series reaches max(): {axis}"

    # Every widget draws a gap at the axis, decided in one place: six sites each said
    # `or 0.0` and one of them was reached with the raw series and crashed.
    assert "or 0.0" not in source, "a widget is deciding what a gap looks like on its own"
    assert draw.flat([0.5, None, 0.25]) == [0.5, 0.0, 0.25]
    same = [0.5, 0.25]
    assert draw.flat(same) is same, "a series with no gaps is copied every frame"
    layout = source[source.index("def _lay_out("):]
    layout = layout[:layout.index("\ndef ", 1)]
    assert "values = flat(values)" in layout, layout
    # The series as the ring hands it over, gaps and all, through the widget that crashed.
    gappy = [0.5, None, 0.25, 0.9, None, None, 0.1, 0.4]
    assert draw._lay_out(60, 40, 250, 150, gappy, 1.0, None) > 0  # noqa: SLF001

    # A fill and a line are the same layout with different ends on it, so both go through
    # _lay_out, each scaling its samples once.
    for name in ("def area(", "def line("):
        widget = source[source.index(name):]
        widget = widget[:widget.index("\ndef ", 1)]
        assert "_lay_out(" in widget, f"{name} lays its points out separately"

    # A sparkline is stroked, and how it is stroked sets the cost: a round join is an arc
    # at every sample and 3.5ms a page, where the weight is free.
    trace = source[source.index("LINE_FLAGS = "):]
    trace = trace[:trace.index("\n")]
    assert "JOIN_MITER" in trace and "PATH_OPEN" in trace, trace
    # Centred, or the band grows to one side of the samples it is drawn from.
    assert "ALIGN_CENTER" in trace, trace
    sparks = source[source.index("def sparklines("):]
    sparks = sparks[:sparks.index("\ndef ", 1)]
    assert "line(plot_x" in sparks, "the sparkline page is not drawing lines"
    assert "screen.alpha" not in sparks, "a line does not need to let the page through"


@check
def test_a_plot_is_placed_by_when_its_readings_were_taken(_h):
    """Three clocks are in play. The host samples every `serve --interval`, the badge polls
    every `interval_ms`, and each is blind to the other's rate.

    A plot animated off an index axis has to guess, and did: it walked at the wrong pace,
    paused, and jumped. The host sends how far apart its points are and how old the newest
    is, and everything follows from that."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import pages

    # How many of the host's points one poll covers: both figures known, and divided.
    pages.note_spacing(1000, 1000)
    assert (pages.EVERY_MS, pages.LEAD) == (1000, 1)
    pages.note_spacing(1000, 5000)
    assert pages.LEAD == 5, "a 5s refresh against a 1s host is handed five at a time"
    pages.note_spacing(250, 1000)
    assert pages.LEAD == 4
    pages.note_spacing(1000, 1250)
    assert pages.LEAD == 2, "rounded up, or the plot is short of room"

    # How far back in the series now is: the age the host quoted plus the time since.
    pages.note_spacing(1000, 1000)
    assert pages.behind_at(0, 0) == 0.0
    assert abs(pages.behind_at(200, 300) - 0.5) < 0.001
    assert abs(pages.behind_at(0, 2500) - 2.5) < 0.001
    # A host that has stopped answering does not scroll a plot off into nothing.
    assert pages.behind_at(0, 600_000) == pages.BEHIND_MAX

    # Motion needs the setting, and it is a setting apart: sweeping a gauge and
    # animating a plot are different choices.
    was = pages.PLOT_ANIMATION
    try:
        pages.PLOT_ANIMATION = False
        assert pages._walk() is None
        pages.PLOT_ANIMATION = True
        pages.BEHIND = 0.5
        assert pages._walk() == 0.5
    finally:
        pages.PLOT_ANIMATION = was
        pages.BEHIND = 0.0
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["plot_animation"] is False
    assert layout.validate({"plot_animation": True,
                            "pages": layout.DEFAULT_PAGES})["plot_animation"] is True
    web = pathlib.Path("src/statsbadge/web")
    assert 'id="plotanim"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert 'bindCheck("plotanim", "plot_animation")' in (web / "app.js").read_text(encoding="utf-8"), \
        "it is not bound"

    # A graph keeps room on its right for the samples still coming in. Laid across the
    # width alone it shifts left and leaves a gap that grows and snaps back.
    flat = [50.0] * 48

    def ends(shift, lead=1):
        draw.WALK_LEAD = lead
        try:
            written = draw._lay_out(60, 40, 250, 150, flat, 100.0, shift)
        finally:
            draw.WALK_LEAD = 2
        return draw._points[0], draw._points[written - 2]

    first, last = ends(None)
    assert abs(first - 60) < 0.01 and abs(last - 310) < 0.01, (first, last)
    for lead in (1, 2, 5):
        for tenth in range(11):
            _first, last = ends(lead * tenth / 10.0, lead)
            assert last >= 310 - 0.01, (lead, tenth, last)

    # A series too short to walk is drawn where it stands. `_graph` plots a field it has no
    # ring for as its live reading twice, and a step is the box over the samples on it: two
    # of them put a whole plot width in one reading, which swept a slab across the page and
    # off the side of it every poll.
    for samples in range(2, draw.WALK_MIN):
        held = [50.0] * samples
        written = draw._lay_out(60, 40, 250, 150, held, 100.0, 0.75)
        assert abs(draw._points[0] - 60) < 0.01, (samples, draw._points[0])
        assert abs(draw._points[written - 2] - 310) < 0.01, (samples,
                                                             draw._points[written - 2])
    walked = draw._lay_out(60, 40, 250, 150, [50.0] * draw.WALK_MIN, 100.0, 0.75)
    assert draw._points[0] < 60 - 0.01, "a series long enough to walk is standing still"
    assert walked

    # A sparkline is drawn still whatever the setting says. 22px tall with a sample every
    # 5px, it has nowhere to scroll, and interpolating at fixed x is a translation
    # whatever it is called, which shows as a jump and not as points settling.
    assert pages.SCROLLS == ("graph", "trend"), pages.SCROLLS
    assert "spark" in pages.PLOTS, "it still wants a series fetched for it"
    sparks = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = sparks[sparks.index("def sparklines("):]
    body = body[:body.index("\n# --", 1)]
    assert "shift" not in body, "a sparkline is still being handed an offset"
    assert "if trace is not None:" in body, "a None can still reach the renderer"
    # Two readings is where a field with no history yet lands, and it must still draw.
    assert draw.line(0, 0, 470, 30, [5.0, 5.0], 47.0) is not None
    assert draw.line(0, 0, 470, 30, [5.0], 47.0) is None

    # Every page that draws a series asks for one, not only the graph pages: a sparkline was
    # plotting the live value twice, a flat line whatever the machine was doing.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    refs = app[app.index("    def _plot_refs(self"):]
    refs = refs[:refs.index("\n    def ", 1)]
    assert "pages_module.PLOTS" in refs, "only the graph pages ask for a series"

    # One poll cannot ask for every ref a layout plots, and the ones it does ask for start
    # at the page on screen. In page order the refs at the end were never fetched at all,
    # and the page holding them drew its live reading twice instead of its history.
    keys = app[app.index("    def _graph_keys(self):"):]
    keys = keys[:keys.index("\n    def ", 1)]
    assert "self._plot_refs(self.page_index)" in keys, "the ask does not follow the reader"
    assert "[:GRAPH_KEYS]" in keys, "the ask is unbounded"
    plotted = {ref for page in layout.DEFAULT_PAGES if page.get("kind") in pages.PLOTS
               for ref in page.get("fields", []) + [page.get("field")] if ref}
    cap = int(re.search(r"^GRAPH_KEYS = (\d+)", app, re.M).group(1))
    assert cap >= len(plotted), (cap, sorted(plotted))
    # Merged into what is held, or turning a page drops the rings the last ask covered and
    # every plot on the new one falls back to a pair until the next poll lands.
    landed = app[app.index('elif what == "history":'):]
    landed = landed[:landed.index("\n        self.dirty", 1)]
    assert "self.history.update(" in landed, "a reply replaces the rings it did not carry"
    assert "self._plot_refs()" in landed, "nothing drops a ring the layout stopped plotting"
    # It comes with its age, every poll, on the stats' schedule. v=3, since
    # a source may answer for a ring that is not on the host's clock.
    assert "&v=3" in app, "the series is fetched without the times it needs"

    # A ring a source answers for itself is on whatever clock its readings are really on -
    # an hour, for a domain's traffic - and a plot is translated as a whole, so one of those
    # is drawn still. Walking it by a number counted in the collector's samples would slide
    # it a year an hour.
    try:
        pages.note_series_spacing({"cf_pinout_xyz.requests": {"every_ms": 3600000}})
        pages.PLOT_ANIMATION = True
        pages.BEHIND = 0.5
        assert pages._walk(("cpu.pct",)) == 0.5
        assert pages._walk(("cf_pinout_xyz.requests",)) is None
    finally:
        pages.note_series_spacing({})
        pages.PLOT_ANIMATION = was
        pages.BEHIND = 0.0
    assert "note_spacing" in app and "behind_at" in app


@check
def test_the_notice_screen_offers_a_way_out(_h):
    """The screen a badge sits on when it can reach nothing, so it has to say what can be
    done as well as what went wrong.

    Polls back off to fifteen seconds apart while a host is quiet, which is no use to
    somebody who has just woken the PC."""
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")

    notice = app[app.index("    def render(self):"):]
    notice = notice[:notice.index("\n    def ", 1)]
    for action in ("C retry", "B set up", "HOME hosts"):
        assert action in notice, action
    assert "self.detail" in notice, "the reason is not shown"

    # C asks again there, the host commands being out of reach.
    pressed = app[app.index("    def buttons(self):"):]
    pressed = pressed[:pressed.index("\n    def ", 1)]
    assert "self.retry()" in pressed and "current_page() is None" in pressed

    # Retrying drops the backoff without waiting it out, and clears what was in flight.
    retry = app[app.index("    def retry(self):"):]
    retry = retry[:retry.index("\n    def ", 1)]
    for cleared in ("self.client.failures = 0", "self._next_poll", "self._queued = None",
                    "self._pending = None"):
        assert cleared in retry, cleared

    # One failed poll is enough to offer setup: waiting for three left that screen with
    # a screen of controls that all did nothing.
    setup = app[app.index("    def needs_setup(self):"):]
    setup = setup[:setup.index("\n    def ", 1)]
    assert "self.client.failures >= SETUP_AFTER" in setup, setup
    assert "SETUP_AFTER = 1" in app, "more than one failed poll before setup is offered"


@check
def test_switching_host_forgets_the_old_one_the_same_way(_h):
    """Three ways to leave one host for another, and all of them reset through one method.

    The beacon in hunt(), the hosts menu, and setup reached from the app. Readings, series
    and revisions are numbered by whoever sent them, so anything held is drawn under the
    new host's name.
    """
    app_dir = pathlib.Path(install.app_source_dir())
    app = (app_dir / "__init__.py").read_text(encoding="utf-8")
    menu = (app_dir / "setup.py").read_text(encoding="utf-8")

    forget = app[app.index("    def forget_host(self):"):]
    forget = forget[:forget.index("\n    def ", 1)]
    for cleared in ("self.layout = None", "self.layout_rev = NO_REV", "self.history = {}",
                    "self.slow = {}", "self.slow_rev = NO_REV", "self._queued = None",
                    "self._commands = []", "self._series_age = 0", "self._series_at = 0",
                    "self.rejected = False", "draw.clear_cache()"):
        assert cleared in forget, cleared

    # Reset nowhere else, or the paths go back to disagreeing. Twice in the app: once as a
    # starting value and once here.
    for cleared in ("self.slow = {}", "self.history = {}", "self.slow_rev = NO_REV"):
        assert app.count(cleared) == 2, cleared
    assert "app.layout" not in menu, "the menu is resetting state of its own"
    assert menu.count("forget_host()") == 2, "a host joined is a host switched to"


@check
def test_a_press_that_closes_a_modal_screen_stops_there(_h):
    """B on the hosts menu picked a server and then fired the page binding behind it.

    A modal screen returns the moment its button goes down, so the edge is still standing
    when the loop reaches `buttons()`. `badge.poll()` rolls it forward first.
    """
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")

    loop = app[app.index("def main():"):]
    menu = loop[loop.index("pairing_ui().hosts_menu(app)"):]
    handled = menu[:menu.index("app.buttons()")]
    assert "badge.poll()" in handled, (
        "the menu press reaches buttons(): " + handled)


@check
def test_sparkline_rows_can_be_told_apart(_h):
    """Six lines on one page looked like one plot with six traces, so the rows are banded.

    The band is worked out from the theme and not named in a palette: a step of
    lightness from the page, which is a step in the same direction whatever the page is.
    """
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    for style in layout.ROW_STYLES:
        assert layout.validate({"rows": style,
                                "pages": layout.DEFAULT_PAGES})["rows"] == style
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["rows"] == "zebra", (
        "banded by default")
    assert layout.validate({"rows": "stripey",
                            "pages": layout.DEFAULT_PAGES})["rows"] == "zebra"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="rows"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.rows" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    for style in layout.ROW_STYLES:
        assert f'value="{style}"' in (web / "index.html").read_text(encoding="utf-8"), style

    # The badge applies it where it applies the rest of the layout.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.ROWS" in applied[:applied.index("\n    def ", 1)]

    # A lift, not the panel colour: a panel can be a different hue as well as a different
    # level, which on a near-black page draws a stripe of colour.
    dark = look.THEMES["dark"]
    assert (dark.stripe.r - dark.bg.r == dark.stripe.g - dark.bg.g
            == dark.stripe.b - dark.bg.b == look.STRIPE), "the band shifts hue"
    # Toward the ink on a dark page and away from it on a pale one, since lighten has
    # nowhere to go on a background that is already near white.
    from statsbadge import themes

    pale = look.from_palette("light", themes.written()["light"])
    assert pale.pale and not dark.pale
    assert pale.stripe.r < pale.bg.r and dark.stripe.r > dark.bg.r

    # The axis rule under a plot is drawn only where the rows are otherwise unseparated.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    sparks = source[source.index("def sparklines("):]
    sparks = sparks[:sparks.index("\ndef ", 1)]
    assert "if ROWS == ROW_NONE:" in sparks, "the axis is drawn whatever separates the rows"
    assert draw.ROWS == "zebra" and draw.ROW_NONE == "none"


@check
def test_a_symbol_centres_on_the_words_beside_it(_h):
    """An icon and a string on one baseline do not line up: the icon's box stands a fifth
    taller than a capital and its ink sits in the middle of that box, so the symbol floats.
    """
    import sys

    sys.path.insert(0, install.app_source_dir())
    sys.path.insert(0, str(pathlib.Path("tools")))
    import draw
    import read_af

    # The placement holds only while an icon's ink is centred in a box sat on the baseline,
    # so that is read out of the fonts every time. Through the tool, so a font
    # repacked wide is read as wide, the flag saying which.
    fonts = (pathlib.Path(install.app_source_dir()) / "icons.af",
             pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"
                          "/icons.af"))
    for path in fonts:
        font = read_af.read(str(path))
        box = draw.ICON_BOX * font["units_per_em"]
        for glyph in font["glyphs"]:
            if not glyph["contours"]:
                continue
            assert -1 <= glyph["bbox_y"] and glyph["bbox_y"] + glyph["bbox_h"] <= box + 1, (
                path.name, glyph, box)
            assert abs(glyph["bbox_y"] - (box - glyph["bbox_h"]) / 2.0) <= 1, (
                f"{path.name} {chr(glyph['codepoint'])!r} is not centred in its box")

    text_y, text_size, icon_size = 100, 26, 32
    icon_y = draw.icon_baseline(text_y, text_size, icon_size)
    cap_middle = text_y + text_size * (1.0 - draw.CAP / 2.0)
    ink_middle = icon_y + icon_size * (1.0 - draw.ICON_BOX / 2.0)
    assert abs(cap_middle - ink_middle) <= 1, (cap_middle, ink_middle)
    # Lower than a shared baseline puts it, which was the bug.
    assert icon_y > text_y + text_size - icon_size


@check
def test_the_shipped_fonts_are_packed_as_the_metrics_assume(_h):
    """draw.CAP and draw.ICON_BOX are fractions of the size a string is drawn at, and hold
    only while the fonts keep the em those numbers came from. A wide font is the same ratios
    at a finer grid, so nothing here cares which a font is - but one repacked to different
    proportions would move every symbol and mis-size every big number."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    sys.path.insert(0, str(pathlib.Path("tools")))
    import draw
    import read_af

    text = read_af.read(str(pathlib.Path(install.app_source_dir())
                            / "fonts" / "lexend-regular.af"))
    cap = next(g for g in text["glyphs"] if g["codepoint"] == ord("H"))
    assert abs(cap["bbox_h"] / text["units_per_em"] - draw.CAP) < 0.01, (
        cap["bbox_h"], text["units_per_em"], draw.CAP)

    # The LCD face's digits stand where a capital does, or the clock sizes one of its faces
    # by numbers that do not describe it.
    lcd = read_af.read("extensions/statsbadge-clock/src/statsbadge_clock/badge/lcd.af")
    eight = next(g for g in lcd["glyphs"] if g["codepoint"] == ord("8"))
    assert abs(eight["bbox_h"] / lcd["units_per_em"] - draw.CAP) < 0.01, (
        eight["bbox_h"], lcd["units_per_em"], draw.CAP)

    # The digital face's digits are the app's face at a finer grid, so they have to agree
    # with it on both counts: the cap it is sized from and the width it is placed by. A
    # mismatch draws a time that is the wrong height or does not sit in its column.
    digits = read_af.read(
        "extensions/statsbadge-clock/src/statsbadge_clock/badge/digits.af")
    assert digits["wide"], "the face that draws digits 84pt tall wants the finer grid"
    for char in "0123456789:":
        assert any(g["codepoint"] == ord(char) for g in digits["glyphs"]), char
    for char in ("H", "0"):
        theirs = next(g for g in digits["glyphs"] if g["codepoint"] == ord(char))
        ours = next(g for g in text["glyphs"] if g["codepoint"] == ord(char))
        assert abs(theirs["bbox_h"] / digits["units_per_em"]
                   - ours["bbox_h"] / text["units_per_em"]) < 0.01, char
        assert abs(theirs["advance"] / digits["units_per_em"]
                   - ours["advance"] / text["units_per_em"]) < 0.01, char


@check
def test_the_clock_only_syncs_from_a_fresh_reading(_h):
    """A frame is drawn forty-five times a second and holds the time it was polled at, so a
    stale reading treated as authority drags the hands back to it. Measured on the badge: with
    the reading reconsidered every frame the clock jumped back 30s at 31s, and again after."""
    badge = pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge")
    source = (badge / "clockface.py").read_text(encoding="utf-8")

    resync = source[source.index("def _resync("):]
    resync = resync[:resync.index("\n\n\n") if "\n\n\n" in resync else len(resync)]
    assert "_synced_seq" in resync, "every frame reconsiders the same reading"
    assert resync.index("_synced_seq") < resync.index("RTC()"), (
        "the clock is set before the reading is checked for being a new one")

    # Synced from the host's clock alone: there is one hardware clock and two pages
    # in two zones would set it to theirs each time you turned to them.
    render = source[source.index("def render(page"):]
    render = render[:render.index("\n\n\n") if "\n\n\n" in render else len(render)]
    assert "_resync(host," in render, render[:400]
    assert "_zone_offset(host, here)" in render, "a page elsewhere is not offset from the host"


@check
def test_every_clock_face_the_ui_offers_can_be_drawn(_h):
    """The face list is host side and the renderers are badge side, so a face added to one
    and not the other is a page that draws the default and says nothing."""
    badge = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"))
    source = (badge / "clockface.py").read_text(encoding="utf-8")
    try:
        from statsbadge_clock import Clock
    except ImportError:
        return              # the extension is not pip installed in this environment

    offered = next(s for s in Clock.page_settings if s["key"] == "face")["options"]
    # The dials and the dial-less faces are two tables; between them they are the renderers.
    drawn = set()
    for table in ("FACES = {", "DIGITAL = {"):
        block = source[source.index(table):]
        block = block[:block.index("\n}\n")]
        drawn.update(re.findall(r'^    "([a-z]+)": \{', block, re.M))
    assert set(offered) == drawn, (sorted(offered), sorted(drawn))

    # The seven-segment face needs a font, which is an asset and not code, so it
    # travels only if it is declared.
    assert any(path.endswith("lcd.af") for path in Clock.badge_assets), Clock.badge_assets
    assert (badge / "lcd.af").exists(), "the LCD face's font is not built"
    # Shipped, so its licence ships with it.
    licence = pathlib.Path("licences/OFL-DSEG.txt").read_text(encoding="utf-8")
    assert "keshikan" in licence and "SIL Open Font License" in licence

    # The unlit segments go down before the lit ones, or they cover them.
    body = source[source.index("def _digital"):]
    body = body[:body.index("\ndef ", 1)]
    assert body.index('spec["ghost"]') < body.index("draw.blit_label(hours,"), (
        "the ghost is drawn over the digits")


@check
def test_the_version_is_written_down_once(_h):
    """Nowhere, in fact; the tag is the version.

    A number in pyproject.toml and a `__version__` beside it were two things to bump, with
    one of them silently stale. The build reads the tag, so releasing the wrong number takes
    tagging the wrong number.

    Two things have to agree for that to work, though, and they are in different files: the tag
    prefix a workflow fires on, and the prefix the build strips to get a version."""
    import statsbadge

    source = pathlib.Path("src/statsbadge/__init__.py").read_text(encoding="utf-8")
    # The assignment, since the word itself appears in the docstring above.
    assert not re.search(r"^__version__\s*=", source, re.M), "a second copy of the version"
    assert statsbadge.version(), "nothing can say what is installed"

    with open("pyproject.toml", "rb") as handle:
        main = tomllib.load(handle)
    assert main["project"].get("version") is None, "a static version is back"
    assert "version" in main["project"]["dynamic"], main["project"]
    # Hatchling leaves out what the VCS ignores, and the precompiled app is a build artefact:
    # without this the wheel ships sources alone and the badge compiles them at every launch.
    artifacts = main["tool"]["hatch"]["build"]["artifacts"]
    assert any("badge_app/mpy" in entry for entry in artifacts), artifacts

    workflows = pathlib.Path(".github/workflows")
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with open(pyproject, "rb") as handle:
            plugin = tomllib.load(handle)
        name = plugin["project"]["name"]
        short = name.removeprefix("statsbadge-")
        assert plugin["project"].get("version") is None, name
        assert "version" in plugin["project"]["dynamic"], name
        # Its tags and nobody else's, or a release of one extension versions them all.
        prefix = plugin["tool"]["uv-dynamic-versioning"]["pattern-prefix"]
        assert prefix == f"{short}-", (name, prefix)
        # The prefix the workflow fires on is the prefix the build strips.
        workflow = (workflows / f"publish-{short}.yml").read_text(encoding="utf-8")
        assert f"TAG_PREFIX: {prefix}v" in workflow, (short, prefix)
        for module in (directory / "src").rglob("__init__.py"):
            assert not re.search(r"^__version__\s*=", module.read_text(encoding="utf-8"), re.M), module


@check
def test_a_picture_is_cropped_to_what_is_in_it(_h):
    """A feed's picture, small enough to send and indexed so a theme can own it.

    Indices on a ramp the badge assigns travel, and not colours, so one image suits
    every badge whatever theme it is on, and the host never has to know which.
    """
    from PIL import Image

    from statsbadge import imaging

    # A wide picture with everything happening down the right-hand end. A crop by the middle
    # would take the flat grey, which on a photograph is the wall behind the subject.
    source = Image.new("L", (400, 100), 128)
    for x in range(320, 400):
        for y in range(0, 100, 2):
            source.putpixel((x, y), 255 if x % 2 else 0)
    box = imaging._best_crop(source, 1.0)
    assert box[2] - box[0] == 100 and box[3] - box[1] == 100, box
    assert box[0] > 250, f"the crop missed what was in the picture: {box}"

    raw = io.BytesIO()
    source.save(raw, format="PNG")
    data = raw.getvalue()

    for (preset, orientation), (width, height) in imaging.SIZES.items():
        png = imaging.thumbnail(data, preset, orientation)
        assert png.startswith(b"\x89PNG\r\n\x1a\n"), preset
        got_w, got_h, depth, colour = struct.unpack(">IIBB", png[16:26])
        assert (got_w, got_h) == (width, height), (preset, orientation, got_w, got_h)
        assert colour == 3, "not an indexed PNG"
        # Two bits a pixel at four colours, which is the quarter-size the point of this,
        # and four at eight - PNG has no three-bit depth.
        assert depth == imaging.DEPTHS[imaging.LEVELS[preset]], (preset, depth)
        assert b"PLTE" in png

        # Pillow reads back what was written, every index landing inside the palette
        back = Image.open(io.BytesIO(png))
        assert back.mode == "P" and back.size == (width, height), (back.mode, back.size)
        assert max(back.tobytes()) < imaging.LEVELS[preset], "an index past the ramp"

    # An unreadable one is a line somebody can act on, in place of a traceback out of Pillow
    try:
        imaging.thumbnail(b"not a picture at all")
    except imaging.ImagingError as exc:
        assert "cannot read" in str(exc), exc
    else:
        raise AssertionError("anything at all was accepted as a picture")


@check
def test_an_api_key_is_masked_until_it_is_asked_for(_h):
    """The config page sits open on a desk all day, and a token is readable across a room.

    Masked and not hidden: "not set" and "set to the wrong one" have to be told apart,
    and the first few characters are enough for somebody checking to recognise.
    """
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    assert "function masked(" in ui and "Edit secrets" in ui
    # A secret does not go in the ordinary run of rows, or it would be on screen anyway
    assert "if (setting.secret) continue" in ui, "a secret is still drawn with the rest"
    # Reopened by name, so a redraw does not close the box under someone's typing
    assert "editingSecrets" in ui

    # Whatever declares one is stored and coerced like any other setting: masking is the
    # UI's business, and the host has to hand the value back or it could not be edited.
    schema = {"thing": [{"key": "api_token", "type": "text", "secret": True}]}
    stored = layout.validate({**layout.DEFAULT_CONFIG,
                              "settings": {"thing": {"api_token": "sekrit"}}},
                             (), schema)["settings"]
    assert stored["thing"] == {"api_token": "sekrit"}, stored


@check
def test_a_number_setting_is_held_to_its_bounds(_h):
    """A reading's units, and how far it can go, are the extension's to declare.

    The browser stops the spinner and marks a field out of range, but a value typed straight
    into one still arrives, so the floor is held to on this side as well. It said "Seconds"
    and why in a note under the field before, which is neither enforceable nor brief.
    """
    schema = {"thing": [{"key": "every", "type": "number", "min": 60, "max": 3600,
                         "unit": "seconds"},
                        {"key": "loose", "type": "number"}]}

    def stored(settings):
        return layout.validate({**layout.DEFAULT_CONFIG, "settings": {"thing": settings}},
                               (), schema)["settings"]["thing"]

    assert stored({"every": 5, "loose": 5}) == {"every": 60.0, "loose": 5.0}
    assert stored({"every": 9999, "loose": 9999}) == {"every": 3600.0, "loose": 9999.0}
    assert stored({"every": 120, "loose": None}) == {"every": 120.0, "loose": None}

    # The UI draws one as a number, with the bounds on the field.
    ui = (pathlib.Path("src/statsbadge/web") / "app.js").read_text(encoding="utf-8")
    assert 'setting.type === "number"' in ui, "a number setting is still a text box"
    assert "setting.unit" in ui, "nowhere to put what it is counted in"


@check
def test_a_notifications_page_sorts_messages_from_counters(_h):
    """One slot list holding two sorts of thing, told apart by looking at the reading.

    That lets one page kind be a feed, a mention, a headline and a follower count in
    whatever mixture: a message is a dict carrying `text`, everything else is a number. The
    alternative was two slot lists and a UI that has to know which is which.
    """
    sys.path.insert(0, install.app_source_dir())
    import draw
    import look
    import pages

    frame = {"feed": {
        "home": {"title": "Maaike", "text": "a post", "age_s": 420, "note": "boosted"},
        "mention": {"title": "dinkster75", "text": "a mention", "age_s": 34200},
        "followers": 1350, "posts": 6466}}

    drawn = {}
    was = draw.notification
    draw.notification = lambda _theme, items, counters: drawn.update(
        items=items, counters=counters)
    try:
        def render(fields):
            drawn.clear()
            pages._notify({"kind": "notify", "fields": fields}, frame, {},
                          look.get("dark"))
            return drawn

        out = render(["feed.home", "feed.mention", "feed.followers", "feed.posts"])
        assert [item["title"] for item in out["items"]] == ["Maaike", "dinkster75"]
        assert out["counters"] == [("FOLLOWERS", "1350"), ("POSTS", "6466")], out["counters"]

        # Order in the slot list does not have to be messages first
        out = render(["feed.followers", "feed.home"])
        assert len(out["items"]) == 1 and len(out["counters"]) == 1, out

        # A page of only one or the other still draws
        assert render(["feed.home"])["counters"] == []
        assert render(["feed.followers"])["items"] == []
        # A field the host stopped producing draws a counter of "--", where it crashed
        assert render(["feed.gone"])["counters"] == [("GONE", "--")], render(["feed.gone"])
    finally:
        draw.notification = was

    # The message shape is four things, and the age is drawn to suit its size. Minutes up
    # to ninety, then hours, then days, off the one function the quake page reads.
    assert [draw.ago(s) for s in (None, 5, 90, 4000, 100000, 400000)] == [
        None, "just now", "1m ago", "66m ago", "27h ago", "4d ago"]


def rules_of(css):
    """Every rule in the sheet, as its full selector and the declarations under it. The
    sheet nests, so a rule's selector is the chain of the ones it sits inside."""
    chain, declarations, found, buffer = [], [], [], ""
    for char in css:
        if char == "{":
            above, _, selector = buffer.rpartition(";")
            if declarations:
                declarations[-1] += above
            chain.append(selector.strip())
            declarations.append("")
            buffer = ""
        elif char == "}":
            declarations[-1] += buffer
            found.append((" ".join(chain), declarations[-1]))
            chain.pop()
            declarations.pop()
            buffer = ""
        else:
            buffer += char
    return found


@check
def test_a_hidden_row_is_actually_hidden(_h):
    """The browser's rule for `hidden` is one attribute selector, so anything naming a
    class or an attribute outranks it and the row stays on screen.

    The second accent takes `display: flex`, to sit its swatch beside the select, and showed
    for every theme - where only a derived palette works one out.
    """
    web = pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
    css, markup = (web / "app.css").read_text(encoding="utf-8"), (web / "index.html").read_text(encoding="utf-8")

    class Hidden(html.parser.HTMLParser):
        """Every element the page starts out hiding, and how a rule could name it."""

        def __init__(self):
            super().__init__()
            self.depth, self.found = 0, []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "hidden" in attrs:
                named = {tag} | {f".{name}" for name in attrs.get("class", "").split()}
                named |= {f"[{key}]" for key in attrs if key.startswith("data-")}
                self.found.append((tag, self.depth, named))
            if tag not in ("input", "link", "meta", "br"):
                self.depth += 1

        def handle_endtag(self, _tag):
            self.depth -= 1

    parser = Hidden()
    parser.feed(markup)
    # The sheet each tab shows, and the rows only a derived palette has.
    assert len({depth for _tag, depth, _named in parser.found}) > 1, parser.found

    # A rule that gives one of them a display has to stand aside when it is hidden. The
    # two selectors are otherwise close enough for source order to settle which wins.
    for selector, declarations in rules_of(css):
        last = re.split(r"[\s>+~]+", selector)[-1]
        if "display:" not in declarations or "[hidden]" in selector:
            continue
        if not (selector.startswith("main") or selector == last):
            continue            # it cannot reach inside a sheet
        for tag, depth, named in parser.found:
            if tag == "section":
                continue        # the sheets, hidden by a rule elsewhere
            for each in named:
                found = (re.search(rf"\b{each}\b", last) if each.isalpha()
                         else each in last)
                assert not found, (selector, tag, depth, each)


@check
def test_a_picture_is_cropped_to_the_block_it_is_in(_h):
    """A message three to a page has 52px of block and the large preset is 96 tall.

    Cropped, holding off any scale: the pixels are palette indices, so halfway between two of
    them is a third colour and not a blend of the two.
    """
    sys.path.insert(0, install.app_source_dir())
    import draw

    class FakeRect:
        """`rect` is the firmware's; the crop only needs somewhere to put four numbers."""

        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    class Picture:
        """Enough of an indexed image to be cropped: a size, and a view of part of it."""

        def __init__(self, width, height):
            self.width, self.height, self.taken = width, height, None

        def window(self, box):
            self.taken = box
            return Picture(box.w, box.h)

    was = getattr(builtins, "rect", None)
    builtins.rect = FakeRect
    try:
        _check_cropping(draw, Picture)
    finally:
        if was is None:
            del builtins.rect
        else:
            builtins.rect = was


def _check_cropping(draw, Picture):
    # Room to spare, so it is drawn whole and kept
    whole = Picture(128, 96)
    assert draw.fitted(whole, 96) is whole and whole.taken is None
    assert draw.fitted(whole, 200) is whole and whole.taken is None

    # Two messages to a page: 78px of block, less its padding
    tall = Picture(128, 96)
    band = draw.fitted(tall, 70)
    assert (band.width, band.height) == (128, 70), (band.width, band.height)
    # From the middle, the crop that made the picture having put what matters there
    assert (tall.taken.x, tall.taken.y) == (0, 13), (tall.taken.x, tall.taken.y)

    # Below a band worth looking at, none: a smear is worse than the room it takes
    assert draw.fitted(Picture(128, 96), 12) is None
    assert draw.fitted(None, 70) is None


@check
def test_a_message_shortens_the_way_the_firmware_does(_h):
    """A post is whatever length it is and the block has room for two or three lines.

    The firmware flows and truncates, `screen.text` taking a rect and an overflow, so this
    checks the page asks for that and reimplements none of it. Doing that here is a
    `measure_text` a word to find the breaks and another per character to trim the last
    line, in Python, on every draw. Measured on a Tufty, that was the page at 34.9ms
    settled against 24.8 for the same page drawn by the firmware.
    """
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def flow("):source.index("def picture(")]
    assert "overflow=ELLIPSES" in body, "the page is not asking for the truncation"
    assert "screen.measure_text(" not in body, "still measuring text to lay it out"
    assert "def wrap(" not in source, "the hand-rolled wrapper is still here"

    # `fit` is still needed for a single line - a name beside a time - and halves rather
    # than trimming a character per measurement.
    fitting = source[source.index("def fit("):]
    fitting = fitting[:fitting.index("\n\n\n")]
    assert "low, high" in fitting and "middle" in fitting, "fit is back to one at a time"


@check
def test_a_plugin_wanting_a_newer_statsbadge_is_explained(_h):
    """An extension asking for a newer statsbadge cannot have one: the library installs
    beside statsbadge and never over it. What is checked here is that the installer's
    prose comes out as something to act on.

    Its last line is "your requirements are unsatisfiable", which is true of every
    resolution failure and says nothing about the versions - and the versions are the whole
    of it. uv wraps its prose to the terminal, so the phrase spans the fold."""
    from statsbadge import tooling

    said = (
        "  × No solution found when resolving dependencies:\n"
        "  ╰─▶ Because all versions of statsbadge-cloudflare depend on\n"
        "      statsbadge>=1.1.0 and you require statsbadge==1.0.0, we can conclude\n"
        "      that your requirements and all versions of statsbadge-cloudflare are\n"
        "      incompatible.\n"
        "      And because you require statsbadge-cloudflare, we can conclude that your\n"
        "      requirements are unsatisfiable.\n")
    line = tooling.explain(said)
    assert line == ("statsbadge-cloudflare needs statsbadge>=1.1.0, and this is "
                    "statsbadge==1.0.0"), line
    # The extension is named, so the caller can tell a plugin just asked for from one
    # that was already in the list.
    assert tooling.blamed(line) == "statsbadge-cloudflare", tooling.blamed(line)
    assert tooling.blamed(said) == "statsbadge-cloudflare"

    # An unknown name is answered as one. uv reports it as a resolver error, which
    # otherwise reads as a version clash.
    assert tooling.explain("error: Because nosuchthing was not found in the package "
                           "registry and you require nosuchthing, we can conclude that "
                           "your requirements are unsatisfiable.") == (
        "no such package: nosuchthing")

    # The build refuses before it promotes anything, so a generation is never left
    # holding an extension that cannot run.
    with tempfile.TemporaryDirectory() as directory:
        where, why = library.build(directory, ["statsbadge-quakes>=99"])
        assert where is None and why, (where, why)
        assert library.generations(directory) == [], "a failed build was promoted"


@check
def test_an_extension_using_a_new_feature_says_which_statsbadge_it_needs(_h):
    """Installed against a host too old, `groups` and `series` are read by nothing.

    It stays quiet: an older collector leaves them alone, so the readings are absent
    from the pickers and a slow group goes out sixty times a minute, both silently. A floor
    in the dependency turns that into a resolver error somebody can act on.
    """
    marks = ("groups = {", "def series(self)")
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        source = "\n".join(path.read_text(encoding="utf-8")
                           for path in sorted(directory.rglob("src/**/__init__.py")))
        if not any(mark in source for mark in marks):
            continue
        with open(pyproject, "rb") as handle:
            requires = tomllib.load(handle)["project"]["dependencies"]
        pinned = [need for need in requires if need.startswith("statsbadge")]
        assert pinned and ">=" in pinned[0], (
            f"{directory.name} declares a group or a series against an unpinned "
            f"statsbadge: {requires}")


@check
def test_every_package_here_can_be_published(_h):
    """Four packages share this repository. PyPI's trusted publishing matches on a workflow
    filename, so an extension with no workflow cannot be published at all, and every
    release fires every workflow, so each has to test its tag prefix or they all try."""
    workflows = pathlib.Path(".github/workflows")
    main = (workflows / "publish.yml").read_text(encoding="utf-8")
    # The top-level package takes the plain tags, and lets an extension's release alone.
    assert "startsWith(github.event.release.tag_name, 'v')" in main

    found = []
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with open(pyproject, "rb") as handle:
            name = tomllib.load(handle)["project"]["name"]
        short = name.removeprefix("statsbadge-")
        found.append(short)

        workflow = workflows / f"publish-{short}.yml"
        assert workflow.is_file(), f"{name} has no publish workflow"
        text = workflow.read_text(encoding="utf-8")
        assert f"PACKAGE: {name}" in text, workflow
        assert f"DIRECTORY: extensions/{name}" in text, workflow
        # The prefix appears twice in each workflow: the guard that gates the run, and
        # the strip that checks the version. Both have to match.
        assert f"TAG_PREFIX: {short}-v" in text, workflow
        assert f"startsWith(github.event.release.tag_name, '{short}-v')" in text, workflow
        # It publishes from the extension directory, not the repository root.
        assert text.count("working-directory: ${{ env.DIRECTORY }}") >= 3, workflow
        assert "uv publish --trusted-publishing always" in text, workflow

    assert len(found) >= 3, found
    # Every workflow names a package that is here; a stale one publishes whatever it finds.
    for workflow in workflows.glob("publish-*.yml"):
        short = workflow.stem.removeprefix("publish-")
        assert short in found, f"{workflow.name} publishes an extension that is not here"


@check
def test_a_frame_is_walked_past_its_own_scalars(h):
    """A frame carries a few scalars beside the groups of readings, so anything
    walking one has to step over them.

    `probe` kept a second list, which never gained `slow_rev`, and printed it as a group -
    `_fmt` then iterating an int. app.js keeps a copy too, JavaScript being unable to import
    this one, so both are held to it here.
    """
    from statsbadge import collect

    _status, frame = h.raw("GET", "/api/stats")
    loose = {key for key, value in frame.items() if not isinstance(value, (dict, list))}
    assert loose == set(collect.FRAME_SCALARS), loose

    source = pathlib.Path(install.__file__).parent / "__main__.py"
    assert "collect.FRAME_SCALARS" in source.read_text(encoding="utf-8"), "probe keeps a second list again"

    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    named = re.search(r"const FRAME_SCALARS = \[(.*?)\]", script).group(1)
    assert [word.strip().strip('"') for word in named.split(",")] == list(collect.FRAME_SCALARS)


@check
def test_a_source_that_recovered_stops_being_reported_as_broken(h):
    """An upstream 503 or a subprocess that took too long is a blip on a source that goes on
    working, so the count is kept and the reason is dropped. Left permanently set, a fault
    replaced what the source provides with the name of a Python exception."""
    from statsbadge.sources import base

    source = base.Source({})
    source.note_fault(urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", 503,
                                             "Service Unavailable", {}, None))
    # The message says what happened and where, without repeating the exception's name.
    assert source.last_fault == "HTTP 503 Service Unavailable from api.open-meteo.com", \
        source.last_fault
    source.note_ok()
    assert source.last_fault is None and source.faults == 1, vars(source)

    # The ones sources actually hit, in the words of the thing that failed.
    said = {}
    for exc in (urllib.error.URLError("_ssl.c:1063: The handshake operation timed out"),
                subprocess.TimeoutExpired(["ioreg", "-r", "-c", "IOAccelerator"], 4),
                ValueError("something we did not expect")):
        said[type(exc).__name__] = base.readable(exc)
    assert said["URLError"] == "the connection timed out", said
    assert said["TimeoutExpired"] == "ioreg did not finish inside 4s", said
    # Anything unrecognised keeps its type, which is the clue to what went wrong.
    assert said["ValueError"] == "ValueError: something we did not expect", said

    # The API reports both, so the UI can say "failing" and "recovered".
    _status, caps = h.raw("GET", "/api/capabilities")
    assert caps["sources"], caps
    for entry in caps["sources"]:
        assert set(entry) >= {"name", "provides", "faults", "last_fault"}, entry

    # Every source that expects to fail clears it, or the reason sticks for the session.
    for path in ["src/statsbadge/sources/macos.py", "src/statsbadge/sources/linux.py",
                 "src/statsbadge/sources/windows.py",
                 *sorted(str(p) for p in pathlib.Path("extensions").glob("*/src/*/__init__.py"))]:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        if "note_fault" not in text or "sudoers" in text and "note_ok" not in text:
            continue
        assert "note_ok" in text, f"{path} records faults and never clears one"
    # The UI puts the reason under the name, keeping both.
    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    assert 'source.last_fault ? "faulty" : null' in script, \
        "a recovered source still shows as broken"
    assert 'provides.join(", ")' in script.split("function renderSources")[1][:600]


@check
def test_the_cpu_temperature_linux_reports_is_the_hottest_one(_h):
    """A sensor is ranked by its label, and blank labels all fall back to the chip name.

    Every entry then ranks the same, and taking the first is taking core 0: an idle core
    beside a busy one, reported as the CPU temperature.
    """
    import types

    from statsbadge.sources import linux

    def entry(label, current):
        return types.SimpleNamespace(label=label, current=current, high=None, critical=None)

    source = linux.LinuxHwmon({})
    # psutil only grows sensors_temperatures on Linux, so on any other host it is added.
    missing = object()
    was = getattr(linux.psutil, "sensors_temperatures", missing)
    try:
        linux.psutil.sensors_temperatures = lambda: {
            "coretemp": [entry("", 41.0), entry("", 78.4), entry("", 52.0)]}
        assert source._cpu_temp() == 78.4, "reported an idle core"  # noqa: SLF001

        # A better label still wins, hotter reading or not: k10temp offers Tctl as a
        # control value that runs above the die it sits on.
        linux.psutil.sensors_temperatures = lambda: {
            "k10temp": [entry("Tdie", 60.0), entry("Tctl", 91.0)]}
        assert source._cpu_temp() == 91.0, "Tctl outranks Tdie"  # noqa: SLF001
        linux.psutil.sensors_temperatures = lambda: {
            "coretemp": [entry("Package id 0", 60.0), entry("", 91.0)]}
        assert source._cpu_temp() == 60.0, "an unlabelled core beat the package"  # noqa: SLF001

        # A chip that means nothing about the CPU is no reading, and not the hottest drive.
        linux.psutil.sensors_temperatures = lambda: {"nvme": [entry("Composite", 44.0)]}
        assert source._cpu_temp() is None  # noqa: SLF001
    finally:
        if was is missing:
            del linux.psutil.sensors_temperatures
        else:
            linux.psutil.sensors_temperatures = was


class FakeBoard:
    """The board's half of the raw REPL, enough to answer what repl.py asks of it.

    Stands in for a serial port, so the framing is checked with no badge on the cable.

    The protocol is four control characters and two end markers, and getting one wrong is
    a hang, which an error would at least surface.
    """

    def __init__(self, printed="ok\r\n", failed=""):
        self.printed, self.failed = printed.encode(), failed.encode()
        self.written = b""
        self.scripts = []
        self.out = b""
        self.closed = False
        self.phase = "friendly"
        self._script = b""

    # -- what pyserial offers -----------------------------------------------
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, count=1):
        data, self.out = self.out[:count], self.out[count:]
        return data

    def write(self, data):
        self.written += data
        for byte in data:
            self._take(bytes((byte,)))
        return len(data)

    def close(self):
        self.closed = True

    # -- and what a MicroPython board answers -------------------------------
    def _take(self, byte):
        if self.phase == "raw" and byte != b"\x04":
            self._script += byte
            return
        if byte == b"\x01":
            self.out += b"\r\n" + FakeBoard.PROMPT + b">"
            self.phase = "armed"
        elif byte == b"\x04" and self.phase == "armed":
            self.out += b"\r\n" + b"soft reboot\r\n" + FakeBoard.PROMPT + b">"
            self.phase = "raw"
        elif byte == b"\x04" and self.phase == "raw":
            self.scripts.append(self._script.decode())
            self._script = b""
            self.out += b"OK" + self.printed + b"\x04" + self.failed + b"\x04>"
        elif byte == b"\x02":
            self.phase = "friendly"

    PROMPT = b"raw REPL; CTRL-B to exit\r\n"


@check
def test_the_badge_is_talked_to_over_the_raw_repl_and_nothing_else(_h):
    """Only the raw REPL, with mpremote off the PATH and no interpreter spawned.

    `statsbadge install` is how a badge is set up, and a dependency's console script is
    off PATH when this is installed as a uv tool. So the two things ever asked of a badge,
    run a script and hard reset, are spoken here.

    The board's side is faked, because the failure this guards against is a protocol that
    hangs, as against one that raises."""
    from statsbadge import repl

    board = FakeBoard(printed="2e8a01\r\n")
    fault = type("SerialException", (Exception,), {})
    stub = type(sys)("serial")
    stub.SerialException = fault
    stub.Serial = lambda *_args, **_kwargs: board
    was = sys.modules.get("serial")
    sys.modules["serial"] = stub
    try:
        assert repl.run("/dev/fake", "print(badge.uid)") == "2e8a01\r\n"
        # Interrupt what was running, raw mode, then a soft reset for a clean interpreter:
        # without that the app is still in memory, holding the screen.
        assert board.written.startswith(b"\r\x03\x03\r\x01\x04"), board.written
        assert board.scripts == ["print(badge.uid)"], board.scripts
        # The badge is handed back out of raw mode, which otherwise blanks the screen.
        assert board.written.endswith(b"\r\x02") and board.closed, board.written

        # A script that raised is an exception here, not output the caller has to inspect.
        board = FakeBoard(printed="", failed="Traceback:\r\nImportError: no module named x")
        try:
            repl.run("/dev/fake", "import x")
        except repl.ReplError as exc:
            assert "ImportError" in str(exc), exc
        else:
            raise AssertionError("a traceback came back as ordinary output")

        # A script longer than one chunk still arrives whole.
        board = FakeBoard()
        long_one = "\n".join(f"print({number})" for number in range(200))
        repl.run("/dev/fake", long_one)
        assert board.scripts == [long_one], len(board.scripts)

        # The reset is a hard one, so the badge runs main.py again and does not sit at
        # a prompt. It sleeps first, letting the acknowledgement get out.
        board = FakeBoard()
        repl.reset("/dev/fake")
        assert "machine.reset()" in board.scripts[0], board.scripts
        assert "sleep_ms" in board.scripts[0], board.scripts

        # A board of any other kind is named in the message. Every script here starts by
        # importing badgeware, which on anything else is a traceback naming a module the
        # reader would have to go and look up.
        board = FakeBoard(printed="BOARD Raspberry Pi Pico2 with RP2350\r\n")
        try:
            install.check_board("/dev/fake")
        except install.InstallError as exc:
            assert "Pico2" in str(exc) and install.BOARD in str(exc), exc
        else:
            raise AssertionError("a board with no badgeware on it was accepted")
        board = FakeBoard(printed=f"BOARD Pimoroni {install.BOARD} with RP2350\r\n")
        assert install.BOARD in install.check_board("/dev/fake")

        # Somebody else holding the port is answer enough, the fix being theirs.
        def held(*_args, **_kwargs):
            raise fault("Could not exclusively lock port /dev/fake")

        stub.Serial = held
        try:
            repl.run("/dev/fake", "print(1)")
        except repl.Busy:
            pass
        else:
            raise AssertionError("a held port is not reported as busy")
        # install.py says whose problem that is, in the words of the thing to close.
        try:
            install._exec("/dev/fake", "print(1)")
        except install.PortBusy as exc:
            assert "busy" in str(exc) and "Thonny" in str(exc), exc
        else:
            raise AssertionError("a held port is not reported as busy")

        # A port that failed to open is skipped on the way out. Every command hard resets
        # there, and a reset that cannot happen would spend the enumeration timeout before
        # announcing one.
        started = time.monotonic()
        assert install.hard_reset("/dev/fake") is False
        assert time.monotonic() - started < 2.0, "waited for a reset that never happened"
    finally:
        if was is None:
            del sys.modules["serial"]
        else:
            sys.modules["serial"] = was

    # mpremote has gone from the runtime, and pyserial is a plain dependency.
    assert "mpremote" not in pathlib.Path("src/statsbadge/install.py").read_text(encoding="utf-8")
    with open("pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    assert any(name.startswith("pyserial") for name in project["dependencies"]), project
    assert "install" not in project.get("optional-dependencies", {}), (
        "an extra that no longer adds anything")


@check
def test_a_published_readme_links_to_somewhere_that_exists(_h):
    """A README is the project page on PyPI as well as on GitHub, and PyPI resolves a
    relative link against pypi.org: `shots/cpu.png` is a broken image and `DEVELOPMENT.md`
    a 404.

    So every target is absolute, which makes the repository name part of the text. This
    repository has been renamed once, so the names are checked against the URLs the
    packages declare."""
    for pyproject in [pathlib.Path("pyproject.toml"),
                      *sorted(pathlib.Path("extensions").glob("*/pyproject.toml"))]:
        with open(pyproject, "rb") as handle:
            project = tomllib.load(handle)["project"]
        readme = pyproject.parent / project["readme"]
        text = readme.read_text(encoding="utf-8")
        repository = project["urls"]["Repository"].removesuffix("/")
        slug = repository.removeprefix("https://github.com/")
        raw = f"https://raw.githubusercontent.com/{slug}/main/"

        for label, target in re.findall(r"\[([^\]]*)\]\(([^)\s]+)\)", text):
            where = f"{readme}: [{label}]({target})"
            assert target.startswith(("http", "#")), f"{where} does not resolve on PyPI"
            # Every picture is one of ours, so the repository name is in the URL: renaming the
            # repository and leaving a README behind leaves a broken image.
            if target.startswith("https://raw.githubusercontent.com/"):
                assert target.startswith(raw), where
            # A link naming a path in this tree can be looked at, so it is: a 404 for a reader
            # passes silently otherwise. Anything else - the repository itself, another project -
            # belongs to whoever owns it.
            for prefix in (f"{repository}/blob/main/", f"{repository}/tree/main/", raw):
                if target.startswith(prefix):
                    assert pathlib.Path(target.removeprefix(prefix)).exists(), where


@check
def test_the_list_is_what_every_build_is_made_from(_h):
    """`extensions.txt` is the record, and one extension can be named three ways.

    A list holding two spellings of one plugin asks the installer for it twice, so
    everything is compared by the short name `ext add` would have been given.
    """
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-list-")
    try:
        # A short name becomes the package; anything already specific is left alone.
        assert tooling.as_requirement("clock") == "statsbadge-clock"
        for given in ("statsbadge-clock", "./extensions/statsbadge-iss", "statsbadge-iss>=0.2",
                      "git+https://example.invalid/x.git"):
            assert tooling.as_requirement(given) == given, given
        for requirement, short in (("statsbadge-clock", "clock"),
                                   ("/src/sb/extensions/statsbadge-iss", "iss"),
                                   ("statsbadge-quakes>=0.2", "quakes")):
            assert tooling.short_name(requirement) == short, requirement
        assert tooling.names(["/src/sb/extensions/statsbadge-clock"]) == {"clock"}
        assert tooling.short_name("clock") == tooling.short_name("statsbadge-clock")

        # The resolver explains itself at length; the useful part is the name.
        resolver = ("error: Because statsbadge-nope was not found in the package registry and "
                    "you require statsbadge-nope, we can conclude that your requirements are "
                    "unsatisfiable.")
        assert tooling.explain(resolver) == "no such package: statsbadge-nope"
        assert tooling.explain("error: no internet") == "no internet"
        assert tooling.explain("") == "uv did not say why"

        # Which package it was, out of either form. The caller holds the explained line,
        # and a build installs every entry, so the name it trips over need not be the one
        # just asked for. Saying which makes the message actionable.
        assert tooling.blamed(resolver) == "statsbadge-nope"
        assert tooling.blamed(tooling.explain(resolver)) == "statsbadge-nope"
        assert tooling.blamed("no internet") is None

        # An index is only asked about a bare name, a path or a specifier being skipped.
        # An unreachable index is no answer either, so both come back None.
        assert tooling.on_index("./extensions/statsbadge-iss") is None
        assert tooling.on_index("statsbadge-clock>=2") is None
        assert tooling.on_index("git+https://example.invalid/x.git") is None

        tooling.write_wanted(work, ["statsbadge-clock", "statsbadge-iss"])
        assert tooling.read_wanted(work) == ["statsbadge-clock", "statsbadge-iss"]
        tooling.forget_wanted(work)
        assert tooling.read_wanted(work) == []
        tooling.write_wanted(work, ["statsbadge-clock", "statsbadge-iss"])
        # The file explains itself, and the comments stay comments.
        assert pathlib.Path(work, tooling.WANTED).read_text(encoding="utf-8").startswith("#")
        # What is asked for against what is here, which is what `ext sync` repairs.
        assert tooling.adrift(work, ["clock"]) == ["statsbadge-iss"]
        assert tooling.adrift(work, ["clock", "iss"]) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_an_extension_asked_for_but_absent_is_built_back(_h):
    """A Python upgrade leaves the library unread, so the list asks for what is not here.

    Asking for it again builds it back instead of reporting an install nothing can see.
    The whole list is built, not only the name just given.
    """
    from statsbadge import __main__ as cli
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-upgrade-")
    try:
        tooling.write_wanted(work, ["statsbadge-clock", "/src/statsbadge-cloudflare"])

        class Args:
            names = ["cloudflare"]
            config_dir = work
            verbose = False

        built = []
        was = (cli.tooling.library.build, cli.tooling.library.activate,
               cli.tooling.library.holds, cli.extensions.describe)
        try:
            def build(_directory, requirements, **_kwargs):
                built.append(list(requirements))
                return "/lib/gen", None

            cli.tooling.library.build = build
            cli.tooling.library.activate = lambda *_a: None
            cli.tooling.library.holds = lambda *_a: True
            # After an upgrade: clock is back, cloudflare still missing.
            cli.extensions.describe = lambda: [{"name": "clock"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert built == [["statsbadge-clock", "/src/statsbadge-cloudflare"]], built
            assert "not installed" in said.getvalue(), said.getvalue()
            # The list is untouched: it already asked for exactly this.
            assert tooling.read_wanted(work) == ["statsbadge-clock",
                                                 "/src/statsbadge-cloudflare"]

            # With the extension actually there, adding it again builds nothing.
            built.clear()
            cli.extensions.describe = lambda: [{"name": "clock"}, {"name": "cloudflare"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert built == [], built
            assert "already installed" in said.getvalue(), said.getvalue()
        finally:
            (cli.tooling.library.build, cli.tooling.library.activate,
             cli.tooling.library.holds, cli.extensions.describe) = was
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_an_extension_already_in_the_environment_is_recorded_and_reported(_h):
    """A checkout runs from a virtualenv where an extension may be pip installed already.

    It is reported as present and written down, so a later build asks for it too. Taking
    one out of the list cannot take it out of the environment, which the caller is told.
    """
    from statsbadge import __main__ as cli
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-add-")
    try:
        class Args:
            names = ["bluesky"]
            config_dir = work
            verbose = False

        was = (cli.tooling.library.build, cli.tooling.library.activate,
               cli.tooling.library.elsewhere, cli.extensions.describe,
               cli.tooling.on_index)
        try:
            cli.tooling.library.build = lambda *_a, **_k: ("/lib/gen", None)
            cli.tooling.library.activate = lambda *_a: None
            cli.tooling.on_index = lambda *_a, **_k: True
            # Installed into the environment, where a build cannot reach it.
            cli.tooling.library.elsewhere = lambda _dir, short: (
                "/venv/site-packages" if short == "bluesky" else None)

            # Installed, with nothing on the list: it has never been written.
            cli.extensions.describe = lambda: [{"name": "bluesky"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert "already installed" in said.getvalue(), said.getvalue()
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]

            # Asking again is quiet, and does not write it twice.
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]

            # Removing cannot take, so nothing is written and nothing is built. The list
            # goes on asking for it, since it goes on being installed: dropping the line
            # made a second attempt report success over an extension that was still there.
            built = []
            cli.tooling.library.build = lambda _d, r, **_k: (built.append(list(r))
                                                             or "/lib/gen", None)
            said, complained = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(said), contextlib.redirect_stderr(complained):
                assert cli._change_extensions(Args, "remove") == 1  # noqa: SLF001
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]
            assert built == [], "it built for a removal that could not happen"
            spoken = complained.getvalue()
            assert spoken.startswith("Unable to uninstall bluesky."), spoken
            assert "/venv/site-packages" in spoken, spoken
            assert "Removed" not in said.getvalue(), said.getvalue()

            # And again, since the list still asks for it: the same answer, not a success.
            complained = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(complained):
                assert cli._change_extensions(Args, "remove") == 1  # noqa: SLF001
            assert complained.getvalue().startswith("Unable to uninstall bluesky.")
        finally:
            (cli.tooling.library.build, cli.tooling.library.activate,
             cli.tooling.library.elsewhere, cli.extensions.describe,
             cli.tooling.on_index) = was
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_the_mark_is_the_same_one_everywhere(h):
    """The badge draws it from splash.py's numbers, the config UI links a file and the site
    inlines a copy so it needs no request. Three expressions of one mark, so each is checked
    against the geometry every time."""
    # As bytes: the server hands the file over unchanged, and a Windows checkout with CRLF
    # line endings would not match text read back through universal newlines.
    icon = pathlib.Path("src/statsbadge/web/icon.svg").read_bytes().decode("utf-8")
    page = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    site = pathlib.Path("index.html").read_text(encoding="utf-8")

    # The UI asks for the file, and the server hands it over with the right type: a favicon
    # served as octet-stream is a favicon the browser ignores. Safari reads no SVG favicon at
    # all, so a raster of the same mark is offered first and it takes that.
    assert '<link rel="icon" href="/icon.svg"' in page
    assert page.index('href="/icon.png"') < page.index('href="/icon.svg"'), \
        "the fallback is behind the SVG Safari cannot read"
    with urllib.request.urlopen(h.url("/icon.png"), timeout=5) as response:
        assert response.headers.get("content-type") == "image/png", response.headers
        assert response.read()[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    with urllib.request.urlopen(h.url("/icon.svg"), timeout=5) as response:
        assert response.status == 200
        assert response.headers.get("content-type") == "image/svg+xml", response.headers
        assert response.read().decode() == icon

    # The site inlines the same geometry, so a change to one shows up here before the two
    # marks drift apart. Its data URI quotes attributes with apostrophes, so the numbers
    # are compared and not the markup around them.
    assert 'rel="icon"' in site, "the site has no mark to be the same as"
    for outline in re.findall(r'd="([^"]+)"', icon):
        assert outline in site, outline
    for numbers in re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" '
                              r'width="([\d.]+)" height="([\d.]+)"', icon):
        for number in numbers:
            assert f"'{number}'" in site, (numbers, number)

    # The proportions come from splash.py, which the badge draws before it has a
    # font. The bars carry it: three widths, two gaps and the tallest of them.
    splash = (pathlib.Path(install.app_source_dir()) / "splash.py").read_text(encoding="utf-8")
    numbers = {}
    for line in splash.splitlines():
        if line.startswith(("BAR_W", "BAR_GAP", "BAR_HEIGHTS", "OUTER", "INNER")):
            exec(line, numbers)  # noqa: S102  a module in this repo, five constants off the top
    boxes = [(float(w), float(t)) for w, t in
             re.findall(r'<rect x="[\d.]+" y="[\d.]+" width="([\d.]+)" height="([\d.]+)"', icon)]
    assert len(boxes) == len(numbers["BAR_HEIGHTS"]), boxes
    scale = boxes[0][0] / numbers["BAR_W"]
    for (_wide, tall), height in zip(boxes, numbers["BAR_HEIGHTS"], strict=True):
        assert abs(tall / scale - height) < 0.5, (tall, height, scale)
    # The arc's radii are the dial's, at the same scale.
    radii = sorted({float(r) for r in re.findall(r"A([\d.]+) [\d.]+ 0 1 [01]", icon)})
    assert abs(radii[0] / scale - numbers["INNER"]) < 0.5, radii
    assert abs(radii[-1] / scale - numbers["OUTER"]) < 0.5, radii

    # And the tray's, off the same generator. A template is shape in the alpha alone:
    # AppKit paints it to suit the menu bar, and any colour left in it is ignored.
    from PIL import Image
    assets = pathlib.Path("src/statsbadge/tray/assets")
    for name in ("tray", "tray-attention", "tray-template", "tray-template-attention"):
        with Image.open(assets / f"{name}.png") as art:
            assert art.size[0] == art.size[1], (name, art.size)
            if "template" not in name:
                continue
            coloured = [rgba for _count, rgba in art.convert("RGBA").getcolors(1 << 20)
                        if rgba[3] and rgba[:3] != (0, 0, 0)]
            assert not coloured, (name, coloured[:4])
    assert (assets / "statsbadge.ico").is_file(), "the Windows installer wants an .ico"


@check
def test_a_brightness_the_ui_offers_stays_a_fraction(_h):
    """Everything the panel is ever asked for is a 0-1 fraction, and never zero.

    The firmware maps a fraction onto the panel. This side owes it a number in range:
    `display.backlight` casts to a byte, so a value over 1.0 wraps to a dark panel over a
    framebuffer that still dumps perfectly.

    Never zero either, since a dark room should dim the badge and not switch it off.
    `LIGHT_FLOOR` holds that line, and is the setting to move if a dark room reads too dim.
    """
    sys.path.insert(0, install.app_source_dir())
    import look as look_module

    def sent(wanted):
        """What `backlight` passes on, which is the clamp."""
        return max(0.0, min(1.0, wanted))

    # The slider is 5 to 100 in fives; see web/index.html.
    for percent in range(5, 101, 5):
        asked = percent / 100.0
        assert 0.0 < sent(asked) <= 1.0, f"{percent}% leaves the range as {sent(asked)}"

    # Auto-brightness scales the configured level by the room, so the dimmest the badge can
    # ask for is the bottom of the slider in the dark.
    darkest = 0.05 * look_module.LIGHT_FLOOR
    assert 0.0 < sent(darkest) <= 1.0, f"a dark room asks for {sent(darkest)}"
    assert look_module.LIGHT_FLOOR > 0.0, "a dark room would switch the panel off"

    # A fraction that escaped above 1.0 would wrap, so the clamp is what stops it.
    assert sent(2.463) == 1.0, "an out-of-range brightness reaches the panel"


@check
def test_the_badge_can_report_on_itself_with_no_host(_h):
    """The one page kind whose readings do not come from the frame. It needs no field, so
    nothing can be picked for it and nothing can fail to answer: a prune that keeps only pages
    this host can fill would otherwise drop the page that asked for none of them."""
    config = layout.validate({"pages": [{"id": "b1", "kind": "badge", "title": "Badge"},
                                        {"id": "cpu", "kind": "dial", "field": "cpu.pct"}]})
    page = config["pages"][0]
    assert page == {"id": "b1", "kind": "badge", "title": "Badge"}, page
    # A host measuring none of them still keeps it, and drops the dial.
    kept = layout.prune(config["pages"], {"available": {}})
    assert [p["kind"] for p in kept] == ["badge"], kept

    # The kind picker is written out in the page and not built from the API, so it is the
    # one place a new kind can reach the badge and be forgotten in the browser.
    markup = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    app = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    offered = set(re.findall(r'<option value="([a-z]+)">', markup))
    for kind in layout.KINDS:
        assert kind in offered, f"{kind} is not in the kind picker"
        assert f"  {kind}: {{" in app, f"{kind} has no field slots declared in app.js"

    # The badge draws it: the kind is in the app's table, reads no fields, and stays
    # animated, being numbers to read.
    source = (pathlib.Path(install.app_source_dir()) / "pages.py").read_text(encoding="utf-8")
    table = source[source.index("_KINDS = {"):]
    table = table[:table.index("}")]
    assert '"badge": _badge_page' in table, table
    body = source[source.index("def _badge_page("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_frame" in body.split(")")[0], "the badge page takes the frame seriously"
    assert 'ANIMATED.add("badge")' not in source


@check
def test_the_world_map_is_parsed_once_for_every_page_that_wants_it(_h):
    """Two extensions draw on the firmware's coastlines. 215KB of JSON is 1256ms and 184KB on
    the badge, so a copy per page would be both twice, and a page that has not been turned to
    should cost neither."""
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    assert worldmap._shapes is None, "the map is parsed at import, not on first use"
    # First ask arms it and says wait, so the frame paying for the parse comes before the one
    # was meant to draw the notice.
    assert worldmap.ready() is False
    assert worldmap._shapes is None, "the parse happened in the frame that asked"

    # The pens are the part a theme change invalidates, and the expensive half of a
    # second page: 288 ramp lookups and 288 composites.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("def pens("):]
    body = body[:body.index("\ndef ", 1)]
    # By `theme.key` and not `theme.name`: a derived theme keeps its name when it is built
    # again from another accent, so a cache under the name outlives the colours in it.
    assert "theme.key" in body and "alpha" in body, "the pens are not keyed by theme"
    assert "_pens.clear()" in body, "the table of pens grows without bound"

    # Every page's band comes out of one View, leaving the projection stated once.
    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        page = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py").read_text(encoding="utf-8")
        assert "worldmap.View(" in page, module
        assert "world.geo.json" not in page, f"{module} reads the map itself"


@check
def test_the_night_side_is_the_one_the_sun_is_not_on(_h):
    """The terminator is a curve and the wash is the polygon closed off at a pole, so the half
    that gets filled depends on which pole is lit. Filling the same side all year - which the
    firmware's iss_tracker does - is right for one solstice and inside out for the other."""
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    # Northern summer: the sun is over the tropic of Cancer, so the north pole is lit all day
    # and the terminator at the sun's longitude is as far south as it goes.
    below = worldmap.terminator_at(23.0, 23.0, 23.0)
    opposite = worldmap.terminator_at(23.0 + 180.0, 23.0, 23.0)
    assert below < 0 and opposite > 0, (below, opposite)
    # Southern summer flips both.
    assert worldmap.terminator_at(0.0, 0.0, -23.0) > 0
    # An equinox has no terminator latitude to give: it saturates at a pole, which is the
    # meridian the curve becomes, and the divisor is held off zero getting there.
    assert abs(worldmap.terminator_at(0.0, 0.0, 0.0)) > 89.0

    # The wash is that curve closed off at a pole, and the pole it closes at is the one in
    # darkness: the other is the one the sun is over. The path is in map degrees, where y is
    # -latitude, so a northern sun closes at y +90.
    assert worldmap.night_path(0.0, 23.0)[0].y == 90.0
    assert worldmap.night_path(0.0, 23.0)[-1].y == 90.0
    assert worldmap.night_path(0.0, -23.0)[0].y == -90.0
    # The curve between them spans the world, so the fill has an edge everywhere.
    path = worldmap.night_path(0.0, 23.0)
    assert path[1].x == -180.0 and path[-2].x == 180.0, (path[1], path[-2])

    # Three copies across, or a view wide enough to see a date line loses the wash at one edge.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("    def night("):]
    body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
    assert "nearest - 360.0, nearest, nearest + 360.0" in body, body[-400:]


@check
def test_a_map_page_stays_inside_its_own_band(_h):
    """A map is 288 polygons placed by a transform, with a track and a terminator drawn in
    degrees beside them.

    None of that stops at the edge of the page's band, so the header, the footer and the
    reading band are one clip away from being drawn over."""
    sys.path.insert(0, install.app_source_dir())
    import look

    pages_and_bands = (
        ("statsbadge-quakes", "quakemap", ("_others", "_reticle")),
        ("statsbadge-iss", "issmap", ("_marker", "_track")),
    )
    for extension, module, drawers in pages_and_bands:
        source = (pathlib.Path("extensions") / extension / "src"
                  / extension.replace("-", "_") / "badge" / f"{module}.py").read_text(encoding="utf-8")
        scope = {"look": look}
        for line in source.splitlines():
            if line.startswith(("BAND_H", "MAP_TOP", "MAP_H", "BAND_TOP")):
                exec(line, scope)  # noqa: S102  a repo module, four constants off the top
        # The band the map draws in plus the band that names it come to the page's band
        # exactly.
        assert scope["MAP_H"] + scope["BAND_H"] == look.BODY_H, (module, scope)
        assert scope["MAP_TOP"] == look.BODY_TOP, module
        assert scope["BAND_TOP"] == look.BODY_TOP + scope["MAP_H"], module
        # Everything the page draws on the map clips to it and puts back what it found, or
        # the next page would inherit the clip.
        for name in drawers:
            body = source[source.index(f"def {name}("):]
            body = body[:body.index("\ndef ", 1)]
            assert "screen.clip = view.box" in body, (module, name)
            assert body.index("was = screen.clip") < body.index("screen.clip = view.box"), name
            assert "screen.clip = was" in body, (module, name)

    # The map itself and the night wash are the app's, and clip themselves for the same reason.
    shared = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    for name in ("    def land(", "    def night("):
        body = shared[shared.index(name):]
        body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
        assert "screen.clip = self.box" in body, name
        assert "screen.clip = was" in body, name


@check
def test_the_station_rides_the_track_it_is_already_drawing(_h):
    """The position feed was asked for every five seconds, at 720 requests an hour.

    The station covers 0.065 degrees a second, which is a pixel of a whole-world map every
    seventeen seconds, so four in five of those replies moved the marker nowhere. The run of
    predictions already carries the position, and `flown` says where now sits in it.
    """
    import ast
    import math

    ext = pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss")
    source = (ext / "__init__.py").read_text(encoding="utf-8")
    assert "POSITION_EVERY = 300.0" in source, "the feed is still asked for every few seconds"

    # The two that place the marker, run without the firmware behind them.
    page = (ext / "badge" / "issmap.py").read_text(encoding="utf-8")
    tree = ast.parse(page)
    wanted = {"eased", "flown_at"}
    picked = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in wanted]
    consts = [node for node in tree.body
              if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in
              ("CATCH_UP", "CATCH_UP_MAX", "TRACK_STEPS")]
    assert len(picked) == len(wanted), [node.name for node in picked]

    class Wrapping:
        shortest = staticmethod(lambda d: d - 360.0 * math.floor(d / 360.0 + 0.5))

    env = {"worldmap": Wrapping}
    exec(compile(ast.Module(body=consts + picked, type_ignores=[]),  # noqa: S102
                 "issmap", "exec"), env)
    steps = env["TRACK_STEPS"]

    # Halfway between two dense points is halfway along the line between them.
    dense = [(10.0, 40.0, 1), (20.0, 44.0, 1), (30.0, 46.0, 0)]
    lon, lat, lit = env["flown_at"](dense, 0.5 / steps)
    assert abs(lon - 15.0) < 1e-6 and abs(lat - 42.0) < 1e-6, (lon, lat)
    assert lit == 1
    # An unwrapped longitude comes back in range, the spline having been given turns.
    assert -180.0 <= env["flown_at"]([(190.0, 0.0, 1)], 0.0)[0] <= 180.0
    assert env["flown_at"]([], 0.0) is None

    # A new prediction a little off the last is eased across, not hopped.
    held = (0.0, 40.0, 1)
    moved = env["eased"](held, (2.0, 40.0, 1))
    assert 0.0 < moved[0] < 2.0, moved
    # Past the ceiling it is a different place, so it goes straight there.
    far = env["eased"](held, (40.0, 40.0, 1))
    assert far == (40.0, 40.0, 1), far

    # The marker takes the eased position and the band prints it, so neither shows a
    # position the other does not.
    assert "_marker(theme, view, at[0], at[1], at[2])" in page
    assert '_band(theme, where, iss.get("aboard"), at)' in page


@check
def test_the_iss_page_agrees_with_its_source(_h):
    """The station is host side and the drawing is badge side. The terminator is the one that
    would fail quietly: the sub-solar point arrives with the position, and a page reading a
    name that moved would draw a map with no night on it and say nothing."""
    source = (pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss/badge")
              / "issmap.py").read_text(encoding="utf-8")
    try:
        from statsbadge_iss import ISS
    except ImportError:
        return              # the extension is not pip installed in this environment

    kind = ISS.badge_page["kind"]
    assert f'pages.EXTRA["{kind}"] = render' in source, kind
    # Held unanimated, unlike the quake map: with the world in view a frame
    # is 78ms, and the station covers 0.06 pixels of it a second. It holds still between
    # readings, so asking for frames it has no use for is 30% of the CPU for a pulse.
    assert f'pages.ANIMATED.add("{kind}")' not in source, kind
    assert "jump_to" in source, "the camera eases on a page that is only drawn once a reading"
    for setting in ISS.page_settings:
        assert f'get("{setting["key"]}")' in source, setting["key"]
    # Every option the UI offers for the camera is one the page tests for.
    for option in next(s for s in ISS.page_settings if s["key"] == "follow")["options"]:
        assert option in source or option == "whole world", option
    # The keys a position carries, all of which the page reads.
    for field in ("lat", "lon", "altitude", "speed", "sunlit", "solar_lat", "solar_lon"):
        assert f'"{field}"' in source, field
    assert '"flown"' in source or 'get("flown")' in source


@check
def test_a_quake_marker_is_a_marker_and_not_a_footprint(_h):
    """The reticle reached twenty degrees, which is a 2200km radius: twice Australia.

    It did that for a magnitude 3 as readily as a 7, and being in degrees it covered the same
    ground however far in the camera was. No ring here can be a footprint - a magnitude 5 is
    strongly felt for about 60km, half a pixel at this scale - so it is a marker, sized in
    pixels and ordered by magnitude.
    """
    import ast

    page = pathlib.Path("extensions/statsbadge-quakes/src/statsbadge_quakes/badge"
                        "/quakemap.py").read_text(encoding="utf-8")
    assert "RING_SPAN" not in page, "the reticle is still measured in degrees"

    tree = ast.parse(page)
    names = ("MAG_LOW", "MAG_HIGH", "RING_PX_LOW", "RING_PX_HIGH",
             "DOT_PX_LOW", "DOT_PX_HIGH", "RING_MIN_PX")
    consts = [node for node in tree.body if isinstance(node, ast.Assign)
              and getattr(node.targets[0], "id", "") in names]
    fns = [node for node in tree.body if isinstance(node, ast.FunctionDef)
           and node.name in {"_dot_px", "_mag_fraction"}]
    env = {}
    exec(compile(ast.Module(body=consts + fns, type_ignores=[]),  # noqa: S102
                 "quakemap", "exec"), env)
    assert len(consts) == len(names), [node.targets[0].id for node in consts]

    # Small: a ring of eight pixels at the top of the scale, against twenty-one before.
    assert env["RING_PX_HIGH"] <= 10.0, env["RING_PX_HIGH"]
    assert env["DOT_PX_HIGH"] <= 3.0, env["DOT_PX_HIGH"]
    # Ordered as well, so two events can be ranked by eye.
    assert env["RING_PX_LOW"] < env["RING_PX_HIGH"]
    low, high = env["_dot_px"](env["MAG_LOW"]), env["_dot_px"](env["MAG_HIGH"])
    assert low < high, (low, high)
    # A ring has to clear the dot it sits around, or it is a disc.
    assert env["RING_PX_LOW"] > high, (env["RING_PX_LOW"], high)
    # The smallest ring drawn still reads as one.
    assert env["RING_MIN_PX"] < env["RING_PX_LOW"], env["RING_MIN_PX"]

    # One size for an epicentre, so the selected event and the rest of the feed agree.
    assert page.count("_dot_px(") >= 3, "the active marker is sized on its own again"


@check
def test_the_quake_page_agrees_with_its_source(_h):
    """The events are host side and the drawing is badge side, so a name that moved on one
    is a page that draws blank and leaves the reader guessing."""
    source = (pathlib.Path("extensions/statsbadge-quakes/src/statsbadge_quakes/badge")
              / "quakemap.py").read_text(encoding="utf-8")
    try:
        from statsbadge_quakes import Quakes, _event
    except ImportError:
        return              # the extension is not pip installed in this environment

    kind = Quakes.badge_page["kind"]
    assert f'pages.EXTRA["{kind}"] = render' in source, kind
    # The camera travels and the rings grow between readings, so the page has to ask for
    # frames it has not been polled for.
    assert f'pages.ANIMATED.add("{kind}")' in source, kind

    # Every per-page setting the UI offers is one the renderer reads.
    for setting in Quakes.page_settings:
        assert f'get("{setting["key"]}")' in source, setting["key"]

    # Every key an event carries is one of them, both ways round: the group name, the
    # list inside it, and the fields of an event.
    event = _event({"properties": {"mag": 4.5, "place": "somewhere", "time": 1700000000000},
                    "geometry": {"coordinates": [1.0, 2.0, 10.0]}})
    assert 'frame.get("quakes") or {}).get("events")' in source
    for field in event:
        if field == "at":
            continue        # worked out into age_s before it is sent
        assert f'"{field}"' in source, field


@check
def test_the_app_keeps_what_extensions_reach_into_it_for(_h):
    """A badge module reaches into draw, look, worldmap and pages by attribute.

    Those resolve on the badge alone, so a helper whose callers are all extensions reads
    as unused here, and taking it out is a crash dialog after launch. `draw.readable` is
    one such helper.
    """
    import ast

    app_dir = pathlib.Path(install.app_source_dir())
    defined = {}
    for module in ("draw", "look", "worldmap", "pages"):
        tree = ast.parse((app_dir / f"{module}.py").read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        defined[module] = names

    reached = set()
    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        path = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in defined):
                reached.add((node.value.id, node.attr))
                assert node.attr in defined[node.value.id], (
                    f"{path}: {node.value.id}.{node.attr} is not in the app")
    assert ("draw", "readable") in reached, "the case this check was written for"


@check
def test_a_map_page_only_uses_names_the_badge_has(_h):
    """An extension's badge module is compiled on the badge at launch and cannot be imported
    here, so a name that is neither defined, imported nor a badge builtin is a crash dialog
    after the app has started. Same check the app's modules get."""
    import ast

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import check_app

    injected = check_app.badge_globals()
    assert not isinstance(injected, str), injected

    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        path = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fault = check_app.check_names(path, tree, injected)
        assert fault is None, fault


@check
def test_a_generation_is_asked_about_by_name_and_not_by_prefix(_h):
    """A dist-info separates name from version with the same hyphen a name spells as an
    underscore, so normalising the whole stem matched nothing.

    Everything asking what a generation holds went through this: the library reported
    nothing as its own, so a Remove button became Disable, and the check refusing an
    extension that wants a newer statsbadge never fired.
    """
    work = tempfile.mkdtemp(prefix="statsbadge-named-")
    try:
        for stem in ("statsbadge_clock-1.2.0", "statsbadge-1.3.3", "statsbadge_iss-1.0.3"):
            os.makedirs(os.path.join(work, f"{stem}.dist-info"))
        assert library.resolved(work, "statsbadge-clock") == "1.2.0"
        assert library.resolved(work, "statsbadge_clock") == "1.2.0"
        # The host itself, which is what the version check reads.
        assert library.resolved(work, "statsbadge") == "1.3.3"
        assert library.resolved(work, "statsbadge-quakes") is None
        assert library.holds(work, "clock") and library.holds(work, "iss")
        assert not library.holds(work, "quakes")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_upgrading_one_extension_leaves_the_others_where_they_are(_h):
    """A build resolves every unpinned name to its latest, so naming one to upgrade would
    carry the whole list up with it. The rest are pinned to what the library holds.

    Naming one is asking for it to move, so a version it was pinned to stops being the
    answer, and the list records that. A bare upgrade leaves every pin alone.
    """
    from statsbadge import tooling

    assert tooling.without_pin("statsbadge-clock==1.1.0") == "statsbadge-clock"
    assert tooling.without_pin("statsbadge-iss>=2") == "statsbadge-iss"
    assert tooling.without_pin("statsbadge-clock") == "statsbadge-clock"
    # A path resolves to whatever is in it, so there is no pin to take off.
    assert tooling.without_pin("/src/statsbadge-clock") == "/src/statsbadge-clock"
    assert tooling.pinned(["statsbadge-clock==1.1.0", "statsbadge-iss"]) == {"clock"}

    work = tempfile.mkdtemp(prefix="statsbadge-upgrade-")
    try:
        # Everything but the one named is held at the version the library carries.
        where = os.path.join(work, "lib", f"{library.tag()}-0001")
        os.makedirs(os.path.join(where, "statsbadge_iss-1.0.3.dist-info"))
        os.makedirs(os.path.join(where, "statsbadge_clock-1.1.0.dist-info"))
        building = tooling.holding(work, ["statsbadge-clock", "statsbadge-iss"], {"clock"})
        assert building == ["statsbadge-clock", "statsbadge-iss==1.0.3"], building

        # Naming a pinned one takes the pin off, and says so.
        done = tooling.plan("upgrade", ["clock"], ["statsbadge-clock==1.1.0"], set())
        assert done["wanted"] == ["statsbadge-clock"], done["wanted"]
        assert done["changed"] == ["statsbadge-clock"], done["changed"]
        assert done["unpinned"] and "1.1.0" in done["unpinned"][0], done["unpinned"]

        # Naming nothing means all of them, and touches no pin.
        done = tooling.plan("upgrade", [], ["statsbadge-clock==1.1.0"], set())
        assert done["changed"] == ["statsbadge-clock==1.1.0"], done["changed"]
        assert done["unpinned"] == [], done["unpinned"]
        # One that was never asked for cannot be upgraded.
        assert tooling.plan("upgrade", ["nope"], [], set())["absent"] == ["nope"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_the_generation_a_build_replaced_comes_off_the_path(_h):
    """Appending the new one is not enough: the old one is earlier in sys.path and would
    go on answering the import."""
    work = tempfile.mkdtemp(prefix="statsbadge-path-")
    try:
        first = os.path.join(work, "lib", f"{library.tag()}-0001")
        second = os.path.join(work, "lib", f"{library.tag()}-0002")
        os.makedirs(first)
        assert library.activate(work) == first
        assert first in sys.path

        os.makedirs(second)
        assert library.activate(work) == second
        assert first not in sys.path, "the replaced generation stayed on the path"
        assert second in sys.path
    finally:
        for entry in (first, second):
            if entry in sys.path:
                sys.path.remove(entry)
        shutil.rmtree(work, ignore_errors=True)


@check
def test_a_rebuild_does_not_prune_away_what_it_is_installing(_h):
    """The generation being replaced is on sys.path, and every build resolves against an
    empty directory and prunes what this environment already has.

    Counting the live generation as "already here" pruned each extension straight back
    out of the generation installing it, so a rebuild emptied the library.
    """
    work = tempfile.mkdtemp(prefix="statsbadge-prune-")
    try:
        live = os.path.join(work, "lib", "gen-0001")
        target = os.path.join(work, "lib", "gen-0002")
        for where in (live, target):
            info = os.path.join(where, "madeup_ext-1.0.dist-info")
            os.makedirs(os.path.join(where, "madeup_ext"))
            os.makedirs(info)
            pathlib.Path(info, "METADATA").write_text(
                "Metadata-Version: 2.1\nName: madeup-ext\nVersion: 1.0\n", encoding="utf-8")
            pathlib.Path(info, "RECORD").write_text(
                "madeup_ext/__init__.py,,\nmadeup_ext-1.0.dist-info/METADATA,,\n",
                encoding="utf-8")
            pathlib.Path(where, "madeup_ext", "__init__.py").write_text("", encoding="utf-8")

        sys.path.append(live)
        try:
            importlib.invalidate_caches()
            assert library.prune(target, ignore=os.path.join(work, "lib")) == []
            assert os.path.isdir(os.path.join(target, "madeup_ext")), "pruned its own build"
            # Without the library ignored, the live generation counts and it goes.
            assert library.prune(target) == ["madeup_ext"]
            assert not os.path.isdir(os.path.join(target, "madeup_ext"))
        finally:
            sys.path.remove(live)
            importlib.invalidate_caches()
    finally:
        shutil.rmtree(work, ignore_errors=True)


@check
def test_the_catalogue_says_what_each_extension_is_and_what_it_needs(_h):
    """The published list, so the config UI can offer them without asking PyPI.

    A page's badge module travels over USB alone, so the entry saying so is what keeps
    someone from installing one and waiting for a page that cannot arrive.
    """
    listed = extensions.catalogue()
    named = {entry["name"] for entry in listed}
    assert {"clock", "iss", "quakes"} <= named, named
    for entry in listed:
        assert entry["summary"], entry
    # The three in this repo ship badge modules, and the entry has to say so.
    ships = {entry["name"] for entry in listed if entry["page"]}
    assert ships == {"clock", "iss", "quakes"}, ships
    # And what has to be typed in before it reports anything.
    assert next(e for e in listed if e["name"] == "cloudflare")["needs"]


@check
def test_an_extension_asked_for_but_absent_is_offered_as_such(_h):
    """`uv tool install` replaces the environment whole and leaves `extensions.txt` alone,
    so the two part company without either being edited. The UI has to show that."""
    offered = {entry["name"]: entry for entry in
               extensions.offered(installed=[], wanted=["statsbadge-quakes"])}
    assert offered["quakes"]["asked"] and not offered["quakes"]["installed"]
    assert not offered["clock"]["asked"]

    # Anything installed that the catalogue does not name is still listed, or a
    # third-party one would be invisible and unremovable.
    offered = {entry["name"]: entry for entry in extensions.offered(
        installed=[{"name": "weather", "version": "2.0", "badge_module": "w.py"}],
        wanted=[])}
    assert offered["weather"]["installed"] and offered["weather"]["page"]


@check
def test_the_config_api_offers_the_catalogue_and_guards_what_it_installs(h):
    """Loopback only, since this installs and then runs arbitrary packages."""
    status, body = h.raw("GET", "/api/extensions")
    assert status == 200, (status, body)
    assert body["offered"] and "manageable" in body, body

    # Naming nothing is a bad request and not an empty rebuild.
    status, body = h.raw("POST", "/api/extensions", json.dumps({"add": []}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 400, (status, body)

    # A name absent from every index is refused before anything is written.
    status, body = h.raw("POST", "/api/extensions",
                         json.dumps({"add": ["statsbadge-not-a-real-one-xyz"]}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200 and body["ok"] is False, (status, body)
    assert body["unknown"] and body["why"], body


@check
def test_an_extension_installed_since_start_is_taken_up_without_a_restart(_h):
    """entry_points() walks sys.path on every call, so a reload picks one up in place.

    One already running is kept as it stands: building it again would throw away what it
    has fetched and start its clock over. One that has gone is stopped.
    """
    from statsbadge import extensions as ext
    from statsbadge.collect import Collector

    class Fake:
        provides = ("fake",)

        def __init__(self, _config):
            self.started = self.stopped = 0

        @classmethod
        def available(cls):
            return True

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

    class Entry:
        name = "fake"

        def load(self):
            return Fake

    offered = []
    was = ext._entries
    ext._entries = lambda: list(offered)
    try:
        collector = Collector(interval=1.0)      # not started: no sampling thread wanted
        assert collector.extensions == []

        offered.append(Entry())
        assert collector.reload_extensions() == ["fake"]
        arrived = collector.extensions[0]
        assert arrived.started == 1, "a new extension was not started"

        assert collector.reload_extensions() == ["fake"]
        assert collector.extensions[0] is arrived, "a reload rebuilt one already running"
        assert arrived.started == 1, "a reload started one that was already going"

        offered.clear()
        assert collector.reload_extensions() == []
        assert arrived.stopped == 1, "one that has gone was left running"
    finally:
        ext._entries = was


@check
def test_a_badge_is_called_behind_from_what_it_was_last_seen_holding(_h):
    """The comparison is local, so it can be made with no badge connected."""
    from statsbadge import pushed

    desired = install.desired_hashes()
    missing = sorted(desired)[0]
    with tempfile.TemporaryDirectory() as directory:
        # Nothing recorded is not the same as up to date, and says so.
        assert pushed.behind(directory, "badge1") is None

        held = {name: digest for name, digest in desired.items() if name != missing}
        pushed.record(directory, "badge1", held)
        state = pushed.behind(directory, "badge1")
        assert state["behind"] and state["added"] == [missing], state

        pushed.record(directory, "badge1", desired)
        assert pushed.behind(directory, "badge1")["behind"] is False

        # Bytecode and sources hash differently, so a build this package cannot find
        # again is left uncompared instead of read as a badge full of changes.
        pushed.record(directory, "badge2", desired, source="/nowhere/mpy")
        assert pushed.behind(directory, "badge2") is None

        assert pushed.forget(directory, "badge1")
        assert pushed.behind(directory, "badge1") is None


@check
def test_wifi_details_are_kept_unless_replacing_them_was_asked_for(_h):
    """An update must not be a way to lose the network the badge is already on."""
    from statsbadge import push

    said = []
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        with open(os.path.join(volume, "system", "secrets.py"), "w", encoding="utf-8") as handle:
            handle.write('WIFI_SSID = "Already Here"\nWIFI_PASSWORD = "old"\n')

        kept = push._set_wifi({"password": "new"}, volume, "Other", said.append)
        assert kept == "kept", kept
        assert install.wifi_network_on(volume) == "Already Here"

        set_it = push._set_wifi({"password": "new", "force_secrets": True}, volume,
                                "Other", said.append)
        assert set_it == "set", set_it
        assert install.wifi_network_on(volume) == "Other"


@check
def test_a_region_the_firmware_does_not_know_is_refused(_h):
    """It sets the radio's country. An unknown one cannot associate, and all the badge can
    say about that is that it cannot reach the host."""
    template = ('WIFI_SSID = ""\nWIFI_PASSWORD = ""\n'
                'REGION = "eu"  # Options are us, cuba, eu, moldova, nz\n')
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        with open(os.path.join(volume, "system", "secrets.py"), "w", encoding="utf-8") as handle:
            handle.write(template)

        # The file is the authority, and it lists what it takes beside the setting.
        assert install.regions_on(volume) == ("us", "cuba", "eu", "moldova", "nz")

        try:
            install.write_secrets(volume, "Some Network", "pw", region="gb")
        except install.InstallError as exc:
            assert "gb" in str(exc) and "moldova" in str(exc), exc
        else:
            raise AssertionError("an unknown region was written to the badge")

        # Nothing was written, region or otherwise.
        assert install.wifi_network_on(volume) is None

        install.write_secrets(volume, "Some Network", "pw", region="EU")
        with open(os.path.join(volume, "system", "secrets.py"), encoding="utf-8") as handle:
            values = {}
            exec(compile(handle.read(), "secrets.py", "exec"), values)
        assert values["REGION"] == "eu", "a region has to reach the badge as it writes it"

    # A volume with no list falls back to what this package knows.
    with tempfile.TemporaryDirectory() as bare:
        assert install.regions_on(bare) == install.REGIONS


@check
def test_a_bundle_with_no_trust_store_is_given_one(_h):
    """Inside a packaged app there are no roots at all, and every HTTPS request an
    extension makes cannot find an issuer."""
    import ssl

    from statsbadge import __main__ as cli

    class Store:
        """A context with as many roots as it was told, since the runner's own count is
        not the thing under test: uv's Python on Linux loads none."""

        def __init__(self, roots):
            self.roots = roots

        def cert_store_stats(self):
            return {"x509": self.roots, "crl": 0, "x509_ca": self.roots}

    fake = type(sys)("certifi")
    fake.where = lambda: os.path.join("nowhere", "cacert.pem")

    was_context, was_certifi = ssl.create_default_context, sys.modules.get("certifi")
    was_file, was_dir = os.environ.pop("SSL_CERT_FILE", None), os.environ.pop("SSL_CERT_DIR", None)
    try:
        ssl.create_default_context = lambda *_args, **_kwargs: Store(0)
        sys.modules["certifi"] = fake
        assert cli.trust_store() == fake.where()
        assert os.environ["SSL_CERT_FILE"] == fake.where()

        # Asked again with one already set, it leaves it alone.
        os.environ["SSL_CERT_FILE"] = "somewhere/else.pem"
        assert cli.trust_store() is None

        # A machine with roots of its own is not touched, wherever it keeps them:
        # Windows names no file at all and loads them from the system store.
        del os.environ["SSL_CERT_FILE"]
        ssl.create_default_context = lambda *_args, **_kwargs: Store(128)
        assert cli.trust_store() is None
        assert "SSL_CERT_FILE" not in os.environ
    finally:
        ssl.create_default_context = was_context
        if was_certifi is None:
            sys.modules.pop("certifi", None)
        else:
            sys.modules["certifi"] = was_certifi
        for key, value in (("SSL_CERT_FILE", was_file), ("SSL_CERT_DIR", was_dir)):
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


@check
def test_uv_is_found_where_it_lives_and_not_only_on_the_path(_h):
    """A tray started at login carries the PATH it was given then, and a uv tool
    environment has no pip behind it: the Extensions tab offered nothing at all on a
    machine that plainly had uv."""
    was_which, was_home = shutil.which, os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as home:
        beside = os.path.join(home, ".local", "bin")
        os.makedirs(beside)
        uv = os.path.join(beside, "uv.exe" if os.name == "nt" else "uv")
        with open(uv, "w", encoding="utf-8") as handle:
            handle.write("")
        try:
            shutil.which = lambda _name: None
            os.environ["HOME"] = home
            # USERPROFILE is what expanduser reads on Windows.
            was_profile = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = home
            assert library._uv() == uv, library._uv()
            assert library.tool()[0] == "uv"
        finally:
            shutil.which = was_which
            for key, value in (("HOME", was_home), ("USERPROFILE", was_profile)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@check
def test_a_packaged_app_is_never_asked_to_stand_in_for_pip(_h):
    """Its sys.executable is the app. `-m pip` there starts a second copy of the app,
    icon and all, and answers nothing."""
    was_executable, was_uv = sys.executable, library._uv
    try:
        sys.executable = os.path.join(os.sep, "Applications", "statsbadge.app",
                                      "Contents", "MacOS", "statsbadge")
        library._uv = lambda: None                 # no uv anywhere either
        assert library.tool() is None
        assert library.installer() is None
    finally:
        sys.executable, library._uv = was_executable, was_uv


@check
def test_a_port_that_will_not_open_is_not_called_a_reset(_h):
    """Every command hands the badge back with a reset. One it never reached has none."""
    assert install.hard_reset("/dev/statsbadge-not-a-port", settle=False) is False


@check
def test_the_install_endpoint_runs_one_and_reports_what_it_did(h):
    """Driven with a port that is not there: a test must never touch a real badge."""
    status, body = h.raw("GET", "/api/install")
    assert status == 200 and body["running"] is False, (status, body)
    assert body["result"] is None and body["log"] == [], body

    status, body = h.raw("POST", "/api/install",
                         json.dumps({"port_dev": "/dev/statsbadge-not-a-port"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200, (status, body)
    for _ in range(100):
        status, body = h.raw("GET", "/api/install")
        if not body["running"]:
            break
        time.sleep(0.1)
    assert body["result"]["ok"] is False, body
    assert body["result"]["error"], "a failed install said nothing about why"


def _source_of(fn):
    import inspect
    return inspect.getsource(fn)


def main():
    harness = Harness()
    failures = []
    try:
        for fn in CHECKS:
            try:
                fn(harness)
                print(f"ok   {fn.__name__}")
            except AssertionError as exc:
                failures.append((fn.__name__, exc))
                print(f"FAIL {fn.__name__}: {exc}")
            except Exception as exc:
                failures.append((fn.__name__, exc))
                print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    finally:
        harness.stop()
    print()
    print(f"{len(CHECKS)} checks, {len(failures)} failed")
    return 1 if failures else 0


# pytest discovers the test_* functions above; give them the fixture it needs.
def pytest_generate_tests(metafunc):  # pragma: no cover
    if "h" in metafunc.fixturenames:
        metafunc.parametrize("h", [_shared()], scope="session")


_HARNESS = None


def _shared():  # pragma: no cover
    global _HARNESS
    if _HARNESS is None:
        _HARNESS = Harness()
    return _HARNESS


if __name__ == "__main__":
    sys.exit(main())
