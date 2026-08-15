"""Pairing, secrets, sequence counters and the enrolment window."""

import json
import os
import tempfile
import time

import pytest

from conftest import headers as _headers

from statsbadge import auth, server


def test_badge_provisioned_by_another_process_is_accepted(h):
    """A badge provisioned by the CLI while the server runs is accepted without a
    restart."""
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


def test_rotated_secret_is_picked_up(h):
    """A rotated secret works from the next request, and the old one stops."""
    # A badge id of its own, leaving the shared harness counter alone.
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


def test_counter_refusal_offers_a_resync(h):
    """A counter refused as a replay or as out of window says what to use next."""
    # The signature is verified before this check, so telling the caller is safe.
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


def test_counter_is_persisted(h):
    """The counter on disk stays within SEQ_PERSIST_EVERY of the one in memory."""
    # Writes are batched, a badge polling once a second, so this drives it past the
    # threshold and checks the file caught up.
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

    # A fresh Store, standing in for a restarted server, refuses a used counter.
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


def test_pairing_is_off_until_asked_for(h):
    """Pairing starts closed, opens on request, and closes again from the UI."""
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


def test_hello_carries_the_identity(h):
    """The badge keys its credentials on the id and name /v1/hello carries."""
    status, body = h.raw("GET", "/v1/hello")
    assert status == 200
    assert body["id"] == h.service.identity["id"], body
    assert body["name"] == h.service.identity["name"], body


def test_enrolment_needs_an_open_window(h):
    h.service.badges.cancel_pairing()
    status, body = h.raw("POST", "/v1/enrol",
                         json.dumps({"badge_id": "asker0001"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 403 and "not open" in body["error"], body


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


def test_denied_enrolment_pairs_nothing(h):
    h.service.badges.begin_pairing(ttl=60)
    asked = h.raw("POST", "/v1/enrol",
                  json.dumps({"badge_id": "asker0003"}).encode(),
                  {"Content-Type": "application/json"})[1]
    h.raw("POST", f"/api/enrol/{asked['request_id']}/deny", b"")
    assert h.raw("GET", f"/v1/enrol/{asked['request_id']}")[1]["status"] == "gone"
    assert "asker0003" not in h.service.badges.list_badges()


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
    # Minted per request, since the badge id is public.
    for i, code in enumerate(codes):
        assert f"unique{i}" not in code.lower()


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


def test_pending_requests_are_capped(h):
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


def test_unreadable_badge_store_is_not_treated_as_empty():
    """An unreadable store raises, and is left where it is."""

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
                pytest.skip("root, or Windows, where a mode is only a read-only bit")
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


def test_a_badge_can_be_given_a_name(h):
    """A badge takes the name it is given, and falls back to its id when that is
    cleared."""
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

    # A badge nobody has named is recorded under its id, and that is all there is to print.
    assert auth.display_names({}) == []
    assert auth.display_names({
        "e661badge0000001": {"name": "Desk badge"},
        "e661badge0000002": {"name": "e661badge0000002"},
        "e661badge0000003": {},
    }) == ["Desk badge (e661badge0000001)", "e661badge0000002", "e661badge0000003"]
