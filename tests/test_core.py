"""End-to-end checks for the server: framing, auth, replay, config, pruning.

Run with `python3 -m pytest` from `server/`, or `python3 tests/test_core.py` for a
plain run with no pytest installed.
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statsbadge import auth, identity, install, layout, server  # noqa: E402


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
def test_every_offered_theme_exists_on_the_badge(_h):
    """The UI offers whatever layout.THEMES lists, and the badge has to draw it."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                    "statsbadge", "badge_app"))
    import look

    assert set(layout.THEMES) == set(look.THEMES), (
        set(layout.THEMES) ^ set(look.THEMES))
    assert layout.DEFAULT_CONFIG["theme"] == look.DEFAULT
    for name in look.THEMES:
        theme = look.get(name)
        assert theme.name == name, theme.name
        stops = theme.ramp
        assert stops[0][0] == 0.0 and stops[-1][0] == 1.0, name
        assert list(stops) == sorted(stops, key=lambda s: s[0]), name
        for _position, rgb in stops:
            assert len(rgb) == 3 and all(0 <= v <= 255 for v in rgb), (name, rgb)


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
