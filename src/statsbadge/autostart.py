"""Run the tray at login.

A registry value on Windows, a LaunchAgent on macOS, an XDG autostart entry elsewhere.
Each backend takes its own base directory, so a test can drive one anywhere.

The base is the desktop's, not config_dir(), which on macOS and Windows is somewhere
else entirely.
"""

import os
import plistlib
import shutil
import subprocess
import sys

from . import bundled
from .tooling import quoted

NAME = "statsbadge"
LABEL = "com.pimoroni.statsbadge"
SUMMARY = "Your PC's vitals on a Badgeware badge"


def backend(base=None):
    if os.name == "nt":
        return Registry(base)
    if sys.platform == "darwin":
        return LaunchAgent(base)
    return Desktop(base)


def enabled(base=None):
    return backend(base).enabled()


def enable(base=None, config_dir=None, port=None, log=None):
    return backend(base).enable(command(config_dir, port), log)


def disable(base=None):
    return backend(base).disable()


def describe(base=None, config_dir=None, port=None):
    it = backend(base)
    return {"enabled": it.enabled(), "where": it.where(),
            "command": command(config_dir, port)}


def command(config_dir=None, port=None):
    argv = launcher()
    if config_dir:
        argv += ["--config-dir", os.path.abspath(config_dir)]
    if port:
        argv += ["--port", str(port)]
    return argv


def launcher():
    """What to run, most specific first.

    Beside sys.executable pins the environment this is running from, which for a uv tool
    is the one holding the extensions.
    """
    # A packaged app is its own launcher. Its executable takes no `-m`, and a
    # statsbadge-tray found on PATH would be some other install of it entirely.
    if bundled():
        return [sys.executable]
    exe = "statsbadge-tray.exe" if os.name == "nt" else "statsbadge-tray"
    beside = os.path.join(os.path.dirname(sys.executable), exe)
    if os.path.isfile(beside):
        return [beside]
    found = shutil.which("statsbadge-tray")
    if found:
        return [found]
    found = shutil.which(NAME)
    if found:
        return [found, "tray"]
    return [sys.executable, "-m", NAME, "tray"]


class Registry:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, base=None):
        self.base = base or self.KEY

    def where(self):
        return f"HKCU\\{self.base}\\{NAME}"

    def enabled(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.base) as key:
                winreg.QueryValueEx(key, NAME)
        except OSError:
            return False
        return True

    def enable(self, argv, log=None):
        del log
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.base) as key:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, quoted(argv))
        return self.where()

    def disable(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.base, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, NAME)
        except OSError:
            return False
        return True


class LaunchAgent:
    def __init__(self, base=None):
        # launchctl only against the real directory, so a test cannot register anything.
        self.live = base is None
        self.base = base or os.path.expanduser("~/Library/LaunchAgents")

    def where(self):
        return os.path.join(self.base, f"{LABEL}.plist")

    def enabled(self):
        return os.path.isfile(self.where())

    def enable(self, argv, log=None):
        os.makedirs(self.base, exist_ok=True)
        # No KeepAlive. Quitting from the menu means it.
        entry = {"Label": LABEL, "ProgramArguments": list(argv),
                 "RunAtLoad": True, "ProcessType": "Interactive"}
        if log:
            # Anything printed before logs.start replaces the streams, which is where
            # a missing extra or a broken import shows up.
            os.makedirs(os.path.dirname(log), exist_ok=True)
            entry["StandardOutPath"] = entry["StandardErrorPath"] = log
        with open(self.where(), "wb") as handle:
            plistlib.dump(entry, handle)
        if self.live:
            _launchctl("bootstrap", f"gui/{os.getuid()}", self.where())
        return self.where()

    def disable(self):
        target = self.where()
        if not os.path.isfile(target):
            return False
        if self.live:
            _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        os.remove(target)
        return True


class Desktop:
    def __init__(self, base=None):
        self.base = base or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "autostart")

    def where(self):
        return os.path.join(self.base, f"{NAME}.desktop")

    def enabled(self):
        return os.path.isfile(self.where())

    def enable(self, argv, log=None):
        del log
        os.makedirs(self.base, exist_ok=True)
        with open(self.where(), "w", encoding="utf-8") as handle:
            handle.write("[Desktop Entry]\n"
                         "Type=Application\n"
                         f"Name={NAME}\n"
                         f"Comment={SUMMARY}\n"
                         f"Exec={_desktop_exec(argv)}\n"
                         "Terminal=false\n"
                         "X-GNOME-Autostart-enabled=true\n")
        return self.where()

    def disable(self):
        target = self.where()
        if not os.path.isfile(target):
            return False
        os.remove(target)
        return True


def _launchctl(*argv):
    """Takes effect now instead of at the next login."""
    tool = shutil.which("launchctl")
    if not tool:
        return
    try:
        subprocess.run([tool, *argv], capture_output=True, check=False)
    except OSError:
        pass


def _desktop_exec(argv):
    """Desktop Entry quoting: a literal % doubles, and a quoted part escapes \\ " ` $."""
    parts = []
    for original in argv:
        part = original.replace("%", "%%")
        if any(mark in part for mark in ' \t"\'\\><~|&;$*?#()`'):
            for mark in '\\"`$':
                part = part.replace(mark, "\\" + mark)
            part = f'"{part}"'
        parts.append(part)
    return " ".join(parts)
