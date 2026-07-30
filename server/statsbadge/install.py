"""Install the badge app and its credentials over USB.

`/system` is read-only to MicroPython, so the app itself cannot be written over the
serial REPL: it goes on by putting the badge into USB mass storage mode, which means
a reset and a volume appearing. What *can* be written over the REPL is `/state`,
which is where the pairing secret goes - so the common case, re-pairing or moving to
a new host, needs no remount at all.

Nothing here remounts a filesystem behind the user's back: pushing the app asks
first, because it resets the badge.
"""

import glob
import json
import os
import platform
import shutil
import subprocess
import time

APP_NAME = "stats"
STATE_FILE = "/state/stats.json"


class InstallError(Exception):
    pass


# -- finding the badge ------------------------------------------------------

# 0x2E8A is Raspberry Pi's vendor id, and a debug probe shares it with the board it
# is attached to. Talking MicroPython to a CMSIS-DAP interface just times out, so
# these are excluded by product id rather than hoping the ordering works out.
NOT_A_BADGE_PIDS = frozenset((
    0x0003,     # RP2 BOOTSEL mass storage
    0x0004,     # picoprobe
    0x000C,     # Debug Probe (CMSIS-DAP)
    0x000A,     # Pico SDK stdio only
))


def find_ports():
    """Serial ports that look like a Badgeware board, best candidate first."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return _find_ports_by_glob()

    ranked = []
    for port in list_ports.comports():
        if port.vid != 0x2E8A or port.pid in NOT_A_BADGE_PIDS:
            continue
        text = " ".join(filter(None, (port.product, port.manufacturer))).lower()
        if "cmsis" in text or "debug probe" in text:
            continue
        # A board that says MicroPython is a surer thing than one that does not.
        rank = 0 if "micropython" in text else 1
        ranked.append((rank, port.device))
    # pyserial can see vid/pid, so its answer is authoritative: falling back to a
    # glob here would hand back the debug probe it just ruled out.
    ranked.sort()
    return [device for _, device in ranked]


def _find_ports_by_glob():
    if platform.system() == "Darwin":
        return sorted(glob.glob("/dev/cu.usbmodem*"))
    if platform.system() == "Linux":
        return sorted(glob.glob("/dev/ttyACM*"))
    return []


def _mpremote():
    exe = shutil.which("mpremote")
    if not exe:
        raise InstallError(
            "mpremote not found. pip install mpremote, or pass --port and use the "
            "USB volume by hand."
        )
    return exe


def _run(port, *args, timeout=30):
    argv = [_mpremote(), "connect", port] + list(args)
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        if "in use" in detail:
            raise InstallError(
                f"{port} is busy. Close Thonny, a serial monitor or another mpremote."
            )
        raise InstallError(detail or "mpremote failed")
    return done.stdout


def badge_id(port):
    """The badge's uid, which is what it identifies itself as when signing."""
    out = _run(port, "exec", "import badgeware; print(badge.uid)")
    uid = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not uid:
        raise InstallError(f"could not read the badge uid from {port}")
    return uid


def badge_info(port):
    out = _run(port, "exec", (
        "import badgeware, os, sys\n"
        "print(badge.model)\n"
        "print(badge.uid)\n"
        "print('stats' in os.listdir('/system/apps'))\n"
    ))
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if len(lines) < 3:
        raise InstallError(f"unexpected reply from the badge: {out!r}")
    return {"model": lines[-3], "uid": lines[-2], "app_installed": lines[-1] == "True"}


# -- credentials ------------------------------------------------------------

def write_state(port, host, http_port, secret, badge_uid, seq=0):
    """Write the app's config to /state, which MicroPython can write directly.

    This is the whole of pairing from the badge's side: where the host is and what
    key to sign with. `seq` has to match the counter the server recorded for this
    badge, or the badge's first request is outside the replay window.
    """
    payload = json.dumps({
        "host": host,
        "port": http_port,
        "secret": secret,
        "badge_id": badge_uid,
        "seq": seq,
    })
    script = (
        "import os, json\n"
        "try:\n"
        "    os.mkdir('/state')\n"
        "except OSError:\n"
        "    pass\n"
        f"open({STATE_FILE!r}, 'w').write({payload!r})\n"
        f"print('wrote', {STATE_FILE!r})\n"
    )
    out = _run(port, "exec", script)
    if "wrote" not in out:
        raise InstallError(f"could not write {STATE_FILE}: {out.strip()}")
    return STATE_FILE


def read_state(port):
    out = _run(port, "exec",
               f"try:\n print(open({STATE_FILE!r}).read())\nexcept OSError:\n print('')\n")
    text = out.strip()
    if not text:
        return None
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        return None


# -- the app itself ---------------------------------------------------------

def app_source_dir():
    """Where the badge app lives in this checkout or wheel."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "badge_app"),                        # packaged
        os.path.join(here, "..", "..", APP_NAME),               # repo checkout
    ):
        resolved = os.path.normpath(candidate)
        if os.path.isfile(os.path.join(resolved, "__init__.py")):
            return resolved
    raise InstallError("cannot find the badge app source")


def enter_mass_storage(port):
    """Ask the badge to present its USB volume.

    The `mass_storage` app does this from the launcher; doing it over the REPL means
    importing the same `_msc` the double-tap path uses. The badge resets, so the
    serial port goes away and comes back.
    """
    try:
        _run(port, "exec", "import _msc", timeout=10)
    except (InstallError, subprocess.TimeoutExpired):
        # Expected: the board resets mid-command and the REPL never replies.
        pass


def wait_for_volume(timeout=30):
    """Wait for the badge's FAT volume to mount, and return its path."""
    deadline = time.time() + timeout
    candidates = _volume_candidates()
    while time.time() < deadline:
        for path in _volume_candidates():
            if os.path.isdir(os.path.join(path, "system", "apps")):
                return path
            if os.path.isdir(os.path.join(path, "apps")):
                return path
        time.sleep(0.5)
    raise InstallError(
        "the badge's USB volume did not appear. Double-tap RESET and try again."
        + (" Saw: {}".format(", ".join(candidates)) if candidates else "")
    )


def _volume_candidates():
    system = platform.system()
    if system == "Darwin":
        return sorted(glob.glob("/Volumes/*"))
    if system == "Linux":
        user = os.environ.get("USER", "")
        return sorted(glob.glob(f"/media/{user}/*") + glob.glob(f"/run/media/{user}/*"))
    if system == "Windows":
        return [f"{chr(letter)}:\\" for letter in range(ord("D"), ord("Z") + 1)
                if os.path.isdir(f"{chr(letter)}:\\")]
    return []


def copy_app(volume, source=None, extra_modules=()):
    """Copy the app onto a mounted badge volume."""
    source = source or app_source_dir()
    apps = os.path.join(volume, "system", "apps")
    if not os.path.isdir(apps):
        apps = os.path.join(volume, "apps")
    if not os.path.isdir(apps):
        raise InstallError(f"{volume} does not look like a badge volume")

    target = os.path.join(apps, APP_NAME)
    os.makedirs(target, exist_ok=True)
    copied = []
    for name in sorted(os.listdir(source)):
        if name.startswith(".") or name == "__pycache__":
            continue
        src = os.path.join(source, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(target, name), dirs_exist_ok=True)
        else:
            shutil.copy2(src, os.path.join(target, name))
        copied.append(name)

    if extra_modules:
        # `ext`, not `pages`: see load_extensions() in the app - a `pages` directory
        # would shadow the app's pages.py module.
        ext = os.path.join(target, "ext")
        os.makedirs(ext, exist_ok=True)
        for _name, path in extra_modules:
            shutil.copy2(path, os.path.join(ext, os.path.basename(path)))
            copied.append(f"ext/{os.path.basename(path)}")
    return target, copied


def wait_for_port(timeout=40, previous=None):
    """Wait for a badge's REPL to come back after a reset, and return its port.

    Enumeration is not instant and the port may come back under a different name, so
    this polls rather than assuming the old path still works.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in find_ports():
            if previous and port == previous:
                # Same path: make sure it actually answers before trusting it.
                try:
                    badge_id(port)
                except (InstallError, subprocess.SubprocessError):
                    continue
            return port
        time.sleep(1.0)
    raise InstallError("the badge did not come back after resetting")


def eject(volume):
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["diskutil", "eject", volume], capture_output=True, timeout=20)
        elif system == "Linux":
            subprocess.run(["sync"], capture_output=True, timeout=20)
            subprocess.run(["udisksctl", "unmount", "-b", volume],
                           capture_output=True, timeout=20)
    except Exception:
        pass
