"""Fixtures for the suite, and the path and builtins setup every test file relies on.

pytest imports this before any test module, which makes the badge stand-ins work:
`look` builds a Theme at import, so cannot be imported without `color`, and every module
under `badge_app/` reaches `look`. Installing them here is the one ordering guarantee.
"""

import ast
import html.parser
import json
import pathlib
import re
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

# badge/wasm/ is MicroPython, run against the firmware by `node tools/wasm/run.mjs`. It
# reaches `screen` and `rect`, so it cannot be imported here at all, let alone collected.
collect_ignore_glob = ["badge/wasm/*.py"]

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


class Markup(html.parser.HTMLParser):
    """Every element the page gives an id, and the tag it is."""

    def __init__(self):
        super().__init__()
        self.ids = {}

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if found.get("id"):
            self.ids[found["id"]] = tag


class ConfigUI:
    """The config UI as data: what index.html defines, and what app.js binds each id to.

    Matching a binding as a substring - `'bindCheck("animate", "animate")' in script` -
    breaks on a reformat, and passes a control bound to a setting the server
    rejects. Reading the calls out gives the pair to check against the real schema.
    """

    def __init__(self, web):
        self.markup = (web / "index.html").read_text(encoding="utf-8")
        self.script = (web / "app.js").read_text(encoding="utf-8")
        self.css = (web / "app.css").read_text(encoding="utf-8")
        parser = Markup()
        parser.feed(self.markup)
        self.ids = parser.ids
        self.bindings = dict(self._bindings())

    def _bindings(self):
        for match in re.finditer(r"\bbind(Range|Check|Select)\b", self.script):
            if self.script[:match.start()].rstrip().endswith("function"):
                continue                      # the definition, not a call of it
            body = self._call_at(match.end())
            named = re.match(r'\(\s*"([^"]+)"', body)
            assert named, body[:60]
            if match.group(1) == "Select":
                # A select reads and writes through closures, so its setting is whatever
                # the write half assigns.
                keys = sorted(set(re.findall(r"config\.(\w+)\s*=(?!=)", body)))
                assert len(keys) == 1, (named.group(1), keys)
                yield named.group(1), keys[0]
                continue
            yield named.group(1), re.match(r'\(\s*"[^"]+"\s*,\s*"([^"]+)"', body).group(1)

    @property
    def constants(self):
        """The script's `const NAME = <number>` declarations, several of which restate a
        figure from look.py: JavaScript cannot import it, so the two are checked instead."""
        return {name: float(value) if "." in value else int(value)
                for name, value in re.findall(
                    r"^const ([A-Z][A-Z0-9_]*) = (-?[\d.]+)\s*$", self.script, re.M)}

    def function(self, name):
        """One named function's body, to the brace that closes it.

        Slicing a fixed number of characters after the name reads whatever happens to
        follow, so a helper moved in above the line under test quietly empties the check.
        """
        start = self.script.index(f"function {name}")
        opened = self.script.index("{", start)
        depth = 0
        for position in range(opened, len(self.script)):
            if self.script[position] == "{":
                depth += 1
            elif self.script[position] == "}":
                depth -= 1
                if depth == 0:
                    return self.script[opened:position + 1]
        raise AssertionError(f"unbalanced braces in {name}")

    def _call_at(self, start):
        """From the bracket after the name, to the one that closes it."""
        opened = self.script.index("(", start)
        depth = 0
        for position in range(opened, len(self.script)):
            if self.script[position] == "(":
                depth += 1
            elif self.script[position] == ")":
                depth -= 1
                if depth == 0:
                    return self.script[opened:position + 1]
        raise AssertionError(f"unbalanced brackets from {opened}")


@pytest.fixture(scope="session")
def ui(web_dir):
    return ConfigUI(web_dir)


@pytest.fixture(scope="session")
def badge_constants():
    """Read a badge module's module-level constants without importing it.

    Several of these have to agree with a host-side figure, and `badge_app/app.py`
    cannot be imported on a host at all. Matching the assignment as text breaks on a
    comment or a reflow, and proves nothing about the value, so the source is parsed and the
    constants evaluated in order, each seeing the ones above it.
    """
    def constants(module):
        # A name for one of the app's modules, or a Path for an extension's. Not sniffed
        # out of the string: a Windows path has no forward slash in it.
        where = (module if isinstance(module, pathlib.Path)
                 else pathlib.Path(install.app_source_dir()) / module)
        source = where.read_text(encoding="utf-8")
        found = {}
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            names = target.elts if isinstance(target, ast.Tuple) else [target]
            if not all(isinstance(name, ast.Name) for name in names):
                continue
            try:
                value = eval(  # noqa: S307  a module in this repo, constants only
                    compile(ast.Expression(node.value), module, "eval"), {}, dict(found))
            except Exception:  # noqa: BLE001  anything needing the firmware is not a constant
                continue
            if isinstance(target, ast.Tuple):
                found.update(zip((name.id for name in names), value, strict=True))
            else:
                found[target.id] = value
        return found
    return constants


def headers(badge_id, seq, secret, method="GET", path="/v1/stats", body=b""):
    """A signed request's headers, for a test building one by hand."""
    return {
        auth.SIGNED_HEADER_ID: badge_id,
        auth.SIGNED_HEADER_SEQ: str(seq),
        auth.SIGNED_HEADER_SIG: auth.sign(secret, method, path, seq, body),
    }
