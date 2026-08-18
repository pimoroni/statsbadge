"""Framing, dispatch and the config API: what the server answers."""

import io
import json
import struct
import socket
import sys
import time

from statsbadge import auth, collect, identity, layout, server


def test_hello_is_open(h):
    """A badge must be able to find the host before it has a secret."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200, status
    assert body["server"] == "statsbadge"
    assert "layout_rev" in body


def test_stats_needs_a_signature(h):
    status, body = h.raw("GET", "/v1/stats")
    assert status == 401, (status, body)
    assert "unsigned" in body["error"]


def test_signed_stats(h):
    status, body = h.signed("GET", "/v1/stats")
    assert status == 200, (status, body)
    assert body["cpu"]["pct"] is not None
    assert body["sys"]["host"]
    assert "layout_rev" in body


def test_bad_signature_is_refused(h):
    bogus = "00" * 32
    status, body = h.signed("GET", "/v1/stats", secret=bogus)
    assert status == 401, (status, body)
    assert "signature" in body["error"]


def test_replay_is_refused(h):
    seq = h.seq
    h.seq += 1
    status, _ = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 200, status
    status, body = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 401, (status, body)
    assert "replay" in body["error"]


def test_sequence_cannot_run_away(h):
    status, body = h.signed("GET", "/v1/stats", seq=h.seq + auth.SEQ_WINDOW + 10)
    assert status == 401, (status, body)
    assert "ahead" in body["error"]


def test_unknown_badge_is_refused(h):
    saved = h.badge_id
    h.badge_id = "nosuchbadge"
    try:
        status, body = h.signed("GET", "/v1/stats")
        assert status == 403, (status, body)
    finally:
        h.badge_id = saved


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


def test_layout_and_history(h):
    status, body = h.signed("GET", "/v1/layout")
    assert status == 200, status
    assert body["pages"], "layout should have pages"
    assert body["theme"] in layout.THEMES
    time.sleep(0.5)
    status, body = h.signed("GET", "/v1/history?keys=cpu.pct&points=8")
    assert status == 200, status
    assert "cpu.pct" in body, body

    # v=2 places the points in time: their spacing, and how old the newest is. Without
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
    #
    # Measured on the rings still filling. A ring at its cap gains a point and drops one, so
    # its length stops moving, and the two caps differ: a series ring holds SERIES_LEN
    # against history_len for a scalar. Comparing lengths across the two once either has
    # filled compares a ring that is still growing with one that cannot.
    rings = h.service.collector.history(None, 160)
    caps = {key: (collect.SERIES_LEN if ring and isinstance(ring[0], list)
                  else h.service.collector.history_len)
            for key, ring in rings.items()}
    before = {key: len(ring) for key, ring in rings.items()}
    time.sleep(0.5)
    after = {key: len(ring) for key, ring in h.service.collector.history(None, 160).items()}

    filling = {after[key] - length for key, length in before.items() if after[key] < caps[key]}
    assert len(filling) <= 1, (before, after, caps)
    for key, length in before.items():
        if length >= caps[key]:
            assert after[key] == caps[key], (key, length, after[key], caps[key])


def test_unbound_command_is_refused(h):
    status, body = h.signed("POST", "/v1/command", {"cmd": "lock"})
    assert status == 403, (status, body)
    assert "not bound" in body["error"]


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


def test_bad_config_is_rejected(h):
    status, body = h.raw("PUT", "/api/config",
                         json.dumps({"pages": [{"kind": "nonsense"}]}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 400, (status, body)
    assert "kind" in body["error"]


def test_layout_rev_moves_on_change(h):
    _, before = h.signed("GET", "/v1/layout")
    status, config = h.raw("GET", "/api/config")
    config["theme"] = "vapor"
    h.raw("PUT", "/api/config", json.dumps(config).encode(),
          {"Content-Type": "application/json"})
    _, after = h.signed("GET", "/v1/layout")
    assert after["rev"] > before["rev"], (before["rev"], after["rev"])
    assert after["theme"] == "vapor"


def test_response_is_one_write(h):
    """Headers and body leave in a single segment, which the framing is for."""
    # A short timeout after the first recv, so a body in a later segment reads short.
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


def test_a_dropped_connection_is_not_reported(h):
    """A connection reset between requests leaves stderr quiet, and a real fault does
    not."""
    # SO_LINGER at 0 resets rather than closing, as the badge does. The handler thread is
    # parked in readline, so the reset surfaces there with nothing in flight.
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


def test_nodelay_is_set():
    assert server.Handler.disable_nagle_algorithm is True


def caller(h, address, path):
    """A handler far enough along to be dispatched to, without a socket behind it.

    Built off the running server's handler class, so it carries the service `make_server`
    bound to it, and records what it answers instead of writing it.
    """
    class Caller(h.httpd.RequestHandlerClass):
        def __init__(self):
            self.client_address = (address, 51234)
            self.path = path
            self.headers = {}
            self.server = h.httpd
            self.answered = None

        def _send(self, status, body, _kind, _extra=None):
            self.answered = (status, json.loads(body))

    return Caller()


def test_config_api_is_loopback_only(h):
    """The config API can mint secrets, so it answers on loopback alone."""
    for address in ("127.0.0.1", "::1"):
        assert caller(h, address, "/api/capabilities")._is_local(), address
    for address in ("10.0.0.5", "192.168.1.20", "8.8.8.8", "not-an-address"):
        assert not caller(h, address, "/api/capabilities")._is_local(), address

    # That guard is the one dispatch keeps: a config path from off the machine is
    # refused before it reaches the API.
    off_box = caller(h, "10.0.0.5", "/api/capabilities")
    off_box._dispatch("GET")
    assert off_box.answered[0] == 403, off_box.answered
    assert "loopback" in off_box.answered[1]["error"], off_box.answered

    # The same path from loopback gets through to a real answer.
    local = caller(h, "127.0.0.1", "/api/capabilities")
    local._dispatch("GET")
    assert local.answered[0] == 200, local.answered
    assert "kinds" in local.answered[1], local.answered

    # A badge path is not behind the guard: badges are on the network by definition.
    badge = caller(h, "10.0.0.5", "/v1/hello")
    badge._dispatch("GET")
    assert badge.answered[0] == 200, badge.answered


def test_server_identity_is_stable(h):
    """A badge keys credentials on this, so it must survive a restart."""
    first = identity.load(h.dir)
    again = identity.load(h.dir)
    assert first["id"] == again["id"], "the id changed between loads"
    assert len(first["id"]) >= 16
    assert first["name"]


def test_the_general_settings_are_read_and_written_over_one_route(h):
    """The Settings tab reads and saves through `/api/settings`, which reaches the sources.

    The route the browser really calls, since the control that calls it is built at runtime
    and nothing here can press it.
    """
    was = h.raw("GET", "/api/settings")[1]
    try:
        status, block = h.raw("GET", "/api/settings")
        assert status == 200, (status, block)
        assert set(block) == set(server.HOST_KEYS), block

        status, saved = h.raw("POST", "/api/settings",
                              json.dumps({"place": " Sheffield, GB ", "latitude": "",
                                          "longitude": ""}).encode(),
                              {"Content-Type": "application/json"})
        assert status == 200, (status, saved)
        assert saved["place"] == "Sheffield, GB", saved
        assert h.raw("GET", "/api/settings")[1]["place"] == "Sheffield, GB"
        for source in h.service.collector.extensions:
            assert source.home == {"place": "Sheffield, GB"}, source.name

        # A browser does not enforce min and max on a typed value, so the host clamps.
        _status, clamped = h.raw("POST", "/api/settings",
                                 json.dumps({"latitude": 120}).encode(),
                                 {"Content-Type": "application/json"})
        assert clamped["latitude"] == 90.0, clamped

        # The help tab reports what the platform needs and takes nothing. 404, the router
        # answering an unmatched method the same way it answers an unmatched path.
        status, _body = h.raw("POST", "/api/help", b"{}",
                              {"Content-Type": "application/json"})
        assert status == 404, status
    finally:
        h.raw("POST", "/api/settings", json.dumps(was).encode(),
              {"Content-Type": "application/json"})
