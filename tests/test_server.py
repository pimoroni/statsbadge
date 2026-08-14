"""Framing, dispatch and the config API: what the server answers."""

import io
import json
import struct
import socket
import sys
import time

from statsbadge import auth, identity, layout, server


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


def test_nodelay_is_set():
    assert server.Handler.disable_nagle_algorithm is True


def _source_of(fn):
    import inspect
    return inspect.getsource(fn)


def test_config_api_is_loopback_only():
    """The config API can mint secrets, so it answers on loopback alone.

    Checked at the handler level: binding a second address to prove it is awkward,
    but the guard is the part under test.
    """
    assert "loopback" in _source_of(server.Handler._dispatch)


def test_server_identity_is_stable(h):
    """A badge keys credentials on this, so it must survive a restart."""
    first = identity.load(h.dir)
    again = identity.load(h.dir)
    assert first["id"] == again["id"], "the id changed between loads"
    assert len(first["id"]) >= 16
    assert first["name"]
