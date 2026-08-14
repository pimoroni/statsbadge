"""Fixtures for the suite, and the path and builtins setup every test file relies on.

pytest imports this before any test module, which is what makes the badge stand-ins work:
`look` builds a Theme at import and so cannot be imported without `color`, and every module
under `badge_app/` reaches `look`. Installing them here is the one ordering guarantee.
"""

import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import badgefakes  # noqa: E402  the paths above have to be set before this

badgefakes.install()

from statsbadge import auth, install, server  # noqa: E402

class Harness:
    """A running server, its store, and a badge paired with it."""

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

@pytest.fixture(scope="session")
def h():
    harness = Harness()
    yield harness
    harness.stop()

@pytest.fixture(autouse=True)
def _tidy_enrolments(request):
    """Clear anything a test left waiting: a request counts against the enrolment cap.

    Only for tests that took the harness, so the rest never start a server.
    """
    yield
    if "h" not in request.fixturenames:
        return
    harness = request.getfixturevalue("h")
    for waiting in harness.service.badges.pending_enrolments():
        harness.service.badges.deny_enrolment(waiting["request_id"])
    harness.service.badges.cancel_pairing()

@pytest.fixture(scope="session")
def repo_root():
    """The checkout, so a test reads a file by its place in the tree and not by the cwd."""
    return ROOT

@pytest.fixture(scope="session")
def web_dir(repo_root):
    return repo_root / "src" / "statsbadge" / "web"

@pytest.fixture(scope="session")
def badge_modules():
    """The badge app's modules, imported as the badge imports them: top level, off app_dir."""
    directory = install.app_source_dir()
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import draw
    import look
    import net
    import pages
    import worldmap
    return {"draw": draw, "look": look, "net": net, "pages": pages, "worldmap": worldmap}


def headers(badge_id, seq, secret, method="GET", path="/v1/stats", body=b""):
    """A signed request's headers, for a test building one by hand."""
    return {
        auth.SIGNED_HEADER_ID: badge_id,
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: auth.sign(secret, method, path, seq, body),
    }
