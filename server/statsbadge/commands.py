"""Things a badge button can ask the host to do.

Every command is opt-in: the config maps a button to a command name, and a name not
in the registry is refused. Nothing here takes an argument from the badge that
reaches a shell, because a signed request is still a request from a device that
lives in a bag.
"""

import platform
import shutil
import subprocess

_SYSTEM = platform.system()


class CommandError(Exception):
    pass


def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise CommandError(f"{argv[0]} is not available here") from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{argv[0]} timed out") from None
    if done.returncode != 0:
        raise CommandError((done.stderr or done.stdout or "failed").strip()[:200])
    return (done.stdout or "").strip()


def _osascript(script):
    return _run(["osascript", "-e", script])


# -- volume -----------------------------------------------------------------

def _volume_delta(step):
    if _SYSTEM == "Darwin":
        current = int(_osascript("output volume of (get volume settings)"))
        target = max(0, min(100, current + step))
        _osascript(f"set volume output volume {target}")
        return {"volume": target}
    if _SYSTEM == "Linux":
        sign = "+" if step > 0 else "-"
        if shutil.which("pactl"):
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                  f"{sign}{abs(step)}%"])
            return {"volume": None}
        if shutil.which("amixer"):
            _run(["amixer", "-q", "sset", "Master", f"{abs(step)}%{sign}"])
            return {"volume": None}
        raise CommandError("no pactl or amixer")
    if _SYSTEM == "Windows":
        raise CommandError("volume needs a helper on Windows")
    raise CommandError("unsupported platform")


def volume_up():
    return _volume_delta(5)


def volume_down():
    return _volume_delta(-5)


def mute():
    if _SYSTEM == "Darwin":
        muted = _osascript("output muted of (get volume settings)") == "true"
        _osascript(
            f"set volume {'without' if muted else 'with'} output muted")
        return {"muted": not muted}
    if _SYSTEM == "Linux":
        if shutil.which("pactl"):
            _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
            return {"muted": None}
        raise CommandError("no pactl")
    raise CommandError("unsupported platform")


# -- media ------------------------------------------------------------------

def play_pause():
    if _SYSTEM == "Darwin":
        # Key code 16 is F13-space on the media row; targeting the frontmost player
        # is more predictable than a system-wide key event.
        _osascript('tell application "System Events" to key code 49 using {}')
        return {"ok": True}
    if _SYSTEM == "Linux":
        if shutil.which("playerctl"):
            _run(["playerctl", "play-pause"])
            return {"ok": True}
        raise CommandError("no playerctl")
    raise CommandError("unsupported platform")


def next_track():
    if _SYSTEM == "Linux" and shutil.which("playerctl"):
        _run(["playerctl", "next"])
        return {"ok": True}
    if _SYSTEM == "Darwin":
        _osascript('tell application "Music" to next track')
        return {"ok": True}
    raise CommandError("unsupported platform")


# -- session ----------------------------------------------------------------

def lock():
    if _SYSTEM == "Darwin":
        _run(["pmset", "displaysleepnow"])
        return {"ok": True}
    if _SYSTEM == "Linux":
        for argv in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
            if shutil.which(argv[0]):
                _run(argv)
                return {"ok": True}
        raise CommandError("no loginctl or xdg-screensaver")
    if _SYSTEM == "Windows":
        _run(["rundll32.exe", "user32.dll,LockWorkStation"])
        return {"ok": True}
    raise CommandError("unsupported platform")


def sleep_host():
    if _SYSTEM == "Darwin":
        _osascript('tell application "System Events" to sleep')
        return {"ok": True}
    if _SYSTEM == "Linux":
        _run(["systemctl", "suspend"])
        return {"ok": True}
    if _SYSTEM == "Windows":
        _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return {"ok": True}
    raise CommandError("unsupported platform")


def screenshot():
    """Take a screenshot on the host. Returns where it went, not the image."""
    import os
    import time
    target = os.path.expanduser(f"~/Desktop/badge-{int(time.time())}.png")
    if _SYSTEM == "Darwin":
        _run(["screencapture", "-x", target])
        return {"path": target}
    if _SYSTEM == "Linux":
        for argv in (["gnome-screenshot", "-f", target], ["scrot", target]):
            if shutil.which(argv[0]):
                _run(argv)
                return {"path": target}
        raise CommandError("no screenshot tool")
    raise CommandError("unsupported platform")


REGISTRY = {
    "volume_up": volume_up,
    "volume_down": volume_down,
    "mute": mute,
    "play_pause": play_pause,
    "next_track": next_track,
    "lock": lock,
    "sleep": sleep_host,
    "screenshot": screenshot,
}


def names():
    return sorted(REGISTRY)


def run(name):
    handler = REGISTRY.get(name)
    if handler is None:
        raise CommandError(f"unknown command: {name}")
    return handler() or {"ok": True}
