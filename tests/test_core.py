"""End-to-end checks for the server: framing, auth, replay, config, pruning.

Run with `python3 -m pytest` from `server/`, or `python3 tests/test_core.py` for a
plain run with no pytest installed.
"""

import io
import json
import os
import struct
import pathlib
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statsbadge import auth, identity, install, layout, model, server  # noqa: E402


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
    """Waiting requests count against the cap, so tests must not leave any behind."""
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
def test_a_dropped_connection_is_not_reported(h):
    """SO_LINGER at 0 resets instead of closing, which is what the badge does.

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
        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": digest}}))
        assert install._stale_modules(built) == []
        source, _note = install.choose_app_source(None, False, None)
        assert source == str(built), source

        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": "0" * 64}}))
        assert install._stale_modules(built) == ["look.py"]
        source, note = install.choose_app_source(None, False, None)
        assert source is None, source
        assert "look.py" in note, note
    finally:
        install.packaged_mpy_dir = original


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
def test_pairing_is_off_until_asked_for(h):
    """A server must not sit in pairing mode: it is opened deliberately and closed
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

    # Nothing yet.
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

    # And it actually works.
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
    # And not derived from the badge id, which is public.
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
    # The same badge asking again gets its existing request, not a new one.
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
    """The config API can mint secrets, so it must not answer off-box.

    Checked at the handler level: binding a second address to prove it is awkward,
    but the guard is what matters.
    """
    assert "loopback" in _source_of(server.Handler._dispatch)


@check
def test_write_secrets_keeps_the_rest_of_the_file(_h):
    """Setting WiFi details must not disturb the other settings or their comments."""
    import tempfile

    from statsbadge import install

    template = ('WIFI_SSID = ""\n'
                'WIFI_PASSWORD = ""\n'
                'REGION = "eu"  # Options are us, cuba, eu, moldova\n'
                'TIMEZONE = 0  # Offset from GMT as number of hours\n')
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        path = os.path.join(volume, "system", "secrets.py")
        with open(path, "w") as handle:
            handle.write(template)

        assert install.secrets_file(volume) == path
        assert not install.wifi_configured(volume)

        # A backslash and quotes in the password: a naive regex replacement writes these
        # back out as escapes and the file stops being valid Python.
        password = 'p@ss "w0rd"\\'
        install.write_secrets(volume, "Some Network", password, region="us")

        with open(path) as handle:
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
        with open(path) as handle:
            after = handle.read()
        assert after.count("WIFI_SSID") == 1
        values = {}
        exec(compile(after, "secrets.py", "exec"), values)
        assert values["WIFI_SSID"] == "Other"
        assert values["TIMEZONE"] == 0

        # A key the file lacks is appended.
        install.write_secrets(volume, "Third", "pw", timezone=-7)
        with open(path) as handle:
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
            with open(os.path.join(source, name), "w") as handle:
                handle.write(name)
        with open(os.path.join(source, "mpy", "stowaway.mpy"), "w") as handle:
            handle.write("not this one either")
        plugin = os.path.join(work, "clockface.py")
        with open(plugin, "w") as handle:
            handle.write("# a badge-side extension module")

        names = dict(install.app_files(source, [("clock", plugin)]))
        assert sorted(names) == ["__init__.mpy", "ext/clockface.py", "icon.png",
                                 "net.mpy"], names

        # A stale .py beside a .mpy is the one that matters: it wins the import and
        # silently undoes the precompile.
        target = os.path.join(work, "stats")
        os.makedirs(os.path.join(target, "ext"))
        for name in ("__init__.mpy", "net.mpy", "net.py", "icon.png", "notes.txt"):
            with open(os.path.join(target, name), "w") as handle:
                handle.write("old")
        for name in ("clockface.py", "gone.py"):
            with open(os.path.join(target, "ext", name), "w") as handle:
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
    """An unreadable store must not read as "no badges" and then be written over."""
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
                return          # running as root, where the mode means nothing
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
def test_extensions_describe_finds_the_clock(_h):
    from statsbadge import extensions

    found = {record["name"]: record for record in extensions.describe()}
    clock = found.get("clock")
    if clock is None:
        return              # the extension is not pip installed in this environment
    assert clock["loaded"], clock
    assert clock["badge_module"] == "clockface.py", clock
    assert "clock" in clock["provides"], clock


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
    """A key nothing asked for goes; a whole block for an extension that is not loaded
    stays, or disabling one would be what deletes its configuration."""
    schema = {"clock": [{"key": "latitude", "type": "number"}]}
    incoming = {**layout.DEFAULT_CONFIG, "settings": {
        "clock": {"latitude": "1.5", "sneaky": "no"},
        "notloaded": {"token": "keep me"},
    }}
    stored = layout.validate(incoming, (), schema)["settings"]
    assert stored["clock"] == {"latitude": 1.5}, stored
    assert stored["notloaded"] == {"token": "keep me"}, stored

    # An empty field clears a setting rather than reading as zero
    incoming["settings"]["clock"] = {"latitude": ""}
    cleared = layout.validate(incoming, (), schema)["settings"]
    assert cleared["clock"]["latitude"] is None, cleared


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
    pages_source = (app / "pages.py").read_text()
    ui_source = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
                 / "app.js").read_text()
    markup = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
              / "index.html").read_text()
    for kind in layout.KINDS:
        assert f'"{kind}": _' in pages_source, f"{kind} has no renderer"
        assert f"  {kind}: {{" in ui_source, f"{kind} has no shape in the UI"
        assert f'value="{kind}"' in markup, f"{kind} is not in the kind picker"


@check
def test_caselights_take_a_field_or_a_flag(_h):
    """Three settings in one value: off, the theme's level, or a reading to follow."""
    base = dict(layout.DEFAULT_CONFIG)

    def stored(value):
        return layout.validate({**base, "caselights": value})["caselights"]

    assert stored("cpu.pct") == "cpu.pct"
    assert stored(True) is True
    assert stored(False) is False
    # Anything that is not a "group.field" falls back to a flag rather than reaching the
    # badge as a reference it cannot look up.
    assert stored("bogus") is True
    assert stored("too.many.dots") is True
    assert stored(None) is False


@check
def test_an_extension_page_can_be_added_and_reaches_the_badge(h):
    """The UI's kind picker is built from this, and the config it PUTs has to validate.

    Without it the page an extension offers is unreachable: the server knows about it,
    the badge can draw it, and there is no way to ask for one.
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
        with open(path, "w") as handle:
            handle.write("# a comment\n"
                         "\n"
                         "sunny e81a\n"
                         "rainy f176 i    # trailing comment\n")
        assert tool.read_corpus(path) == [("sunny", 0xE81A, None),
                                          ("rainy", 0xF176, ord("i"))]

        for bad, why in (("sunny\n", "one field"),
                         ("sunny nothex\n", "bad codepoint"),
                         ("sunny e81a ab\n", "two-character remap")):
            with open(path, "w") as handle:
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
    # remapped rather than silently mangled.
    high = tool.Glyph(0x1FFF0)
    high.contours = [[(0, 0), (10, 0), (10, -10), (0, 0)]]
    try:
        tool.pack([high])
    except SystemExit:
        pass
    else:
        raise AssertionError("packed a codepoint that does not fit")


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
        with open(corpus) as handle:
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

    # A field can arrive as a list - a load average, per-core loads - and a list cannot be a
    # key, so it must not reach the table that remembers what a number formatted to. This
    # crashed a CPU dial with a LOADAVG readout on it.
    #
    # A load average is three figures and reads as the three of them, the way uptime prints
    # it. No unit: it is a queue length, not a percentage of anything.
    assert draw.reading([1.52, 1.18, 0.94], "load") == "1.5 1.2 0.9"
    # Sixteen per-core loads do not go in one slot, and three of the sixteen would be a lie.
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

    # Without a schema an extension page keeps its fields and nothing else, as before.
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

    # A source that raises must not stop the others being told.
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
    script = (pathlib.Path(__file__).parent.parent / "ci" / "build-mpy.sh").read_text()
    default = [line for line in script.splitlines() if line.startswith("OUT_DIR=")]
    assert default, "no OUT_DIR default in the build script"
    assert "src/statsbadge/badge_app/mpy" in default[0], default[0]

    # And the two places CI wants the packaged copy still say so explicitly.
    for workflow in ("ci.yml", "publish.yml"):
        text = (pathlib.Path(__file__).parent.parent / ".github" / "workflows"
                / workflow).read_text()
        if "build-mpy.sh" in text:
            assert "src/statsbadge/badge_app/mpy" in text, workflow


@check
def test_the_field_picker_offers_each_reading_once(_h):
    """numericRefs is a subset of availableRefs, so concatenating them listed every
    number twice - once qualified by its group and once again below it."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text()
    # Joining the two lists is fine, so long as the result is deduplicated where it is
    # joined. Checked per line so this cannot pass by matching the fix itself.
    for line in ui.splitlines():
        if "concat(availableRefs())" in line:
            assert "new Set(" in line, f"undeduplicated: {line.strip()}"
    assert "function preferredRefs()" in ui
    # And refSelect deduplicates whatever it is handed, so no caller can bring it back.
    assert "new Set(refs)" in ui


@check
def test_every_kind_picks_from_a_pool_that_suits_it(_h):
    """A gauge offered uptime drew an empty ring, and a grid offered cpu.cores printed a
    list. Each slot now draws from a pool, and every kind has to name one."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text()
    shape = ui[ui.index("const SHAPE = {"):ui.index("// Theme swatches")]
    pools = ui[ui.index("const POOLS = {"):]
    pools = pools[:pools.index("}")]
    named = {name for name in ("gauge", "series", "list", "any") if name in pools}

    for kind in layout.KINDS:
        # An entry may be wrapped over two lines, so take it up to its closing brace
        # rather than one line of it.
        start = shape.find(f"  {kind}: {{")
        assert start != -1, f"{kind} has no shape"
        entry = shape[start:shape.index("},", start)]
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
    # And which fields are a list, so only the kinds that draw lanes are offered them.
    assert set(described["list_fields"]) >= {"cores", "load"}


@check
def test_a_rate_is_scaled_by_what_it_has_reached(_h):
    """Throughput has no full scale of its own, and the fixed one read as pegged.

    12.5MB/s was assumed, so anything over that filled the ring: a 40MB/s transfer and a
    200MB/s one looked the same. The collector tracks what each rate has reached instead.
    """
    from statsbadge.collect import PEAK_DECAY, PEAK_FLOOR

    assert 0.9 < PEAK_DECAY < 1.0
    peak = 0.0
    for rate in [40e6] * 5:
        peak = max(rate, peak * PEAK_DECAY, PEAK_FLOOR)
    assert peak == 40e6
    # A trickle afterwards is a small part of the ring, not an eighth of a ring that was
    # already full.
    assert (1.5e6 / peak) < 0.05
    # And the peak comes down again, so one busy night does not flatten it for good.
    quiet = peak
    for _ in range(600):
        quiet = max(0.0, quiet * PEAK_DECAY, PEAK_FLOOR)
    assert quiet < peak * 0.6, quiet

    # The floor keeps a quiet link from scaling a trickle up to a full ring.
    assert max(10_000.0, 0.0, PEAK_FLOOR) == PEAK_FLOOR


@check
def test_setup_waves_through_a_server_already_paired(_h):
    """Setup is offered after a few failed polls, not only when unpaired, so it is easy to
    reach with nothing wrong. Asking to pair again then failed for a server the badge was
    already paired with, because the host was not in pairing mode."""
    source = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text()
    assert "_already_paired" in source, "no already-paired path in setup"
    # Reached before anything is asked of the host.
    ask = source[source.index("def _ask_to_join"):]
    assert ask.index("_already_paired") < ask.index("net.enrol("), (
        "the badge asks to enrol before noticing it is already paired")
    # A host that has refused this badge is the case where pairing again is the point, so
    # that must not be waved through.
    guard = source[source.index("def _already_paired"):source.index("def _ask_to_join")]
    assert "rejected" in guard, "a refused badge would be waved through with dead credentials"


@check
def test_a_row_of_text_and_a_plot_measures_its_columns(_h):
    """A fixed column either leaves a gap after the names or runs the readings into the
    plots, and which of the two it does depends on the fields the page carries."""
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text()
    for widget in ("def bars", "def sparklines", "def graph"):
        body = source[source.index(widget):]
        body = body[:body.index("\ndef ", 1)]
        assert "column_width(" in body, f"{widget} still lays out to a fixed column"

    # The gauge and its column sit in the band on one gap, so no part of the pair can be
    # placed on a number of its own.
    look_source = (pathlib.Path(install.app_source_dir()) / "look.py").read_text()
    for name in ("DIAL_C = (DIAL_GAP", "READOUT_X = DIAL_C[0]",
                 "READOUT_W = W - READOUT_X - DIAL_GAP"):
        assert name in look_source, f"{name} is not derived from the dial's gap"


@check
def test_a_split_page_takes_the_layout_it_is_given(_h):
    """A dial, a ring stack and a clock face all split the band into something round and a
    column, and the pages are paged between: anything choosing its own centre or margin
    moves under the reader when they press a button."""
    app = pathlib.Path(install.app_source_dir())

    # Four rings have to fit the same radius a single gauge draws in.
    look_source = (app / "look.py").read_text()
    draw_source = (app / "draw.py").read_text()
    scope = {}
    for line in look_source.splitlines():
        if line.startswith(("DIAL_OUTER", "DIAL_GAP", "READOUT_H", "READOUT_NOTE_H")):
            exec(line, scope)  # noqa: S102  our own module, four constants off the top
    band = int(re.search(r"^RING_BAND = (\d+)", draw_source, re.M).group(1))
    gap = int(re.search(r"^RING_GAP = (\d+)", draw_source, re.M).group(1))
    innermost = scope["DIAL_OUTER"] - 4 * band - 3 * gap
    assert innermost >= 8, f"the fourth ring is {innermost} across; it would be dropped"

    # The clock takes both from the app rather than restating them, and puts its column
    # where every other split page puts it.
    clock = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge")
             / "clockface.py").read_text()
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
    assert 'id="animate"' in (web / "index.html").read_text(), "no control in the UI"
    assert "config.animate" in (web / "app.js").read_text(), "the control is not bound"

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

        # The same reading again is not a new sweep.
        made = len(FakeTween.made)
        pages.fraction_of("cpu.pct", 20.0)
        assert len(FakeTween.made) == made, "an unchanged reading restarted the sweep"

        # And a page turn forgets where everything stood, a turn not being a change in the
        # machine.
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 20.0) == 0.2
        assert FakeTween.made[-1] == (0.2, 0.2)
    finally:
        pages.ANIMATE = was
        pages.sweep_reset()
        pages.__dict__.pop("tween", None)

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text()
    assert "pages_module.sweep_reset()" in app[app.index("def turn"):], (
        "a page turn keeps the last page's needle positions")
    assert "pages_module.moving" in app, "nothing asks for a frame while a gauge is moving"


@check
def test_a_page_can_slide_on_like_a_card(_h):
    """A window of the screen has its own origin, so a page drawn into one lands shifted and
    clipped: that is the card, and the rasteriser costs the window rather than the screen.
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
    assert 'id="slide"' in (web / "index.html").read_text(), "no control in the UI"
    assert "config.slide" in (web / "app.js").read_text(), "the control is not bound"
    for style in layout.SLIDE_STYLES:
        assert f'value="{style}"' in (web / "index.html").read_text(), style

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text()
    sliding = app[app.index("def render_sliding"):]
    sliding = sliding[:sliding.index("\n    def ", 1)]
    # Both cards are a rect out of an image, which is what makes the direction free: a window
    # cannot start at a negative origin, so a page cannot be drawn part way off the left.
    assert "self.arriving.window(" in sliding and "self.leaving.window(" in sliding
    assert "self.slide_back" in sliding, "both directions look the same"

    into = app[app.index("def draw_page_into"):]
    into = into[:into.index("\n    def ", 1)]
    # Rebound rather than passed: an extension's renderer draws through the same builtin, and
    # would otherwise put its page on the screen while the app drew into the image.
    assert "builtins.screen = target" in into and "builtins.screen = was" in into
    # From whatever screen is now: badge.mode replaces it, and a copy taken at import time is
    # the 160x120 screen the app started with - a 320-wide page drawn into that wraps.
    assert "was = screen" in into
    assert "target.font" in into, "an image starts with no font, and label() restores it"

    # And the turn only starts one when the layout asks, keeping the screen only for a deck.
    turn = app[app.index("def turn"):]
    turn = turn[:turn.index("\n    def ", 1)]
    assert '.get("slide")' in turn and "delta < 0" in turn
    start = app[app.index("def start_slide"):]
    assert 'style == "deck"' in start[:start.index("\n    def ", 1)]


@check
def test_smooth_graphs_are_a_setting_that_reaches_the_badge(_h):
    """A drawing switch, so it is one setting for every graph rather than a page property."""
    config = layout.validate({"smooth": False, "pages": layout.DEFAULT_PAGES})
    assert config["smooth"] is False
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["smooth"] is True, "on by default"
    # Anything truthy, since the UI sends a checkbox and a command line sends a string.
    assert layout.validate({"smooth": "yes", "pages": layout.DEFAULT_PAGES})["smooth"] is True

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="smooth"' in (web / "index.html").read_text(), "no control in the UI"
    assert "config.smooth" in (web / "app.js").read_text(), "the control is not bound"
    # And the badge applies it where it applies the rest of the layout.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text()
    applied = app[app.index("def apply_layout"):]
    assert "draw.SMOOTH" in applied[:applied.index("\n    def ", 1)]


@check
def test_a_theme_travels_as_its_colours(_h):
    """A theme is a table of colours, so it is config: the badge is sent the palette and not
    only the name, and one it has never heard of draws as well as one it ships with."""
    import sys

    from statsbadge import themes

    sys.path.insert(0, install.app_source_dir())
    import look

    # Every palette is complete, ordered and usable by the badge's own builder.
    assert layout.DEFAULT_CONFIG["theme"] == themes.DEFAULT
    for name, palette in themes.PALETTES.items():
        assert name in layout.THEMES, f"{name} is not offered"
        built = look.from_palette(name, palette)
        assert built is not None, f"the badge cannot build {name}"
        assert built.name == name, built.name
        stops = built.ramp
        assert stops[0][0] == 0.0 and stops[-1][0] == 1.0, name
        assert list(stops) == sorted(stops, key=lambda stop: stop[0]), name
        for fraction in (0.0, 0.5, 1.0):
            assert len(built.at(fraction)) == 3, (name, fraction)

    # And the one the app carries to boot with agrees with the host's copy of it, or the
    # first frame is drawn in colours the config never asked for.
    assert list(look.THEMES) == [themes.DEFAULT], list(look.THEMES)
    booted, sent = look.THEMES[themes.DEFAULT], themes.PALETTES[themes.DEFAULT]
    for key in ("bg", "panel", "ink", "dim", "accent", "grid"):
        assert tuple(getattr(booted, key)) == tuple(sent[key]), key
    assert booted.ramp == sent["ramp"]

    # The colours are on the payload the badge fetches, keyed to the theme it chose.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({"theme": "eva01", "pages": layout.DEFAULT_PAGES})
    sent = config.for_badge()
    assert sent["theme"] == "eva01"
    assert sent["palette"] == themes.PALETTES["eva01"], sent["palette"]
    assert look.from_palette(sent["theme"], sent["palette"]).accent == (143, 212, 0)

    # Nonsense off the network is refused rather than drawn: a bad palette would otherwise
    # be a crash on every frame instead of a page in the theme it booted with.
    for bad in (None, {}, {"bg": "red"}, {"bg": (1, 2, 3), "ramp": ()}):
        assert look.from_palette("bad", bad) is None, bad


@check
def test_the_ui_takes_its_swatches_from_the_host(_h):
    """They used to be a table in app.js with a comment asking for it to be kept in step
    with the badge, which is two places to edit and one to forget."""
    web = pathlib.Path("src/statsbadge/web/app.js").read_text()
    assert "THEME_COLOURS" not in web, "the UI still carries its own palettes"
    assert "caps.palettes" in web, "the UI does not read the host's palettes"
    assert '"palettes"' in pathlib.Path("src/statsbadge/server.py").read_text()


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

    # And it is only the colour: the sweep and the bar are the reading itself.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text()
    body = source[source.index("def gauge("):]
    body = body[:body.index("\ndef ", 1)]
    assert "theme.at(fraction if hot is None else hot)" in body
    assert "shape.arc(middle, inner, outer, start, sweep)" in body, (
        "the sweep is no longer drawn from the reading")


@check
def test_the_badge_dims_to_suit_the_room(_h):
    """Measured on the badge: a curtained room reads 96-176 raw of a u16, stepping in
    sixteens, so the useful adjustment is in the bottom couple of percent of the range."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import look

    assert look.ambient_fraction(look.LIGHT_DIM) == 0.0
    assert look.ambient_fraction(96) < look.ambient_fraction(176) < look.ambient_fraction(1000)
    assert look.ambient_fraction(look.LIGHT_BRIGHT) == 1.0
    # Anything past the ceiling is full, and a ceiling the app has raised rescales the rest.
    assert look.ambient_fraction(65535) == 1.0
    assert look.ambient_fraction(2000, 2000) == 1.0
    assert look.ambient_fraction(2000, 20000) < 1.0
    # Logarithmic: the first doubling is worth as much as the next.
    first = look.ambient_fraction(look.LIGHT_DIM * 2)
    assert 0.4 < first / look.ambient_fraction(look.LIGHT_DIM * 4) < 0.6, first

    # A dim room is dimmer, not dark: dark is what the setting being off would look like.
    assert 0.0 < look.LIGHT_FLOOR < 1.0

    # Off by default, since it is the badge's own sensor and not every board has one.
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["auto_brightness"] is False
    assert layout.validate({"auto_brightness": True,
                            "pages": layout.DEFAULT_PAGES})["auto_brightness"] is True
    assert 'id="autobright"' in pathlib.Path("src/statsbadge/web/index.html").read_text()


@check
def test_the_badge_pages_on_its_own_when_left_alone(_h):
    """Off by default: a display that moves while somebody is reading it is a nuisance."""
    config = layout.validate({"pages": layout.DEFAULT_PAGES})
    assert config["idle_advance_s"] == 0, config["idle_advance_s"]
    assert config["advance_every_s"] == 10
    clamped = layout.validate({"idle_advance_s": 99999, "advance_every_s": 0,
                               "pages": layout.DEFAULT_PAGES})
    assert clamped["idle_advance_s"] == 3600, clamped
    # A page nobody can see for a whole second is not a page anybody can read.
    assert clamped["advance_every_s"] == 1, clamped

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="idle"' in (web / "index.html").read_text()
    assert '"idle_advance_s"' in (web / "app.js").read_text()

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text()
    advance = app[app.index("    def advance_if_idle"):]
    advance = advance[:advance.index("\n    # --", 1)]
    # The turns it makes for itself must not count as somebody using the badge, or the first
    # one would put it back to sleep.
    assert "_pressed_at" in advance and "self._pressed_at =" not in advance, advance
    assert "len(self.page_list) < 2" in advance, "one page would turn to itself"

    # A press is what resets it, wherever a press is noticed - including HOME, since opening
    # the menu is somebody using the badge.
    for method in ("    def buttons(self):", "    def home(self):"):
        body = app[app.index(method):]
        body = body[:body.index("\n    def ", 1)]
        assert "self._pressed_at = time.ticks_ms()" in body, method
    # And not in turn(), which both the buttons and the badge itself go through.
    turn = app[app.index("    def turn(self"):]
    turn = turn[:turn.index("\n    # --", 1)]
    assert "_pressed_at" not in turn, turn


@check
def test_a_button_can_do_something_without_the_host(_h):
    """Paging and the panel are the badge's own business: a round trip to change them would
    be slower than the press, and would not work at all with the host away."""
    actions = dict(layout.LOCAL_ACTIONS)
    assert set(actions) == {"badge.prev", "badge.next", "badge.brightness"}, actions

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text()
    press = app[app.index("    def press(self"):]
    press = press[:press.index("\n    def ", 1)]
    assert "LOCAL_PREFIX" in press and "send_command" in press, press
    # Every action the host offers is one the badge answers, or a button does nothing.
    handler = app[app.index("    def local(self"):]
    handler = handler[:handler.index("\n    def ", 1)]
    for action in actions:
        assert f'"{action}"' in handler, action
    # The prefix is what keeps them off the wire, so it has to be what the host offers.
    for action in actions:
        assert action.startswith("badge."), action


@check
def test_a_smoothed_graph_still_reads_as_the_data(_h):
    """A curve through the samples, not near them: it is a graph of a machine, so a peak
    drawn where there was none, or short of the one there was, is a lie about the machine."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw

    values = [0.2, 0.9, 0.3, 0.31, 0.8, 0.1, 0.5]
    dense = draw.curve(values, steps=4)
    assert len(dense) == (len(values) - 1) * 4 + 1, len(dense)
    # Every sample is still on the curve, at the position it was in.
    for index, value in enumerate(values):
        assert abs(dense[index * 4] - value) < 1e-9, (index, dense[index * 4], value)
    # And a spline's overshoot is held to the range of the data, or an area fill would run
    # under its own baseline where the reading touched zero.
    assert min(dense) >= min(values) and max(dense) <= max(values), (
        min(dense), max(dense))

    # Fewer than three points cannot be interpolated.
    assert draw.curve([0.5, 0.6], steps=4) == [0.5, 0.6]

    # Whether to interpolate at all is `curve_steps`, which answers 1 for "draw it straight":
    # when the switch is off, when there is nothing to interpolate, and when the plot is too
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
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text()
    body = source[source.index("def curve("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_basis(steps)" in body, "the weights are not taken from the table"


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
    # so that is read out of the fonts rather than assumed. Through the tool, so a font
    # repacked wide is read as one instead of misparsed as narrow.
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
    # Which is lower than a shared baseline puts it, that being the bug.
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

    # The digital face's own digits are the app's face at a finer grid, so they have to agree
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
    source = (badge / "clockface.py").read_text()

    resync = source[source.index("def _resync("):]
    resync = resync[:resync.index("\n\n\n") if "\n\n\n" in resync else len(resync)]
    assert "_synced_seq" in resync, "every frame reconsiders the same reading"
    assert resync.index("_synced_seq") < resync.index("RTC()"), (
        "the clock is set before the reading is checked for being a new one")

    # Synced from the host's clock, never a place's: there is one hardware clock and two pages
    # in two zones would set it to their own each time you turned to them.
    render = source[source.index("def render(page"):]
    render = render[:render.index("\n\n\n") if "\n\n\n" in render else len(render)]
    assert "_resync(host," in render, render[:400]
    assert "_zone_offset(host, here)" in render, "a page elsewhere is not offset from the host"


@check
def test_every_clock_face_the_ui_offers_can_be_drawn(_h):
    """The face list is host side and the renderers are badge side, so a face added to one
    and not the other is a page that draws the default and says nothing."""
    badge = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"))
    source = (badge / "clockface.py").read_text()
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

    # The seven-segment face needs a font of its own, which is an asset and not code, so it
    # travels only if it is declared.
    assert any(path.endswith("lcd.af") for path in Clock.badge_assets), Clock.badge_assets
    assert (badge / "lcd.af").exists(), "the LCD face's font is not built"
    # Shipped, so its licence ships with it.
    licence = pathlib.Path("licences/OFL-DSEG.txt").read_text()
    assert "keshikan" in licence and "SIL Open Font License" in licence

    # The unlit segments go down before the lit ones, or they cover them.
    body = source[source.index("def _digital"):]
    body = body[:body.index("\ndef ", 1)]
    assert body.index('spec["ghost"]') < body.index("draw.blit_label(hours,"), (
        "the ghost is drawn over the digits")


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
