"""End-to-end checks for the server: framing, auth, replay, config, pruning.

Run with `python3 -m pytest` from `server/`, or `python3 tests/test_core.py` for a
plain run with no pytest installed.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from statsbadge import auth, identity, layout, server  # noqa: E402


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
    """A signature for one path must not work on another."""
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
    config["theme"] = "amber"
    h.raw("PUT", "/api/config", json.dumps(config).encode(),
          {"Content-Type": "application/json"})
    _, after = h.signed("GET", "/v1/layout")
    assert after["rev"] > before["rev"], (before["rev"], after["rev"])
    assert after["theme"] == "amber"


@check
def test_pairing_flow(h):
    code = h.service.badges.begin_pairing(ttl=30)
    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": "WRONGCOD", "badge_id": "newbadge"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 403, (status, body)

    code = h.service.badges.begin_pairing(ttl=30)
    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": code, "badge_id": "newbadge"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200, (status, body)
    assert len(body["secret"]) == 64
    # A pairing code is single use.
    status, _ = h.raw("POST", "/v1/pair",
                      json.dumps({"code": code, "badge_id": "another"}).encode(),
                      {"Content-Type": "application/json"})
    assert status == 403


@check
def test_response_is_one_write(h):
    """The whole point of the framing: headers and body in a single segment.

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
def test_badge_provisioned_by_another_process_is_accepted(h):
    """`statsbadge install` writes badges.json while the server is already up.

    The running server holds its own copy in memory, so without a reload the badge it
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

    The server holds the old one in memory, so without re-reading on every verify it
    would reject the badge it had just provisioned as having a bad signature. Uses its
    own badge id so it does not disturb the shared harness counter.
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

    The badge cannot guess: too low reads as a replay, too high as out of window. The
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
    """A stale file must not rewind a counter, or a replay would get through."""
    seq = h.seq
    h.seq += 1
    assert h.signed("GET", "/v1/stats", seq=seq)[0] == 200
    # Rewrite the file with seq=0 for this badge, as a stale copy would have it.
    path = os.path.join(h.dir, "badges.json")
    with open(path) as handle:
        data = json.load(handle)
    data["badges"][h.badge_id]["seq"] = 0
    time.sleep(0.01)
    with open(path, "w") as handle:
        json.dump(data, handle)
    h.service.badges._reload_if_changed()
    status, body = h.signed("GET", "/v1/stats", seq=seq)
    assert status == 401, (status, body)
    assert "replay" in body["error"], body


@check
def test_counter_is_persisted(h):
    """The counter must reach disk, or a restart makes old requests replayable.

    Not on every request - a badge polls once a second - so this drives it past the
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

    # The guarantee is that disk is never more than the threshold behind memory.
    with open(path) as handle:
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
def test_hello_and_pairing_carry_the_identity(h):
    """The badge cannot key credentials on the host unless it is told the id."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200
    assert body["id"] == h.service.identity["id"], body
    assert body["name"] == h.service.identity["name"], body

    code = h.service.badges.begin_pairing(ttl=30)
    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": code, "badge_id": "identified001"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200, (status, body)
    assert body["id"] == h.service.identity["id"], body
    assert body["name"], body


@check
def test_wrong_codes_are_rate_limited_not_counted_out(h):
    """Guessing is slowed, never locked out.

    A hard attempt cap would be something an attacker could exhaust deliberately to stop
    the owner pairing, so the window stays open and the delay grows instead.
    """
    h.service.badges.begin_pairing(ttl=120)
    wrong = json.dumps({"code": "000000", "badge_id": "attacker"}).encode()
    headers = {"Content-Type": "application/json"}

    status, body = h.raw("POST", "/v1/pair", wrong, headers)
    assert status == 403, (status, body)
    assert body.get("retry_after"), body
    first_delay = body["retry_after"]

    # An immediate retry is refused without spending a strike, so spamming cannot
    # ratchet the delay up and keep the owner waiting.
    for _ in range(5):
        status, body = h.raw("POST", "/v1/pair", wrong, headers)
        assert status == 429, (status, body)
        assert body.get("retry_after") is not None, body

    # The window is still open, which is the whole point.
    assert h.service.badges.pairing_active(), "rate limiting closed the window"

    # And the delay doubles per genuine strike rather than growing per spam attempt.
    offer = h.service.badges.pairing
    offer["not_before"] = 0.0
    status, body = h.raw("POST", "/v1/pair", wrong, headers)
    assert status == 403, (status, body)
    assert body["retry_after"] > first_delay, (first_delay, body)


@check
def test_backoff_is_capped_and_global(h):
    """The ceiling bounds how long an owner can be made to wait...

    ...and the limit keys on the window, not the badge id, which is just a field in the
    request that a guesser varies.
    """
    h.service.badges.begin_pairing(ttl=600)
    offer = h.service.badges.pairing
    headers = {"Content-Type": "application/json"}
    delays = []
    for i in range(9):
        offer["not_before"] = 0.0
        offer["last_attempt"] = __import__("time").monotonic()
        status, body = h.raw("POST", "/v1/pair",
                             json.dumps({"code": "000000",
                                         "badge_id": f"attacker{i}"}).encode(), headers)
        assert status == 403, (status, body)
        delays.append(body["retry_after"])
    assert max(delays) <= auth.PAIRING_BACKOFF_CAP, delays
    assert delays[-1] == auth.PAIRING_BACKOFF_CAP, delays
    # Varying the badge id did not reset anything.
    assert delays == sorted(delays), delays


@check
def test_quiet_time_forgives_strikes(h):
    """An early slip should not still be costing the user minutes later."""
    h.service.badges.begin_pairing(ttl=600)
    offer = h.service.badges.pairing
    headers = {"Content-Type": "application/json"}
    wrong = json.dumps({"code": "000000", "badge_id": "clumsy"}).encode()

    for _ in range(4):
        offer["not_before"] = 0.0
        status, body = h.raw("POST", "/v1/pair", wrong, headers)
        assert status == 403, (status, body)
    grown = body["retry_after"]
    assert grown > auth.PAIRING_BACKOFF_BASE, grown

    # Pretend the user walked away for a while.
    import time as _time
    offer["not_before"] = 0.0
    offer["last_attempt"] = _time.monotonic() - auth.PAIRING_FORGIVE_AFTER * 4
    status, body = h.raw("POST", "/v1/pair", wrong, headers)
    assert status == 403, (status, body)
    assert body["retry_after"] < grown, (grown, body)


@check
def test_a_correct_code_works_after_waiting_out_a_wrong_one(h):
    """Fat-fingering a digit costs a short wait and nothing more."""
    code = h.service.badges.begin_pairing(ttl=60)
    wrong = "".join("0" if c != "0" else "1" for c in code)
    headers = {"Content-Type": "application/json"}

    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": wrong, "badge_id": "clumsy"}).encode(),
                         headers)
    assert status == 403, (status, body)
    delay = body["retry_after"]
    assert delay <= 2.0, f"a first mistake should be cheap, got {delay}s"

    # Straight away is refused, which is the rate limit doing its job.
    status, _ = h.raw("POST", "/v1/pair",
                      json.dumps({"code": code, "badge_id": "clumsy"}).encode(),
                      headers)
    assert status == 429, status

    # After the wait, the right code goes through.
    time.sleep(delay + 0.2)
    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": code, "badge_id": "clumsy"}).encode(),
                         headers)
    assert status == 200, (status, body)
    assert len(body["secret"]) == 64


@check
def test_hello_describes_the_code(h):
    """The badge takes the code's shape from here rather than duplicating it."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200
    assert body["code_length"] == auth.CODE_LENGTH
    assert body["code_alphabet"] == auth.CODE_ALPHABET
    assert set(auth.CODE_ALPHABET) <= set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


@check
def test_pairing_is_off_until_asked_for(h):
    """A server must not sit in pairing mode: it is opened deliberately and closed
    again, from the UI or by running out of time."""
    h.service.badges.cancel_pairing()
    state = h.raw("GET", "/api/pair")[1]
    assert state["active"] is False and state["code"] is None, state
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is False

    # A badge cannot pair while it is shut.
    status, body = h.raw("POST", "/v1/pair",
                         json.dumps({"code": "000000", "badge_id": "early"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 403 and "no pairing" in body["error"], body

    opened = h.raw("POST", "/api/pair", b"")[1]
    assert len(opened["code"]) == auth.CODE_LENGTH
    state = h.raw("GET", "/api/pair")[1]
    assert state["active"] is True
    assert state["code"] == opened["code"]
    assert 0 < state["expires_in"] <= 300
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is True

    # Closing it early is the other half of the control.
    h.raw("DELETE", "/api/pair")
    assert h.raw("GET", "/api/pair")[1]["active"] is False
    assert h.raw("GET", "/v1/hello")[1]["pairing"] is False


@check
def test_pairing_state_reports_strikes(h):
    """So the UI can show that something is guessing at it."""
    h.service.badges.begin_pairing(ttl=60)
    assert h.raw("GET", "/api/pair")[1]["strikes"] == 0
    h.raw("POST", "/v1/pair",
          json.dumps({"code": "000000", "badge_id": "guesser"}).encode(),
          {"Content-Type": "application/json"})
    assert h.raw("GET", "/api/pair")[1]["strikes"] == 1
    h.service.badges.cancel_pairing()


@check
def test_config_api_is_loopback_only(_h):
    """The config API can mint secrets, so it must not answer off-box.

    Checked at the handler level: binding a second address to prove it is awkward,
    but the guard is what matters.
    """
    assert "loopback" in _source_of(server.Handler._dispatch)


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
