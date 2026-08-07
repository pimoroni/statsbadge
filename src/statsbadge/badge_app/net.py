"""Talking to the host: a step-per-frame HTTP client, and request signing.

The client is a generator advanced a slice at a time from the draw loop, so a poll
never blocks a frame. Measured against a server that writes each response in one go:
9ms a request warm, and the worst single step is 0.6ms. See DEVELOPMENT.md - the
firmware's own `fetch.py` is not used because it is broken on this build and wedges
permanently on the first socket error.

Signing is HMAC-SHA256 over method, path, a counter and a digest of the body. The
counter only ever goes up, and the host refuses anything it has already seen, so a
captured command cannot be replayed.
"""

import binascii
import hashlib
import json
import socket
import time

STATE_FILE = "/state/stats.json"

# How long to wait on a whole request before giving up and dropping the socket.
REQUEST_TIMEOUT_MS = 6000
# Time budget per step. Several short reads per frame beats one per frame without
# risking the frame, and a frame at 90Hz is 11ms.
STEP_BUDGET_US = 2500

IDLE, BUSY, DONE, FAILED = 0, 1, 2, 3

# errno as this firmware actually reports it, checked on the board. Nothing listening
# comes back as ECONNRESET and not ECONNREFUSED, lwIP surfacing the RST that way. An
# address with nothing at it gives ECONNABORTED on the non-blocking path and ETIMEDOUT
# on a blocking one, so both are worded for what they mean.
_NET_ERRORS = {
    104: "no server answering",         # ECONNRESET: nothing there, or it went away
    103: "could not reach the host",    # ECONNABORTED
    110: "could not reach the host",    # ETIMEDOUT
    111: "connection refused",
    113: "host unreachable",
    2: "cannot resolve that name",
    -2: "cannot resolve that name",
}

_HTTP_ERRORS = {
    401: "signature rejected",
    403: "badge not recognised",
    404: "endpoint missing",
    429: "host is rate limiting",
    500: "host error",
}


def error_text(exc):
    """A socket error in words. Keeps the number when there is nothing better to say."""
    code = exc.args[0] if getattr(exc, "args", None) else None
    if code in _NET_ERRORS:
        return _NET_ERRORS[code]
    return f"network error {code}" if code is not None else "network error"


def http_error_text(status):
    return _HTTP_ERRORS.get(status) or f"host said {status}"


def _hmac_sha256(key, message):
    """HMAC-SHA256. MicroPython has hashlib but no hmac, and this is all it takes."""
    block = 64
    if len(key) > block:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (block - len(key))
    outer = bytes(b ^ 0x5C for b in key)
    inner = bytes(b ^ 0x36 for b in key)
    return hashlib.sha256(outer + hashlib.sha256(inner + message).digest()).digest()


def sign(secret_hex, method, path, seq, body=b""):
    key = binascii.unhexlify(secret_hex)
    digest = binascii.hexlify(hashlib.sha256(body or b"").digest()).decode()
    message = "\n".join((method.upper(), path, str(seq), digest))
    return binascii.hexlify(_hmac_sha256(key, message.encode("utf-8"))).decode()


class Config:
    """Every host this badge is paired with, persisted in /state.

    Credentials are keyed on the server's id, not its address, so a host that gets a
    new DHCP lease is still the same host: the beacon carries the id, and the address
    is just the latest place it was seen. Several hosts can be paired at once and the
    badge uses whichever is reachable, which is what makes a desk with two machines
    work without re-pairing.

    Each host keeps its own counter, because the counter is a conversation between one
    badge and one server. The counter is written back once it has advanced past a
    margin, not on every request: flash is finite and a badge polls all day.
    """

    SEQ_FLUSH = 64

    def __init__(self):
        self.badge_id = None
        self.hosts = {}          # server id -> {host, port, secret, name, seq}
        self.active = None       # the server id in use
        self._flushed = 0
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self):
        try:
            with open(STATE_FILE) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False

        self.badge_id = data.get("badge_id")
        if "hosts" in data:
            self.hosts = data["hosts"]
            self.active = data.get("active")
        elif data.get("secret"):
            # One flat host, as older installs and `--state-only` wrote it. Keep it
            # under a stand-in id until a beacon or /v1/hello carries the real one.
            self.hosts = {
                "unknown": {
                    "host": data.get("host"),
                    "port": int(data.get("port", 8420)),
                    "secret": data.get("secret"),
                    "name": data.get("host"),
                    "seq": int(data.get("seq", 0)),
                }
            }
            self.active = "unknown"

        if self.active not in self.hosts:
            self.active = next(iter(self.hosts), None)
        # Start above whatever was last written: anything in flight when the badge lost
        # power never made it to flash.
        for entry in self.hosts.values():
            entry["seq"] = int(entry.get("seq", 0)) + self.SEQ_FLUSH
        self._flushed = self.seq
        return self.paired

    def save(self):
        """Write our keys back, leaving anything else in the file alone.

        The app's page index lives here too, under "page", and State replaces a file
        wholesale - so both writers merge or one wipes the other.
        """
        try:
            try:
                import os
                os.mkdir("/state")
            except OSError:
                pass
            try:
                with open(STATE_FILE) as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data["badge_id"] = self.badge_id
            data["active"] = self.active
            data["hosts"] = self.hosts
            with open(STATE_FILE, "w") as handle:
                json.dump(data, handle)
            self._flushed = self.seq
            return True
        except OSError:
            return False

    # -- the host in use ----------------------------------------------------

    @property
    def entry(self):
        return self.hosts.get(self.active) or {}

    @property
    def host(self):
        return self.entry.get("host")

    @property
    def port(self):
        return int(self.entry.get("port") or 8420)

    @property
    def secret(self):
        return self.entry.get("secret")

    @property
    def name(self):
        return self.entry.get("name") or self.host

    @property
    def seq(self):
        return int(self.entry.get("seq", 0))

    @seq.setter
    def seq(self, value):
        if self.active in self.hosts:
            self.hosts[self.active]["seq"] = int(value)

    @property
    def paired(self):
        return bool(self.host and self.secret and self.badge_id)

    def next_seq(self):
        value = self.seq + 1
        self.seq = value
        if value - self._flushed >= self.SEQ_FLUSH:
            self.save()
        return value

    # -- adding and switching -----------------------------------------------

    def remember(self, server_id, host, port, secret, name=None, seq=0):
        """Store credentials for a host and make it the active one."""
        server_id = server_id or "unknown"
        self.hosts[server_id] = {
            "host": host, "port": int(port), "secret": secret,
            "name": name or host, "seq": int(seq),
        }
        self.active = server_id
        self._flushed = self.hosts[server_id]["seq"]
        return self.save()

    def switch(self, server_id):
        if server_id not in self.hosts or server_id == self.active:
            return False
        self.active = server_id
        self._flushed = self.seq
        self.save()
        return True

    def note_address(self, server_id, host, port, name=None):
        """Update where a known host lives, after a beacon reports it elsewhere.

        This is the DHCP case: same server, new address. Nothing else changes, so the
        secret and the counter carry over untouched.
        """
        entry = self.hosts.get(server_id)
        if entry is None:
            return False
        if entry.get("host") == host and int(entry.get("port", 0)) == int(port):
            return False
        entry["host"] = host
        entry["port"] = int(port)
        if name:
            entry["name"] = name
        self.save()
        return True

    def adopt_id(self, server_id, name=None):
        """Move credentials stored under the stand-in id onto the real one."""
        if not server_id or server_id in self.hosts or "unknown" not in self.hosts:
            return False
        entry = self.hosts.pop("unknown")
        if name:
            entry["name"] = name
        self.hosts[server_id] = entry
        if self.active == "unknown":
            self.active = server_id
        self.save()
        return True


class Client:
    """One keep-alive connection to the host, advanced a step at a time."""

    def __init__(self, config):
        self.config = config
        self.sock = None
        self.status = IDLE
        self.http_status = None
        self.body = None
        self.error = None
        self.headers = {}
        self.round_trip_ms = 0
        self.failures = 0
        self._gen = None
        self._started = 0
        self._buf = bytearray(2048)

    # -- connection ---------------------------------------------------------

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _connect(self):
        info = socket.getaddrinfo(self.config.host, self.config.port,
                                 0, socket.SOCK_STREAM)[0]
        sock = socket.socket(info[0], info[1], info[2])
        sock.setblocking(True)
        sock.connect(info[4])
        self.sock = sock

    # -- requests -----------------------------------------------------------

    def get(self, path):
        self._begin("GET", path, None)

    def post(self, path, payload):
        self._begin("POST", path, json.dumps(payload).encode("utf-8"))

    def _begin(self, method, path, body):
        self.status = BUSY
        self.error = None
        self.http_status = None
        self.body = None
        self._started = time.ticks_ms()
        self._gen = self._exchange(method, path, body or b"")

    def _exchange(self, method, path, body):
        if self.sock is None:
            self._connect()
            yield

        seq = self.config.next_seq()
        signature = sign(self.config.secret, method, path, seq, body)
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {self.config.host}\r\n"
            f"Connection: keep-alive\r\n"
            f"X-Badge-Id: {self.config.badge_id}\r\n"
            f"X-Badge-Seq: {seq}\r\n"
            f"X-Badge-Sig: {signature}\r\n"
        )
        if body:
            request += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        request += "\r\n"

        self.sock.setblocking(True)
        self.sock.write(request.encode("utf-8"))
        if body:
            self.sock.write(body)
        self.sock.setblocking(False)

        self.headers = {}
        while True:
            yield
            line = self.sock.readline()
            if line is None:
                continue
            if line in (b"\r\n", b"\n"):
                break
            if line.startswith(b"HTTP/"):
                try:
                    self.http_status = int(line.split(b" ")[1])
                except (IndexError, ValueError):
                    raise OSError("bad status line") from None
            elif b":" in line:
                key, _, value = line.decode("utf-8").partition(":")
                self.headers[key.strip().lower()] = value.strip()

        length = int(self.headers.get("content-length", 0))
        if length > len(self._buf):
            self._buf = bytearray(length)
        view = memoryview(self._buf)
        got = 0
        while got < length:
            yield
            read = self.sock.readinto(view[got:length])
            if read:
                got += read
        self.body = bytes(view[:got])

        if self.headers.get("connection", "").lower() == "close":
            self.close()

    def step(self):
        """Advance the current request. True when it has finished, either way.

        Drains for a short budget rather than exactly one yield, because a response
        needs a handful of reads and a frame has time for them.
        """
        if self._gen is None:
            return True
        deadline = time.ticks_add(time.ticks_us(), STEP_BUDGET_US)
        while True:
            try:
                next(self._gen)
            except StopIteration:
                self._gen = None
                self.round_trip_ms = time.ticks_diff(time.ticks_ms(), self._started)
                self.status = DONE if self.http_status == 200 else FAILED
                if self.status == FAILED:
                    self.failures += 1
                    self.error = http_error_text(self.http_status)
                    if self.http_status == 401:
                        self._resync()
                else:
                    self.failures = 0
                return True
            except OSError as exc:
                self._gen = None
                self.close()
                self.status = FAILED
                self.failures += 1
                self.error = error_text(exc)
                return True
            except Exception as exc:  # noqa: BLE001
                # Nothing from a socket may reach the draw loop: a surprise here has
                # to end as a failed request, not a crash dialog.
                self._gen = None
                self.close()
                self.status = FAILED
                self.failures += 1
                self.error = str(exc)[:40]
                return True

            if time.ticks_diff(time.ticks_ms(), self._started) > REQUEST_TIMEOUT_MS:
                self._gen = None
                self.close()
                self.status = FAILED
                self.failures += 1
                self.error = "timeout"
                return True
            if time.ticks_diff(time.ticks_us(), deadline) >= 0:
                return False

    def _resync(self):
        """Take the counter the host says to use next.

        A 401 over a counter means the two ends disagree - the badge rebooted and
        lost count, or it was provisioned against a different starting point. The
        host only offers `next_seq` once the signature has checked out, so it is the
        authority, and one request puts them back in step. Without this the counter
        has to be guessed at, which fails in whichever direction was not guessed.
        """
        payload = self.json() or {}
        wanted = payload.get("next_seq")
        if wanted is None:
            self.error = payload.get("error") or self.error
            return
        try:
            # next_seq() pre-increments, so sit one below what the host asked for.
            self.config.seq = int(wanted) - 1
        except (TypeError, ValueError):
            return
        self.config.save()
        self.error = f"resynced to {int(wanted)}"

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None


def discover(timeout_ms=4000):
    """Listen for host beacons, so nobody has to type an IP address.

    `statsbadge serve` broadcasts a small JSON beacon; this collects whatever answers
    within the timeout. Returns a list of dicts with `id`, `host`, `port` and `name`.
    Credentials are keyed on the id, so a host that changed address is still recognised.
    """
    found = []
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", 8421))
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                packet, address = sock.recvfrom(256)
            except OSError:
                time.sleep_ms(50)
                continue
            try:
                beacon = json.loads(packet)
            except ValueError:
                continue
            if not beacon.get("statsbadge"):
                continue
            entry = {
                "id": beacon.get("id"),
                # Trust the packet's source address over anything in the payload: it
                # is where replies will actually reach.
                "host": address[0],
                "port": int(beacon.get("port", 8420)),
                "name": beacon.get("host") or address[0],
            }
            if not any(e["host"] == entry["host"] and e["port"] == entry["port"]
                       for e in found):
                found.append(entry)
    except OSError:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
    return found


def hello(host, port, timeout_ms=4000):
    """Ask an unpaired host who it is. Returns a dict or None."""
    reply, _ = _get_json(host, port, "/v1/hello", timeout_ms)
    return reply


def enrol(host, port, badge_id, name=None, timeout_ms=8000):
    """Ask a host to be let in. Returns (reply, error); reply has `code` to display and
    `request_id` to poll with."""
    return _post_json(host, port, "/v1/enrol",
                      {"badge_id": badge_id, "name": name or badge_id}, timeout_ms)


def enrol_status(host, port, request_id, timeout_ms=6000):
    """Poll a request. Returns (reply, error); reply has `status`, and on approval the
    `secret`, the host `id` and `name`."""
    return _get_json(host, port, f"/v1/enrol/{request_id}", timeout_ms)


def _post_json(host, port, path, payload, timeout_ms):
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    )
    return _exchange_once(host, port, request, body, timeout_ms)


def _get_json(host, port, path, timeout_ms):
    request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    return _exchange_once(host, port, request, b"", timeout_ms)


def _exchange_once(host, port, request, body, timeout_ms):
    """One blocking request on its own socket. Setup screens only, where blocking is
    fine."""
    sock = None
    try:
        info = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
        sock = socket.socket(info[0], info[1], info[2])
        sock.settimeout(timeout_ms / 1000)
        sock.connect(info[4])
        sock.write(request.encode())
        if body:
            sock.write(body)
        raw = b""
        while True:
            chunk = sock.read(512)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 4096:
                break
        head, _, payload = raw.partition(b"\r\n\r\n")
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = {}
        if b" 200 " not in head.split(b"\r\n")[0]:
            return None, parsed or {"error": "refused"}
        return parsed, None
    except OSError as exc:
        return None, {"error": error_text(exc)}
    except ValueError:
        return None, {"error": "bad reply from the host"}
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
