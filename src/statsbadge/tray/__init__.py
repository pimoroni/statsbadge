"""The server on a thread, an icon on the main one.

pystray owns the main thread: on macOS `run()` drives NSApplication, and on Windows it
pumps the message loop. That is the other way round from `serve`.

The menu is built as plain data, so it can be checked without a display.
"""

import shutil
import signal
import subprocess
import sys
import threading
import webbrowser

from .. import auth, autostart, version
from .backend import SEPARATOR, Item

WATCH_INTERVAL = 2.0
PAIR_TTL = 300
SIGNALS = (signal.SIGINT, signal.SIGTERM)


def block_signals():
    """Before any thread starts, so every one of them inherits the block."""
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)


def quit_on_signal(quit_now):
    """Python runs a handler on the main thread between bytecodes, and that thread sits
    inside the toolkit's run loop. A thread waiting on the signal takes it instead."""
    if not hasattr(signal, "sigwait"):
        for number in SIGNALS:
            signal.signal(number, lambda *_: quit_now())
        return

    def wait():
        signal.sigwait(SIGNALS)
        quit_now()

    threading.Thread(target=wait, daemon=True, name="statsbadge-signals").start()


class TrayApp:
    def __init__(self, stack, log_path=None, config_dir=None, port=None,
                 launchd_log=None):
        self.stack = stack
        self.log_path = log_path
        self.launchd_log = launchd_log
        # Carried into an autostart entry, and only when they were asked for.
        self.config_dir = config_dir
        self.port = port
        self.status = stack.status()
        self.at_login = autostart.enabled()
        self.tray = None
        self._announced = set()
        self._stop = threading.Event()

    def refresh(self):
        self.status = self.stack.status()
        self.at_login = autostart.enabled()
        return self.summary()

    def summary(self):
        """Cheap, and enough to tell whether the menu would differ."""
        return (tuple(self.status["addresses"]),
                tuple(sorted(self.status["badges"])),
                tuple(sorted(e["request_id"] for e in self.status["pending"])),
                self.status["pairing"]["active"],
                self.at_login)

    def address(self):
        found = self.status["addresses"]
        return f"{found[0]}:{self.status['port']}" if found else None

    def title(self):
        where = self.address() or "no network"
        waiting = len(self.status["pending"])
        return f"statsbadge - {where}" + (f" - {waiting} waiting" if waiting else "")

    def model(self):
        status = self.status
        waiting = status["pending"]
        items = [
            Item(f"statsbadge {version()}", enabled=False),
            Item(f"Serving on {self.address() or 'no network address'}", enabled=False),
            Item(self._counts(), enabled=False),
            SEPARATOR,
            Item("Open the config UI", self.open_ui, default=True),
            Item("Copy the badge address", self.copy_address,
                 enabled=bool(status["addresses"])),
            SEPARATOR,
        ]
        if waiting:
            items.append(Item("Waiting to pair",
                              submenu=[self._waiting(entry) for entry in waiting]))
        items.append(Item(self._pairing_label(), self.toggle_pairing,
                          checked=status["pairing"]["active"]))
        items.append(SEPARATOR)
        if status["badges"]:
            items.append(Item("Badges", submenu=[
                Item(name, enabled=False)
                for name in auth.display_names(status["badges"])]))
            items.append(SEPARATOR)
        items.append(Item("Start at login", self.toggle_autostart, checked=self.at_login))
        if self.log_path:
            items.append(Item("Open the log", self.open_log))
        items.append(SEPARATOR)
        items.append(Item("Quit", self.quit))
        return items

    def _counts(self):
        badges = len(self.status["badges"])
        waiting = len(self.status["pending"])
        said = "1 badge paired" if badges == 1 else f"{badges} badges paired"
        return f"{said}, {waiting} waiting" if waiting else said

    def _pairing_label(self):
        state = self.status["pairing"]
        if not state["active"]:
            return f"Open pairing for {PAIR_TTL // 60} minutes"
        return f"Pairing closes in {state['expires_in']}s"

    def _waiting(self, entry):
        """The code is checked against the badge's screen, so approving is two steps."""
        return Item(f"{entry['name']} - code {entry['code']}", submenu=[
            Item("Approve", lambda request=entry["request_id"]: self.approve(request)),
            Item("Deny", lambda request=entry["request_id"]: self.deny(request)),
        ])

    def open_ui(self):
        webbrowser.open(f"http://127.0.0.1:{self.status['port']}/")

    def copy_address(self):
        address = self.address()
        if address:
            _to_clipboard(address)

    def open_log(self):
        webbrowser.open(f"file://{self.log_path}")

    def toggle_pairing(self):
        badges = self.stack.service.badges
        if badges.pairing_active():
            badges.cancel_pairing()
        else:
            badges.begin_pairing(ttl=PAIR_TTL)
        self.wake()

    def approve(self, request_id):
        self.stack.service.badges.approve_enrolment(request_id)
        self.wake()

    def deny(self, request_id):
        self.stack.service.badges.deny_enrolment(request_id)
        self.wake()

    def toggle_autostart(self):
        if self.at_login:
            autostart.disable()
        else:
            autostart.enable(config_dir=self.config_dir, port=self.port,
                             log=self.launchd_log)
        self.wake()

    def quit(self):
        self._stop.set()
        if self.tray:
            self.tray.stop()

    def wake(self):
        """Redraw now, rather than at the end of the next poll."""
        self.refresh()
        self._apply()

    def watch(self):
        was = self.summary()
        while not self._stop.wait(WATCH_INTERVAL):
            now = self.refresh()
            # update_menu rebuilds the whole GtkMenu on appindicator, so only on a change.
            if now != was or self.status["pairing"]["active"]:
                was = now
                self._apply()

    def _apply(self):
        if not self.tray:
            return
        waiting = self.status["pending"]
        self.tray.title(self.title())
        self.tray.attention(bool(waiting))
        self.tray.update()
        for entry in waiting:
            if entry["request_id"] in self._announced:
                continue
            self._announced.add(entry["request_id"])
            self.tray.notify(f"{entry['name']} wants to pair. Its code is "
                             f"{entry['code']}.", "statsbadge")
        self._announced &= {entry["request_id"] for entry in waiting}

    def run(self, tray, serving):
        self.tray = tray
        watcher = threading.Thread(target=self.watch, daemon=True,
                                   name="statsbadge-tray-watch")

        def started():
            serving()
            watcher.start()

        try:
            tray.run(setup=started)
        finally:
            self._stop.set()
        return 0


def _to_clipboard(text):
    if sys.platform == "darwin":
        argv = ["pbcopy"]
    elif sys.platform == "win32":
        argv = ["clip"]
    elif shutil.which("wl-copy"):
        argv = ["wl-copy"]
    else:
        argv = ["xclip", "-selection", "clipboard"]
    if not shutil.which(argv[0]):
        return False
    try:
        subprocess.run(argv, input=text.encode(), check=False)
    except OSError:
        return False
    return True
