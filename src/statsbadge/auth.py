"""Pairing and request signing.

Plain HTTP on a LAN, so the transport authenticates nothing; DEVELOPMENT.md has why it is
not HTTPS. Every request carries an HMAC-SHA256 over the method, path, a counter and the
body, keyed on the secret pairing shared. Reads are signed too, at one HMAC a second, so
an unpaired device on the network learns nothing.

Pairing is rate limited and not counted out: a hard cap is something an attacker can
exhaust to stop the owner pairing at all. The limit is global to the window and not per
badge, the badge id being a field a guesser picks fresh each time.

Doubling from 1s to a 30s ceiling fits ~13 guesses into a 300s window, against 5 for a
hard cap of five. That is the same order of safety, 1 in 77,000 for a six-digit code,
and a mistyped code costs a second.

The enrol code is never derived from badge.uid, which travels as X-Badge-Id in clear.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# A badge asks to be let in and shows a short code; a human approves it at the host.
#
# Minted per request, returned for the badge to display, and compared and not entered.
ENROL_CODE_HEX = 6
# Waiting requests allowed at once, so a flood cannot bury the real one.
MAX_PENDING = 6
ENROL_TTL = 180.0

PAIRING_BACKOFF_BASE = 1.0
PAIRING_BACKOFF_CAP = 30.0
# A strike is forgiven per this many seconds of quiet.
PAIRING_FORGIVE_AFTER = 30.0

SIGNED_HEADER_ID = "x-badge-id"
SIGNED_HEADER_SEQ = "x-badge-seq"
SIGNED_HEADER_SIG = "x-badge-sig"

# How far a counter may jump in one request. Generous, since a badge that reboots rounds
# its counter up; small enough that a captured request cannot be replayed.
SEQ_WINDOW = 4096

# Written back once the counter has advanced this far, not every request: a badge polls
# once a second all day, and a restart otherwise rewinds it.
SEQ_PERSIST_EVERY = 32


class AuthError(Exception):
    def __init__(self, reason, status=401, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        # Extra fields for the error body. Only ever set once the signature has been
        # verified, so nothing here is told to an unauthenticated caller.
        self.detail = detail or {}


class Store:
    """Paired badges, persisted next to the config.

    A record is {badge_id: {"secret": hex, "name": str, "seq": int, "paired_at": ts}}.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.badges = {}
        self.pairing = None       # a live pairing window, or None
        self.enrolments = {}      # request id -> a badge waiting to be approved
        self._mtime = None
        self._persisted = {}      # badge_id -> counter last written to disk
        self.load()

    def load(self):
        # An unreadable store is remembered and not treated as empty, or saving over it takes
        # the real pairings with it. Happens after the server has been run with sudo.
        self.unreadable = None
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            self._mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            self.unreadable = str(exc)
            data = {}
        self.badges = data.get("badges", {})
        self._persisted = {
            bid: record.get("seq", 0) for bid, record in self.badges.items()
        }

    def _reload_if_changed(self):
        """Pick up a badge paired by another process.

        `statsbadge install` mints a secret from a separate Store while the server is
        already running, so without this the server never hears about the badge it
        just provisioned. One stat() per request is cheap; re-reading is not, so it
        only happens when the file has actually moved.
        """
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        self._mtime = mtime
        incoming = data.get("badges", {})
        # The higher counter wins: ours may have advanced past the file, and going backwards
        # lets a replay through.
        for badge_id, record in incoming.items():
            existing = self.badges.get(badge_id)
            if existing and existing.get("secret") == record.get("secret"):
                record["seq"] = max(record.get("seq", 0), existing.get("seq", 0))
            self.badges[badge_id] = record

    def save(self):
        if self.unreadable:
            raise PermissionError(
                f"{self.path} cannot be read ({self.unreadable}), so it will not be "
                "written over. Fix its ownership, or pass --config-dir.")
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"badges": self.badges}, handle, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._persisted = {
            bid: record.get("seq", 0) for bid, record in self.badges.items()
        }
        # Remember the write here, so _reload_if_changed does not re-read it.
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    def begin_pairing(self, ttl=300):
        """Open a window during which badges may ask to be let in."""
        with self._lock:
            self.pairing = {"expires": time.monotonic() + ttl,
                            "strikes": 0, "not_before": 0.0,
                            "last_attempt": time.monotonic()}
        return True

    def cancel_pairing(self):
        with self._lock:
            self.pairing = None

    def pairing_active(self):
        with self._lock:
            offer = self.pairing
            if offer and offer["expires"] < time.monotonic():
                self.pairing = None
                return False
            return offer is not None

    def pairing_state(self):
        """The open window, for the config UI."""
        with self._lock:
            offer = self._live_window()
            if offer is None:
                return {"active": False, "expires_in": 0}
            return {
                "active": True,
                "expires_in": max(0, int(offer["expires"] - time.monotonic())),
                "asked": offer.get("strikes", 0),
            }

    def request_enrolment(self, badge_id, name=None):
        """A badge asking to be let in. Returns the request, or raises AuthError.

        Rate limited on the same backoff as the rest of pairing: reachable by anyone on
        the network.
        """
        with self._lock:
            offer = self._live_window()
            if offer is None:
                raise AuthError("pairing is not open on this host", 403)

            now = time.monotonic()
            self._expire_enrolments(now)

            # Before the rate limit: a badge retrying after a dropped reply is not a new
            # attempt.
            for request_id, entry in self.enrolments.items():
                if entry["badge_id"] == badge_id and entry["status"] == "pending":
                    return {"request_id": request_id, "code": entry["code"]}

            wait = offer.get("not_before", 0.0) - now
            if wait > 0:
                raise AuthError(f"too many attempts, try again in {wait:.0f}s", 429,
                                detail={"retry_after": round(wait, 1)})

            if len(self.enrolments) >= MAX_PENDING:
                raise AuthError("too many badges waiting; approve or deny one first", 429)

            # Each request extends the delay, so a flood slows itself down.
            offer["strikes"] = offer.get("strikes", 0) + 1
            offer["not_before"] = now + min(
                PAIRING_BACKOFF_BASE * (2 ** (offer["strikes"] - 1)),
                PAIRING_BACKOFF_CAP)
            offer["last_attempt"] = now

            request_id = secrets.token_hex(16)
            self.enrolments[request_id] = {
                "badge_id": badge_id,
                "name": name or badge_id,
                "code": secrets.token_hex(ENROL_CODE_HEX // 2).upper(),
                "status": "pending",
                "asked_at": now,
                "secret": None,
            }
            return {"request_id": request_id, "code": self.enrolments[request_id]["code"]}

    def enrolment(self, request_id):
        """What became of a request. The secret is handed over once."""
        with self._lock:
            self._expire_enrolments(time.monotonic())
            entry = self.enrolments.get(request_id)
            if entry is None:
                return {"status": "gone"}
            if entry["status"] != "approved":
                return {"status": entry["status"]}
            secret = entry.pop("secret", None)
            if secret is None:
                return {"status": "gone"}      # already collected
            del self.enrolments[request_id]
            return {"status": "approved", "secret": secret}

    def pending_enrolments(self):
        """Requests waiting on a human."""
        with self._lock:
            now = time.monotonic()
            self._expire_enrolments(now)
            return [
                {
                    "request_id": request_id,
                    "badge_id": entry["badge_id"],
                    "name": entry["name"],
                    "code": entry["code"],
                    "waiting_s": int(now - entry["asked_at"]),
                    "expires_in": max(0, int(ENROL_TTL - (now - entry["asked_at"]))),
                }
                for request_id, entry in self.enrolments.items()
                if entry["status"] == "pending"
            ]

    def approve_enrolment(self, request_id, name=None):
        """Let a badge in, minting its secret."""
        with self._lock:
            self._expire_enrolments(time.monotonic())
            entry = self.enrolments.get(request_id)
            if entry is None or entry["status"] != "pending":
                raise AuthError("no such request", 404)
            secret = secrets.token_hex(32)
            self.badges[entry["badge_id"]] = {
                "secret": secret,
                "name": name or entry["name"],
                "seq": 0,
                "paired_at": int(time.time()),
            }
            entry["status"] = "approved"
            entry["secret"] = secret
            self.save()
            return entry["badge_id"]

    def deny_enrolment(self, request_id):
        with self._lock:
            entry = self.enrolments.pop(request_id, None)
            return entry is not None

    def _live_window(self):
        """The open window, or None. Caller holds the lock."""
        offer = self.pairing
        if offer and offer["expires"] < time.monotonic():
            self.pairing = None
            return None
        return offer

    def _expire_enrolments(self, now):
        """Drop unanswered requests. Caller holds the lock."""
        for request_id in [
            rid for rid, entry in self.enrolments.items()
            if entry["status"] == "pending" and now - entry["asked_at"] > ENROL_TTL
        ]:
            del self.enrolments[request_id]

    def provision(self, badge_id, name=None, start_seq=0):
        """Mint a secret directly, for the USB installer where possession of the
        cable is the proof of ownership and there is nobody to read a code.

        `start_seq` must match what the installer writes to the badge, or the badge's
        first request lands outside the replay window.
        """
        with self._lock:
            secret = secrets.token_hex(32)
            self.badges[badge_id] = {
                "secret": secret,
                "name": name or badge_id,
                "seq": start_seq,
                "paired_at": int(time.time()),
            }
            self.save()
            return secret

    def rename(self, badge_id, name):
        """A name the operator chose for a badge, or its id back when they clear it.

        A badge announces itself by whatever its setup screen was told, which is its id
        until somebody names it - and two badges on one host then read the same.
        """
        with self._lock:
            record = self.badges.get(badge_id)
            if record is None:
                return False
            record["name"] = name or badge_id
            self.save()
            return True

    def forget(self, badge_id):
        with self._lock:
            if self.badges.pop(badge_id, None) is None:
                return False
            self.save()
            return True

    def list_badges(self):
        with self._lock:
            return {
                bid: {k: v for k, v in record.items() if k != "secret"}
                for bid, record in self.badges.items()
            }

    def verify(self, method, path, headers, body):
        """Check a signed request. Returns the badge id, or raises AuthError."""
        badge_id = headers.get(SIGNED_HEADER_ID)
        seq_text = headers.get(SIGNED_HEADER_SEQ)
        signature = headers.get(SIGNED_HEADER_SIG)
        if not (badge_id and seq_text and signature):
            raise AuthError("unsigned request")

        with self._lock:
            # Every request, not only for an unknown badge: `install --new-secret` replaces the
            # secret of a known one, and the stale copy would reject it. A stat() unless the file
            # moved.
            self._reload_if_changed()
            record = self.badges.get(badge_id)
            if record is None:
                raise AuthError("unknown badge", 403)
            secret = record["secret"]
            last_seq = record.get("seq", 0)

        try:
            seq = int(seq_text)
        except ValueError:
            raise AuthError("bad sequence") from None

        expected = sign(secret, method, path, seq, body)
        if not hmac.compare_digest(signature.lower(), expected):
            raise AuthError("bad signature")

        # Only a good signature may move the counter, or anyone could push it up and lock the
        # badge out.
        #
        # Both refusals carry the counter to use next. Safe, the signature having proved the
        # caller holds the secret, and it resyncs a rebooted badge in one request.
        if seq <= last_seq:
            raise AuthError("replayed request", detail={"next_seq": last_seq + 1})
        if seq > last_seq + SEQ_WINDOW:
            raise AuthError("sequence too far ahead",
                            detail={"next_seq": last_seq + 1})

        with self._lock:
            self.badges[badge_id]["seq"] = seq
            self.badges[badge_id]["last_seen"] = int(time.time())
            if seq - self._persisted.get(badge_id, 0) >= SEQ_PERSIST_EVERY:
                self.save()
        return badge_id

    def secret_for(self, badge_id):
        with self._lock:
            record = self.badges.get(badge_id)
            return record["secret"] if record else None


def sign(secret, method, path, seq, body):
    """The signature both ends compute.

    Over method, path, counter and a digest of the body, newline separated. The body
    is hashed rather than included so signing a large POST costs the badge one pass.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hashlib.sha256(body or b"").hexdigest()
    message = "\n".join((method.upper(), path, str(seq), digest))
    return hmac.new(bytes.fromhex(secret), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def fingerprint(secret):
    """A short, safe way to show which secret a badge was given, for the UI."""
    return base64.b32encode(
        hashlib.sha256(bytes.fromhex(secret)).digest()[:5]
    ).decode().rstrip("=")


def display_names(paired):
    """Each paired badge as the name it was given, with its id alongside where they differ.

    A badge nobody has renamed is recorded under its own id, so one name is all there is to
    print for it.
    """
    shown = []
    for badge_id, record in (paired or {}).items():
        name = record.get("name") or badge_id
        shown.append(name if name == badge_id else f"{name} ({badge_id})")
    return shown
