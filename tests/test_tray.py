"""Checks for the tray, its menu and run at login.

Needs no server, no display and no pystray, so it runs on a headless CI box.

    uv run pytest tests/test_tray.py
"""

import configparser
import io
import os
import pathlib
import plistlib
import socket
import sys
import tempfile
import tomllib

import pytest

from statsbadge import autostart, logs, runner
from statsbadge.tray import TrayApp
from statsbadge.tray.backend import SEPARATOR

ARGV = ["/opt/a place/statsbadge-tray", "--config-dir", "/tmp/a b", "--port", "8421"]

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

def test_a_waiting_badge_is_approved_one_at_a_time_by_its_code():
    """Approving takes two steps and names one badge, so there is no menu item that
    pairs every badge waiting."""
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


def test_nothing_is_offered_that_cannot_be_done():
    """No badge waiting, no submenu. No address, nothing to copy."""
    quiet = TrayApp(FakeStack()).model()
    assert "Waiting to pair" not in labels(quiet)
    assert "Badges" not in labels(quiet)

    adrift = TrayApp(FakeStack(addresses=[])).model()
    assert not find(adrift, "Copy the badge address").enabled
    assert "no network" in find(adrift, "Serving on no network address").label


def test_the_pairing_item_reports_the_window_it_opens():
    stack = FakeStack()
    app = TrayApp(stack)
    item = find(app.model(), "Open pairing for 5 minutes")
    assert item.checked is False
    item.action()
    assert stack.service.badges.open

    app = TrayApp(FakeStack(pairing={"active": True, "expires_in": 240}))
    assert find(app.model(), "Pairing closes in 240s").checked is True


def test_the_summary_moves_only_when_the_menu_would():
    """appindicator rebuilds the whole menu on update, so a poll that changed nothing
    must not ask for one."""
    app = TrayApp(FakeStack())
    was = app.summary()
    assert app.refresh() == was

    app.stack._status["pending"] = [WAITING]
    assert app.refresh() != was


# -- run at login -----------------------------------------------------------

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
        # No KeepAlive, or quitting from the menu starts it again.
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


@pytest.mark.skipif(os.name != "nt", reason="the registry entry is Windows only")
def test_windows_keeps_its_entry_in_the_registry():
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


def test_the_gui_entry_point_still_takes_a_command():
    """A bare word reaches the command it names, and a flag goes to the tray."""
    # A packaged app has this as its only entry point: `ext` and `status` are how CI asks
    # the built app whether it works, and a login entry passes flags.
    from statsbadge import PIP_VERB, __main__ as cli

    ran = []
    was_main = cli.main
    try:
        cli.main = ran.append
        cli.tray_main([])
        cli.tray_main(["--config-dir", "/tmp/x"])
        cli.tray_main(["ext", "outdated"])
        cli.tray_main([PIP_VERB, "--version"])
    finally:
        cli.main = was_main
    assert ran == [["tray"], ["tray", "--config-dir", "/tmp/x"],
                   ["ext", "outdated"], [PIP_VERB, "--version"]], ran


def test_the_packaged_app_names_files_that_are_there():
    """Every source and icon the briefcase config names is on disk."""
    # Briefcase falls back to its mascot for an icon it cannot find, and says so in one
    # line among hundreds.
    root = pathlib.Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)["tool"]["briefcase"]
    app = config["app"]["statsbadge-tray"]

    for source in app["sources"]:
        assert (root / source / "__main__.py").is_file(), source
    for suffix in (".icns", ".ico"):
        icon = (root / app["icon"]).with_suffix(suffix)
        assert icon.is_file(), f"{icon} is what the bundle would fall back from"
    # statsbadge goes in as a wheel: without its .dist-info the bundle reports no version
    # and finds no extensions.
    assert "." in app["requires"], app["requires"]


def test_a_packaged_app_runs_itself_at_login():
    """A bundle runs its sys.executable at login, not a statsbadge-tray found on PATH."""
    was = sys.executable
    try:
        sys.executable = os.path.join(os.sep, "Applications", "statsbadge.app",
                                      "Contents", "MacOS", "statsbadge")
        assert autostart.command() == [sys.executable]
        assert autostart.command(port=8420)[0] == sys.executable
    finally:
        sys.executable = was


def test_what_runs_at_login_is_a_real_path():
    argv = autostart.command()
    assert argv, "nothing to run"
    assert os.path.isabs(argv[0]), argv
    # Login starts this from somewhere else, so a relative directory is made absolute.
    with_flags = autostart.command(config_dir=os.path.join("some", "where"), port=9000)
    assert with_flags[-4] == "--config-dir", with_flags
    assert os.path.isabs(with_flags[-3]), with_flags
    assert with_flags[-2:] == ["--port", "9000"], with_flags


# -- the log ----------------------------------------------------------------

def test_a_terminal_still_sees_everything_the_log_does():
    """A line goes to the terminal as well as the log, and the terminal still reports as
    one."""
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


def test_a_print_survives_having_nowhere_to_print():
    """A print still lands in the log where sys.stdout is None, as under pythonw."""
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

def test_a_port_nobody_holds_answers_nothing():
    """A free port answers nothing, which is what the single-instance check reads."""
    # The only guard on Windows, where SO_REUSEADDR lets a second bind succeed.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    assert runner.already_serving(free, timeout=0.3) is None
