"""Pairing and request signing.

Plain HTTP on a LAN, so the transport authenticates nothing. Every request carries
an HMAC-SHA256 over the method, path, a counter and the body, keyed on a secret the
badge and host share from pairing. That makes a command unforgeable without the
secret, and the counter stops one being replayed. See NETWORKING.md for why the
transport is not HTTPS.

Reads are signed too. It costs the badge one HMAC per second and means an
unpaired device on the network learns nothing about the machine.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# A pairing code the user can read off a screen and type on a badge. No vowels and
# no 0/O/1/I, so nothing in it can be misread or spell anything.
CODE_ALPHABET = "23456789BCDFGHJKLMNPQRSTVWXYZ"
CODE_LENGTH = 8

SIGNED_HEADER_ID = "x-badge-id"
SIGNED_HEADER_SEQ = "x-badge-seq"
SIGNED_HEADER_SIG = "x-badge-sig"

# How far a counter may jump forward in one request. Generous, because a badge that
# reboots loses count and rounds its counter up; small enough that a captured
# request cannot be replayed later.
SEQ_WINDOW = 4096

# Write the counter back to disk once it has advanced this far since the last write.
# Not every request: a badge polls once a second all day. But it has to be persisted
# at all, or a restart rewinds the counter and every request captured before it
# becomes replayable again.
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
        self.pairing = None       # a live pairing offer, or None
        self._mtime = None
        self._persisted = {}      # badge_id -> counter last written to disk
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self):
        try:
            with open(self.path) as handle:
                data = json.load(handle)
            self._mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            data = {}
        self.badges = data.get("badges", {})
        self._persisted = {
            bid: record.get("seq", 0) for bid, record in self.badges.items()
        }

    def _reload_if_changed(self):
        """Pick up a badge paired by another process.

        `statsbadge install` mints a secret from its own Store while the server is
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
            with open(self.path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        self._mtime = mtime
        incoming = data.get("badges", {})
        # Keep whichever counter is higher: ours may have advanced past the file
        # since it was written, and going backwards would let a replay through.
        for badge_id, record in incoming.items():
            existing = self.badges.get(badge_id)
            if existing and existing.get("secret") == record.get("secret"):
                record["seq"] = max(record.get("seq", 0), existing.get("seq", 0))
            self.badges[badge_id] = record

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump({"badges": self.badges}, handle, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._persisted = {
            bid: record.get("seq", 0) for bid, record in self.badges.items()
        }
        # Remember our own write, so _reload_if_changed does not re-read it.
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    # -- pairing ------------------------------------------------------------

    def begin_pairing(self, ttl=300):
        """Open a pairing window and return the code to show the user."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock:
            self.pairing = {"code": code, "expires": time.monotonic() + ttl}
        return code

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

    def claim(self, code, badge_id, name=None):
        """Trade a correct pairing code for a fresh shared secret."""
        with self._lock:
            offer = self.pairing
            if not offer:
                raise AuthError("no pairing in progress", 403)
            if offer["expires"] < time.monotonic():
                self.pairing = None
                raise AuthError("pairing expired", 403)
            if not hmac.compare_digest(code.strip().upper(), offer["code"]):
                raise AuthError("wrong pairing code", 403)
            secret = secrets.token_hex(32)
            self.badges[badge_id] = {
                "secret": secret,
                "name": name or badge_id,
                "seq": 0,
                "paired_at": int(time.time()),
            }
            self.pairing = None
            self.save()
            return secret

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

    # -- verifying ----------------------------------------------------------

    def verify(self, method, path, headers, body):
        """Check a signed request. Returns the badge id, or raises AuthError."""
        badge_id = headers.get(SIGNED_HEADER_ID)
        seq_text = headers.get(SIGNED_HEADER_SEQ)
        signature = headers.get(SIGNED_HEADER_SIG)
        if not (badge_id and seq_text and signature):
            raise AuthError("unsigned request")

        with self._lock:
            # Every request, not just for an unknown badge: `install --new-secret`
            # replaces the secret of a badge we already know, and keeping the stale
            # one in memory would reject the badge we just provisioned. It is a stat()
            # unless the file actually moved.
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

        # Only now that the signature is good is it safe to let the request move the
        # counter, otherwise anyone could push it up and lock the badge out.
        #
        # Both refusals carry the counter to use next. That is safe because the
        # signature above already proved the caller holds the secret, and it lets a
        # badge that rebooted, or was provisioned against a different starting point,
        # resync in one request instead of guessing.
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
    """A short, safe way to show which secret a badge holds, for the UI."""
    return base64.b32encode(
        hashlib.sha256(bytes.fromhex(secret)).digest()[:5]
    ).decode().rstrip("=")
