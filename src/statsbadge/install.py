"""Install the badge app and its credentials over USB."""

import glob
import hashlib
import json
import os
import re
import pathlib
import platform
import shutil
import subprocess
import time

from . import NO_WINDOW, repl

APP_NAME = "stats"
STATE_FILE = "/state/stats.json"
# Where an extension's badge modules go under the app directory, one directory each.
EXT_DIR = "ext"


class InstallError(Exception):
    pass


class PortBusy(InstallError):
    """Something else has the port, so the badge was never reached."""


# MICROPY_HW_USB_PID from each board definition. The vendor id is Raspberry Pi's,
# shared with every other RP2350 board.
BADGE_VID = 0x2E8A
BADGE_PIDS = frozenset((
    0x1100,     # Badger 2350
    0x1101,     # Tufty 2350
    0x1102,     # Blinky 2350
))


def find_ports():
    """Return serial ports that are a Badgeware board."""
    from serial.tools import list_ports

    return sorted(port.device for port in list_ports.comports()
                  if port.vid == BADGE_VID and port.pid in BADGE_PIDS)


def _exec(port, script, timeout=30):
    """Run a script on the badge and return what it printed."""
    try:
        return repl.run(port, script, timeout=timeout)
    except repl.Busy:
        raise PortBusy(
            f"{port} is busy. Close Thonny, a serial monitor or whatever else has it open."
        ) from None
    except (repl.ReplError, OSError) as exc:
        raise InstallError(str(exc) or f"the badge on {port} did not answer") from None


# What `os.uname()[4]` reports on the board this app is for. Checked before
# `import badgeware`, which on anything else fails as a traceback.
BOARD = "Tufty 2350"

_BOARD_SCRIPT = (
    "import os\n"
    "print('BOARD', os.uname()[4])\n"
)


def check_board(port):
    """Refuse a board that is not a Tufty, by name, before anything imports badgeware."""
    out = _exec(port, _BOARD_SCRIPT, timeout=10)
    machine = ""
    for line in out.splitlines():
        if line.strip().startswith("BOARD "):
            machine = line.strip()[6:].strip()
    if not machine:
        raise InstallError(f"the board on {port} did not say what it is")
    if BOARD not in machine:
        raise InstallError(
            f"{machine} is not a {BOARD}, so it has no badgeware to install into. "
            f"Connect the badge, or pass --port-dev.")
    return machine


def badge_id(port):
    """Return the badge's uid, the name it signs under."""
    check_board(port)
    out = _exec(port, "import badgeware; print(badge.uid)")
    uid = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not uid:
        raise InstallError(f"could not read the badge uid from {port}")
    return uid


def badge_info(port):
    check_board(port)
    out = _exec(port, (
        "import badgeware, os, sys\n"
        "print(badge.model)\n"
        "print(badge.uid)\n"
        "print('stats' in os.listdir('/system/apps'))\n"
        "print(getattr(sys.implementation, '_mpy', 0))\n"
    ))
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if len(lines) < 4:
        raise InstallError(f"unexpected reply from the badge: {out!r}")
    return {
        "model": lines[-4],
        "uid": lines[-3],
        "app_installed": lines[-2] == "True",
        # The bytecode version this firmware loads. Only the badge reports it.
        "mpy": int(lines[-1] or 0),
    }


def check_precompiled(directory, badge_mpy):
    """Refuse an .mpy build the attached badge cannot load."""
    path = pathlib.Path(directory)
    if not path.is_dir():
        raise InstallError(f"no such directory: {directory}. Build one with "
                           f"ci/build-mpy.sh, or unzip a -mpy release.")
    found = sorted(path.glob("*.mpy"))
    if not found:
        raise InstallError(f"no .mpy files in {directory}")
    versions = set()
    for compiled in found:
        header = compiled.read_bytes()[:4]
        if header[:1] != b"M":
            raise InstallError(f"{compiled.name} is not a .mpy")
        versions.add((header[3] << 8) | header[1])
    if len(versions) > 1:
        raise InstallError(f"mixed bytecode versions in {directory}: {sorted(versions)}")
    stale = _stale_modules(path)
    if stale:
        print(f"warning: {', '.join(stale)} changed since {directory} was built. "
              "Rebuild with ci/build-mpy.sh.")

    built = versions.pop()
    if badge_mpy and built != badge_mpy:
        raise InstallError(
            f"this build is bytecode v{built & 0xFF}.{(built >> 8) & 3} (_mpy {built}) "
            f"but the badge loads v{badge_mpy & 0xFF}.{(badge_mpy >> 8) & 3} "
            f"(_mpy {badge_mpy}). Rebuild against the firmware the badge is running."
        )
    return built, len(found)


def write_state(port, host, http_port, secret, badge_uid, seq=0, server_id=None,
                name=None):
    """Add this host to the app's config in /state, which MicroPython can write."""
    entry = json.dumps({
        "host": host, "port": http_port, "secret": secret,
        "name": name or host, "seq": seq,
    })
    key = server_id or "unknown"
    # Read-modify-write, so a pairing with another host survives and an older flat file
    # is upgraded in place.
    script = (
        "import os, json\n"
        "try:\n"
        "    os.mkdir('/state')\n"
        "except OSError:\n"
        "    pass\n"
        f"path = {STATE_FILE!r}\n"
        "try:\n"
        "    data = json.load(open(path))\n"
        "except (OSError, ValueError):\n"
        "    data = {}\n"
        "hosts = data.get('hosts')\n"
        "if hosts is None:\n"
        "    hosts = {}\n"
        "    if data.get('secret'):\n"
        "        hosts['unknown'] = {'host': data.get('host'),\n"
        "                            'port': data.get('port', 8420),\n"
        "                            'secret': data['secret'],\n"
        "                            'name': data.get('host'),\n"
        "                            'seq': data.get('seq', 0)}\n"
        f"entry = json.loads({entry!r})\n"
        # A stand-in entry with the same secret is this host before it was identified.
        # Folded in at the higher counter, or it reads as a second machine.
        "old = hosts.get('unknown')\n"
        "if old and old.get('secret') == entry['secret']:\n"
        "    entry['seq'] = max(entry.get('seq', 0), old.get('seq', 0))\n"
        "    del hosts['unknown']\n"
        f"hosts[{key!r}] = entry\n"
        f"data['badge_id'] = {badge_uid!r}\n"
        f"data['active'] = {key!r}\n"
        "data['hosts'] = hosts\n"
        "open(path, 'w').write(json.dumps(data))\n"
        "print('wrote', path, len(hosts), 'host(s)')\n"
    )
    out = _exec(port, script)
    if "wrote" not in out:
        raise InstallError(f"could not write {STATE_FILE}: {out.strip()}")
    return STATE_FILE


APP_DIR = f"/system/apps/{APP_NAME}"

# Hashed on the badge, with only the digests crossing the wire. Marker-prefixed, to
# pick the lines out of whatever else the REPL said.
_HASH_SCRIPT = """
import hashlib, os, binascii
def walk(base, prefix=''):
    for name in sorted(os.listdir(base)):
        path = base + '/' + name
        if os.stat(path)[0] & 0x4000:
            walk(path, prefix + name + '/')
            continue
        h = hashlib.sha256()
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(512)
                if not chunk:
                    break
                h.update(chunk)
        print('H', prefix + name, binascii.hexlify(h.digest()).decode())
try:
    walk(%r)
except OSError:
    pass
print('HEND')
"""


def installed_hashes(port):
    """Return the sha256 of every file in the app directory on the badge, by name."""
    out = _exec(port, _HASH_SCRIPT % APP_DIR)
    if "HEND" not in out:
        raise InstallError(f"could not read the installed app: {out.strip()}")
    hashes = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "H":
            hashes[parts[1]] = parts[2]
    return hashes


def desired_hashes(source=None, extra_modules=()):
    """Return the same mapping for what an install would put there."""
    hashes = {}
    for name, path in app_files(source, extra_modules):
        with open(path, "rb") as handle:
            hashes[name] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def app_changes(installed, desired):
    """Return (added, changed, removed) between what is on the badge and what would be."""
    added = sorted(set(desired) - set(installed))
    changed = sorted(name for name in set(desired) & set(installed)
                     if desired[name] != installed[name])
    removed = sorted(name for name in set(installed) - set(desired)
                     if name.endswith(PRUNABLE))
    return added, changed, removed


def secrets_file(volume):
    """Return the badge's secrets.py on its USB volume."""
    for candidate in (os.path.join(volume, "system", "secrets.py"),
                      os.path.join(volume, "secrets.py")):
        if os.path.exists(candidate):
            return candidate
    return None


def wifi_network(port):
    """Return the SSID the badge is set to use, over the REPL. Never the password."""
    try:
        out = _exec(port,
                   "import secrets\n"
                   "print('SSID', getattr(secrets, 'WIFI_SSID', '') or '')\n")
    except InstallError:
        return None
    for line in out.splitlines():
        if line.startswith("SSID "):
            return line[5:].strip() or None
    return None


# What the firmware takes as a WiFi country, mirrored from the badge's own secrets.py.
# One outside this set leaves the radio unable to associate.
REGIONS = ("us", "cuba", "eu", "moldova", "lebanon", "egypt", "chile", "australia", "nz")


def regions_on(volume):
    """Return the regions the badge's secrets.py lists, or the mirrored list."""
    path = secrets_file(volume)
    if path:
        with open(path, encoding="utf-8") as handle:
            match = re.search(r"^\s*REGION\s*=[^\n#]*#\s*Options are ([^\n]+)$",
                              handle.read(), re.M)
        if match:
            found = tuple(word.strip().lower()
                          for word in match.group(1).split(",") if word.strip())
            if found:
                return found
    return REGIONS


def wifi_network_on(volume):
    """Return the SSID secrets.py names on a mounted volume, or None."""
    path = secrets_file(volume)
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return _secret_value(handle.read(), "WIFI_SSID") or None


def wifi_configured(volume):
    """Return whether secrets.py already names a network."""
    return bool(wifi_network_on(volume))


def _secret_value(text, key):
    match = re.search(rf"^\s*{key}\s*=\s*[\"'](.*?)[\"']", text, re.M)
    return match.group(1) if match else ""


def write_secrets(volume, ssid, password, region=None, timezone=None):
    """Set WiFi details in the badge's secrets.py, leaving the rest of the file alone."""
    path = secrets_file(volume)
    if not path:
        raise InstallError(f"no secrets.py on {volume}")
    values = {"WIFI_SSID": ssid, "WIFI_PASSWORD": password}
    if region:
        allowed = regions_on(volume)
        if region.lower() not in allowed:
            raise InstallError(f"{region} is not a WiFi region this badge knows. "
                               f"One of: {', '.join(allowed)}")
        values["REGION"] = region.lower()
    if timezone is not None:
        values["TIMEZONE"] = int(timezone)

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for key, value in values.items():
        # json.dumps, not repr: it matches the double quotes the file ships with.
        literal = json.dumps(value)

        def replace(match, key=key, literal=literal):
            # Keep any trailing comment - REGION's lists the values it accepts.
            comment = (match.group(1) or "").strip()
            return f"{key} = {literal}  {comment}" if comment else f"{key} = {literal}"

        # A function as the replacement, which keeps a backslash in a password from
        # being read as an escape.
        text, count = re.subn(rf"^[ \t]*{key}[ \t]*=[^\n#]*(\s*#[^\n]*)?$",
                              replace, text, count=1, flags=re.M)
        if not count:
            text = text.rstrip("\n") + f"\n{key} = {literal}\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read_state(port):
    out = _exec(port,
               f"try:\n print(open({STATE_FILE!r}).read())\nexcept OSError:\n print('')\n")
    text = out.strip()
    if not text:
        return None
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        return None


def secret_in_state(state, server_id=None):
    """Return the secret a read-back state holds for a host, whatever format it is in."""
    if not state:
        return None
    hosts = state.get("hosts")
    if hosts:
        key = server_id or state.get("active")
        entry = hosts.get(key) or {}
        return entry.get("secret")
    return state.get("secret")


def app_source_dir():
    """Return where the badge app lives."""
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.join(here, "badge_app")
    if not os.path.isfile(os.path.join(app, "__init__.py")):
        raise InstallError(f"cannot find the badge app at {app}")
    return app


def packaged_mpy_dir():
    """Return the precompiled app shipped inside the package, or None."""
    app = pathlib.Path(app_source_dir()) / "mpy"
    if app.is_dir() and any(app.glob("*.mpy")):
        return str(app)
    return None


def choose_app_source(explicit, force_source, badge_mpy):
    """Return which directory to install from, as (source or None for .py, note)."""
    if force_source:
        return None, "installing sources, as asked"
    if explicit:
        built, count = check_precompiled(explicit, badge_mpy)
        return explicit, (f"precompiled from {explicit}: {count} modules, "
                          f"bytecode v{built & 0xFF}.{(built >> 8) & 3}")
    packaged = packaged_mpy_dir()
    if packaged is None:
        return None, "installing sources; no precompiled build in this package"
    stale = _stale_modules(packaged)
    if stale:
        note = (f"installing sources: {', '.join(stale)} changed since the bundled build, "
                "so that bytecode is the older program.")
        current = _current_build_elsewhere()
        if current:
            return None, (f"{note} There is a build matching these sources at {current}: "
                          f"install it with --mpy {current}, or rebuild in place with "
                          "ci/build-mpy.sh")
        return None, f"{note} Rebuild it with ci/build-mpy.sh to install bytecode again"
    try:
        built, count = check_precompiled(packaged, badge_mpy)
    except InstallError as exc:
        return None, f"installing sources instead: {exc}"
    return packaged, (f"precompiled, shipped with the package: {count} modules, "
                      f"bytecode v{built & 0xFF}.{(built >> 8) & 3}")


def enter_mass_storage(port):
    """Ask the badge to present its USB volume."""
    try:
        _exec(port, "import _msc", timeout=10)
    except InstallError:
        # Expected. The board resets mid-command and the REPL stops answering.
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


# Where a build ends up if it was made before the default pointed into the package.
OTHER_BUILD_DIRS = ("build/mpy",)


def _current_build_elsewhere():
    """Return a build directory that does match the sources, if one is lying around."""
    for candidate in OTHER_BUILD_DIRS:
        path = pathlib.Path(candidate)
        if path.is_dir() and not _stale_modules(path):
            try:
                next(path.glob("*.mpy"))
            except StopIteration:
                continue
            return candidate
    return None


def _stale_modules(built_dir):
    """Return the names of modules whose source has changed since the build."""
    try:
        info = json.loads((pathlib.Path(built_dir) / "BUILD_INFO").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        sources = pathlib.Path(app_source_dir())
    except InstallError:
        return []
    stale = []
    for name, digest in sorted(info.get("sources", {}).items()):
        source = sources / name
        if not source.exists():
            continue
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            stale.append(name)
    return stale


# MPY_VERSION and BUILD_INFO are notes from the precompile and stay on the host.
NOT_APP_FILES = ("__pycache__", "MPY_VERSION", "BUILD_INFO", "mpy")


def app_files(source=None, extra_modules=()):
    """Return what an install puts on the badge, as (name relative to the app dir, path)."""
    source = source or app_source_dir()
    files = []
    for name in sorted(os.listdir(source)):
        if name.startswith(".") or name in NOT_APP_FILES:
            continue
        path = os.path.join(source, name)
        if os.path.isdir(path):
            for inner in sorted(os.listdir(path)):
                files.append((f"{name}/{inner}", os.path.join(path, inner)))
            continue
        files.append((name, path))
    owners = {os.path.splitext(name)[0]: "the app" for name, _path in files
              if name.endswith((".py", ".mpy"))}
    for extension, path in extra_modules:
        base = os.path.basename(path)
        if base.endswith(".py"):
            module = base[:-3]
            if owners.get(module, extension) != extension:
                raise InstallError(f"{extension} ships a badge module called {base}, "
                                   f"which {owners[module]} already has")
            owners[module] = extension
        files.append((f"{EXT_DIR}/{extension}/{base}", path))
    return files


def copy_app(volume, source=None, extra_modules=()):
    """Copy the app onto a mounted badge volume, and remove what does not belong."""
    apps = os.path.join(volume, "system", "apps")
    if not os.path.isdir(apps):
        apps = os.path.join(volume, "apps")
    if not os.path.isdir(apps):
        raise InstallError(f"{volume} does not look like a badge volume")

    target = os.path.join(apps, APP_NAME)
    os.makedirs(target, exist_ok=True)
    files = app_files(source, extra_modules)
    for name, path in files:
        destination = os.path.join(target, *name.split("/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        _copy(path, destination)
    removed = prune_app(target, {name for name, _ in files})
    return target, [name for name, _ in files], removed


# How many goes a single file gets, and how long to leave between them.
COPY_TRIES = 3
COPY_WAIT = 0.5


def _copy(path, destination):
    """Copy one file onto the badge, and check it arrived whole."""
    wrong = None
    for attempt in range(COPY_TRIES):
        if attempt:
            time.sleep(COPY_WAIT)
        try:
            shutil.copy2(path, destination)
        except OSError as exc:
            wrong = str(exc)
            continue
        if os.path.getsize(destination) == os.path.getsize(path):
            return
        wrong = "it arrived short"
    raise InstallError(f"could not write {os.path.basename(destination)} "
                       f"to the badge: {wrong}")


# Only these are the installer's to delete. Anything else came from elsewhere and stays.
PRUNABLE = (".py", ".mpy", ".png", ".af")


def prune_app(target, keep):
    """Delete app files that are not part of the install, returning their names."""
    removed = []
    for name in _existing_app_files(target):
        if name in keep or not name.endswith(PRUNABLE):
            continue
        try:
            os.remove(os.path.join(target, *name.split("/")))
        except OSError:
            continue
        removed.append(name)
    return sorted(removed)


def _existing_app_files(target, prefix=""):
    """Return the names, relative to the app directory, of what is on the badge now."""
    found = []
    for name in sorted(os.listdir(target)):
        path = os.path.join(target, name)
        if os.path.isdir(path):
            found.extend(_existing_app_files(path, f"{prefix}{name}/"))
            continue
        found.append(prefix + name)
    return found


def wait_for_port(uid, timeout=40, previous=None):
    """Wait for the badge with this uid to come back after a reset, and return its port."""
    deadline = time.time() + timeout
    others = set()
    while time.time() < deadline:
        for port in sorted(find_ports(), key=lambda candidate: candidate != previous):
            if port in others:
                continue
            try:
                found = badge_id(port)
            except InstallError:
                continue
            if found == uid:
                return port
            others.add(port)
            hard_reset(port, settle=False)
        time.sleep(1.0)
    raise InstallError("the badge did not come back after resetting")


def hard_reset(port, settle=True):
    """Reset the badge so it boots as it normally would. True if it was reset."""
    try:
        repl.reset(port, timeout=10)
    except repl.NotOpened:
        return False
    except (repl.ReplError, OSError):
        # Expected. The port goes away mid-command, and the reply is lost with it.
        pass
    if settle:
        wait_for_enumeration(previous=port)
    return True


def wait_for_enumeration(previous=None, timeout=15):
    """Wait for the badge's port to come back, without talking to it."""
    deadline = time.time() + timeout
    # Wait for the previous path to go, or it reads as the badge already being back.
    if previous:
        while time.time() < deadline and previous in find_ports():
            time.sleep(0.2)
    while time.time() < deadline:
        ports = find_ports()
        if ports:
            return ports[0]
        time.sleep(0.2)
    return None


def eject(volume, port=None):
    """Send the badge back out of disk mode, and take the volume with it."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["diskutil", "eject", volume], capture_output=True, timeout=20)
        elif system == "Linux":
            subprocess.run(["sync"], capture_output=True, timeout=20)
            subprocess.run(["udisksctl", "unmount", "-b", volume],
                           capture_output=True, timeout=20)
        elif system == "Windows" and port:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Write-VolumeCache -DriveLetter {volume[0]}"],
                           capture_output=True, timeout=20, **NO_WINDOW)
            hard_reset(port, settle=False)
    except Exception:
        pass
