"""Checks for the tray, its menu and run at login.

Needs no server, no display and no pystray, so it runs on a headless CI box.

    python3 tests/test_tray.py
"""

import configparser
import io
import os
import plistlib
import socket
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statsbadge import autostart, logs, runner  # noqa: E402
from statsbadge.tray import TrayApp  # noqa: E402
from statsbadge.tray.backend import SEPARATOR  # noqa: E402

CHECKS = []

ARGV = ["/opt/a place/statsbadge-tray", "--config-dir", "/tmp/a b", "--port", "8421"]


def check(fn):
    CHECKS.append(fn)
    return fn


class FakeBadges:
    def __init__(self):
        self.open = False
        self.done = []

    def pairing_active(self):
        return self.open

    def begin_pairing(self, ttl=300):  # noqa: ARG002  the Store's signature
        self.open = True

    def cancel_pairing(self):
        self.open = False

    def approve_enrolment(self, request_id):
        self.done.append(("approve", request_id))

    def deny_enrolment(self, request_id):
        self.done.append(("deny", request_id))


class FakeStack:
    """Enough of runner.Stack for a menu to be built from it."""

    def __init__(self, **status):
        self.service = type("Service", (), {"badges": FakeBadges()})()
        self._status = {"port": 8420, "addresses": ["10.0.0.5"], "badges": {},
                        "pending": [], "pairing": {"active": False, "expires_in": 0}}
        self._status.update(status)

    def status(self):
        return dict(self._status)


WAITING = {"request_id": "r1", "name": "Desk badge", "code": "4F9A2C",
           "waiting_s": 2, "expires_in": 120}


def labels(items):
    return [it.label for it in items if it is not SEPARATOR]


def find(items, label):
    for it in items:
        if it is not SEPARATOR and it.label == label:
            return it
    raise AssertionError(f"no {label!r} in {labels(items)}")


# -- the menu ---------------------------------------------------------------

@check
def test_a_waiting_badge_is_approved_by_its_own_code():
    """Two steps and one decision each, the same contract the CLI and the UI keep.

    Nothing approves every badge at once: a human compares the code on the screen with
    the code on the badge, and a menu that just repopulated must not pair one on a
    stray click.
    """
    stack = FakeStack(pending=[WAITING])
    app = TrayApp(stack)
    waiting = find(app.model(), "Waiting to pair")
    assert waiting.submenu, "a waiting badge is not offered"

    entry = waiting.submenu[0]
    assert "4F9A2C" in entry.label, entry.label
    assert labels(entry.submenu) == ["Approve", "Deny"], labels(entry.submenu)
    assert not any("all" in label.lower() for label in labels(app.model()))

    find(entry.submenu, "Approve").action()
    assert stack.service.badges.done == [("approve", "r1")]
    find(entry.submenu, "Deny").action()
    assert stack.service.badges.done[-1] == ("deny", "r1")


@check
def test_nothing_is_offered_that_cannot_be_done():
    """No badge waiting, no submenu. No address, nothing to copy."""
    quiet = TrayApp(FakeStack()).model()
    assert "Waiting to pair" not in labels(quiet)
    assert "Badges" not in labels(quiet)

    adrift = TrayApp(FakeStack(addresses=[])).model()
    assert not find(adrift, "Copy the badge address").enabled
    assert "no network" in find(adrift, "Serving on no network address").label


@check
def test_the_pairing_item_reports_the_window_it_opens():
    stack = FakeStack()
    app = TrayApp(stack)
    item = find(app.model(), "Open pairing for 5 minutes")
    assert item.checked is False
    item.action()
    assert stack.service.badges.open

    app = TrayApp(FakeStack(pairing={"active": True, "expires_in": 240}))
    assert find(app.model(), "Pairing closes in 240s").checked is True


@check
def test_the_summary_moves_only_when_the_menu_would():
    """appindicator rebuilds the whole menu on update, so a poll that changed nothing
    must not ask for one."""
    app = TrayApp(FakeStack())
    was = app.summary()
    assert app.refresh() == was

    app.stack._status["pending"] = [WAITING]
    assert app.refresh() != was


# -- run at login -----------------------------------------------------------

@check
def test_a_login_entry_round_trips():
    """Written, read back, asked for twice, and taken away again."""
    for backend in (autostart.LaunchAgent, autostart.Desktop):
        base = tempfile.mkdtemp()
        entry = backend(base)
        assert not entry.enabled()
        where = entry.enable(ARGV)
        assert entry.enabled(), where
        assert entry.enable(ARGV) == where, "asking twice moved it"
        assert _reads_back(backend, where) == ARGV
        assert entry.disable()
        assert not entry.enabled()
        assert not entry.disable(), "disabling twice claimed to do something"


def _reads_back(backend, where):
    if backend is autostart.LaunchAgent:
        with open(where, "rb") as handle:
            plist = plistlib.load(handle)
        assert plist["RunAtLoad"] is True
        # No KeepAlive: quitting from the menu means it.
        assert "KeepAlive" not in plist
        return plist["ProgramArguments"]

    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read(where)
    entry = parsed["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Terminal"] == "false"
    return _unquote(entry["Exec"])


def _unquote(line):
    """Undo the Desktop Entry quoting, so the argv can be compared with what went in."""
    out, part, quoted, escape = [], "", False, False
    for character in line:
        if escape:
            part, escape = part + character, False
        elif character == "\\" and quoted:
            escape = True
        elif character == '"':
            quoted = not quoted
        elif character == " " and not quoted:
            if part:
                out.append(part)
            part = ""
        else:
            part += character
    if part:
        out.append(part)
    return [entry.replace("%%", "%") for entry in out]


@check
def test_windows_keeps_its_entry_in_the_registry():
    if os.name != "nt":
        return
    entry = autostart.Registry()
    was = entry.enabled()
    try:
        entry.enable(ARGV)
        assert entry.enabled()
        assert entry.disable()
        assert not entry.enabled()
    finally:
        if was:
            entry.enable(ARGV)


@check
def test_what_runs_at_login_is_a_real_path():
    argv = autostart.command()
    assert argv, "nothing to run"
    assert os.path.isabs(argv[0]), argv
    # A relative directory is made absolute. Login starts this from somewhere else
    # entirely, and a path relative to where `autostart enable` was run is nowhere.
    with_flags = autostart.command(config_dir=os.path.join("some", "where"), port=9000)
    assert with_flags[-4] == "--config-dir", with_flags
    assert os.path.isabs(with_flags[-3]), with_flags
    assert with_flags[-2:] == ["--port", "9000"], with_flags


# -- the log ----------------------------------------------------------------

@check
def test_a_terminal_still_sees_everything_the_log_does():
    """Redirecting a terminal wholesale left `tray` looking hung.

    Without a tray it says why and serves anyway, and that sentence went to the log file
    where nobody was looking. A terminal is echoed to, and the log keeps the record.
    """
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    was = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    watching = Terminal()
    directory = tempfile.mkdtemp()
    try:
        sys.stdout = sys.stderr = watching
        target = logs.start(directory, "watched")
        print("pystray is not installed")
        assert sys.stdout.isatty(), "a terminal behind it should still report as one"
    finally:
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = was

    assert "pystray is not installed" in watching.getvalue(), watching.getvalue()
    assert "pystray is not installed" in open(target).read()


@check
def test_a_print_survives_having_nowhere_to_print():
    """sys.stdout is None under pythonw, and inside an .app bundle. Every print raises."""
    was = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    directory = tempfile.mkdtemp()
    try:
        sys.stdout = sys.stderr = None
        target = logs.start(directory, "check")
        print("a line")
        print("held", end="")
        sys.stdout.flush()
        print("through", file=sys.stderr)
        assert not sys.stdout.isatty()
        try:
            sys.stdout.fileno()
            raise AssertionError("claimed a descriptor it does not have")
        except io.UnsupportedOperation:
            pass
    finally:
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = was

    written = open(target).read()
    for wanted in ("a line", "held", "through"):
        assert wanted in written, (wanted, written)
    assert target == logs.path(directory, "check")


# -- the port ---------------------------------------------------------------

@check
def test_a_port_nobody_holds_answers_nothing():
    """The single-instance check, and the only guard on Windows, where SO_REUSEADDR
    lets a second bind succeed."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    assert runner.already_serving(free, timeout=0.3) is None


def main():
    failures = []
    for fn in CHECKS:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:
            failures.append(fn.__name__)
            print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    print(f"{len(CHECKS)} checks, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
