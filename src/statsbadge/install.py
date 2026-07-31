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
import hashlib
import json
import os
import re
import pathlib
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
        "print(getattr(sys.implementation, '_mpy', 0))\n"
    ))
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if len(lines) < 4:
        raise InstallError(f"unexpected reply from the badge: {out!r}")
    return {
        "model": lines[-4],
        "uid": lines[-3],
        "app_installed": lines[-2] == "True",
        # The bytecode version this firmware will load. Only the badge knows it, which
        # is why a precompiled app is checked here and not only where it was built.
        "mpy": int(lines[-1] or 0),
    }


def check_precompiled(directory, badge_mpy):
    """Refuse a .mpy build the attached badge cannot load.

    A wrong bytecode version does not fail at install: it fails at import, on the badge,
    after the launcher has already started the app, as a crash dialog with no clue in it.
    The header is 'M', version, reserved, flags, and (flags << 8) | version is exactly
    what the firmware reports as sys.implementation._mpy.
    """
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


# -- credentials ------------------------------------------------------------

def write_state(port, host, http_port, secret, badge_uid, seq=0, server_id=None,
                name=None):
    """Add this host to the app's config in /state, which MicroPython can write.

    Credentials are keyed on the server's id rather than its address, so the badge can
    follow a host that changes address, and this *merges* rather than replacing: a
    badge paired with two machines keeps both. `seq` has to match the counter the
    server recorded, or the badge's first request lands outside the replay window.
    """
    entry = json.dumps({
        "host": host, "port": http_port, "secret": secret,
        "name": name or host, "seq": seq,
    })
    key = server_id or "unknown"
    # Read-modify-write on the badge, so an existing pairing with another host
    # survives and an older flat file is upgraded in place.
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
        # A placeholder entry holding the same secret is this host before it was
        # identified, so fold it in and keep the higher counter rather than leaving a
        # duplicate that would look like a second machine.
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
    out = _run(port, "exec", script)
    if "wrote" not in out:
        raise InstallError(f"could not write {STATE_FILE}: {out.strip()}")
    return STATE_FILE


APP_DIR = f"/system/apps/{APP_NAME}"

# Hashing on the badge rather than reading the files back over the REPL: it does the
# whole app directory in 45ms and only the digests cross the wire. Marker-prefixed
# because the reply is many lines and has to be picked out of whatever else the REPL
# said.
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
    """sha256 of every file in the app directory on the badge, by relative name.

    Empty if the app is not installed.
    """
    out = _run(port, "exec", _HASH_SCRIPT % APP_DIR)
    if "HEND" not in out:
        raise InstallError(f"could not read the installed app: {out.strip()}")
    hashes = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "H":
            hashes[parts[1]] = parts[2]
    return hashes


def desired_hashes(source=None, extra_modules=()):
    """The same mapping for what an install would put there."""
    hashes = {}
    for name, path in app_files(source, extra_modules):
        with open(path, "rb") as handle:
            hashes[name] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def app_changes(installed, desired):
    """(added, changed, removed) between what is on the badge and what would be.

    Only prunable names count as removed: a file the installer does not own is not a
    reason to reset the badge.
    """
    added = sorted(set(desired) - set(installed))
    changed = sorted(name for name in set(desired) & set(installed)
                     if desired[name] != installed[name])
    removed = sorted(name for name in set(installed) - set(desired)
                     if name.endswith(PRUNABLE))
    return added, changed, removed


def secrets_file(volume):
    """The badge's secrets.py on its USB volume."""
    for candidate in (os.path.join(volume, "system", "secrets.py"),
                      os.path.join(volume, "secrets.py")):
        if os.path.exists(candidate):
            return candidate
    return None


def wifi_network(port):
    """The SSID the badge is set to use, over the REPL. Never the password."""
    try:
        out = _run(port, "exec",
                   "import secrets\n"
                   "print('SSID', getattr(secrets, 'WIFI_SSID', '') or '')\n")
    except InstallError:
        return None
    for line in out.splitlines():
        if line.startswith("SSID "):
            return line[5:].strip() or None
    return None


def wifi_configured(volume):
    """Whether secrets.py already names a network."""
    path = secrets_file(volume)
    if not path:
        return False
    with open(path) as handle:
        return bool(_secret_value(handle.read(), "WIFI_SSID"))


def _secret_value(text, key):
    match = re.search(rf"^\s*{key}\s*=\s*[\"'](.*?)[\"']", text, re.M)
    return match.group(1) if match else ""


def write_secrets(volume, ssid, password, region=None, timezone=None):
    """Set WiFi details in the badge's secrets.py, leaving the rest of the file alone.

    This is the file the badge's own error message tells people to edit, so it is the one
    to change; a /secrets.py on the internal filesystem would take precedence over it and
    silently defeat that edit.
    """
    path = secrets_file(volume)
    if not path:
        raise InstallError(f"no secrets.py on {volume}")
    values = {"WIFI_SSID": ssid, "WIFI_PASSWORD": password}
    if region:
        values["REGION"] = region
    if timezone is not None:
        values["TIMEZONE"] = int(timezone)

    with open(path) as handle:
        text = handle.read()
    for key, value in values.items():
        # json.dumps, not repr: a valid Python literal either way, and it matches the
        # double quotes the file ships with.
        literal = json.dumps(value)

        def replace(match, key=key, literal=literal):
            # Keep any trailing comment - REGION's lists the values it accepts.
            comment = (match.group(1) or "").strip()
            return f"{key} = {literal}  {comment}" if comment else f"{key} = {literal}"

        # A function as the replacement, so a backslash in a password is not read as an
        # escape and written out broken.
        text, count = re.subn(rf"^[ \t]*{key}[ \t]*=[^\n#]*(\s*#[^\n]*)?$",
                              replace, text, count=1, flags=re.M)
        if not count:
            text = text.rstrip("\n") + f"\n{key} = {literal}\n"
    with open(path, "w") as handle:
        handle.write(text)
    return path


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


def secret_in_state(state, server_id=None):
    """The secret a read-back state holds for a host, whatever format it is in."""
    if not state:
        return None
    hosts = state.get("hosts")
    if hosts:
        key = server_id or state.get("active")
        entry = hosts.get(key) or {}
        return entry.get("secret")
    return state.get("secret")


# -- the app itself ---------------------------------------------------------

def app_source_dir():
    """Where the badge app lives.

    Inside the package, so a checkout and an installed wheel are the same path: uv_build
    ships everything under the module directory, icon included.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.join(here, "badge_app")
    if not os.path.isfile(os.path.join(app, "__init__.py")):
        raise InstallError(f"cannot find the badge app at {app}")
    return app


def packaged_mpy_dir():
    """The precompiled app shipped inside the package, or None.

    CI compiles into badge_app/mpy/ before the wheel is built, so a pip install carries
    both: the .py sources, which load on any firmware, and bytecode for the firmware
    current at release. A local `uv build` has no mpy-cross and produces neither.
    """
    app = pathlib.Path(app_source_dir()) / "mpy"
    if app.is_dir() and any(app.glob("*.mpy")):
        return str(app)
    return None


def choose_app_source(explicit, force_source, badge_mpy):
    """Which directory to install from. Returns (source or None for .py, note).

    Bytecode only loads on the firmware it was built for, so a packaged build that does
    not match the badge is skipped rather than refused - the sources still work.
    """
    if force_source:
        return None, "installing sources, as asked"
    if explicit:
        built, count = check_precompiled(explicit, badge_mpy)
        return explicit, (f"precompiled from {explicit}: {count} modules, "
                          f"bytecode v{built & 0xFF}.{(built >> 8) & 3}")
    packaged = packaged_mpy_dir()
    if packaged is None:
        return None, "installing sources; no precompiled build in this package"
    try:
        built, count = check_precompiled(packaged, badge_mpy)
    except InstallError as exc:
        return None, f"installing sources instead: {exc}"
    return packaged, (f"precompiled, shipped with the package: {count} modules, "
                      f"bytecode v{built & 0xFF}.{(built >> 8) & 3}")


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


def _stale_modules(built_dir):
    """Names of modules whose source has changed since the build.

    By content, not mtime: a wheel's files all carry extraction-time stamps, so an mtime
    comparison there is noise. A build with no BUILD_INFO cannot be checked.
    """
    try:
        info = json.loads((built_dir / "BUILD_INFO").read_text())
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


# MPY_VERSION and BUILD_INFO are notes from the precompile, not something the badge
# needs. `mpy` is the built copy sitting inside the source directory.
NOT_APP_FILES = ("__pycache__", "MPY_VERSION", "BUILD_INFO", "mpy")


def app_files(source=None, extra_modules=()):
    """What an install puts on the badge, as (name relative to the app dir, path).

    The one place that decides which files belong, so the copy, the change check and
    the prune cannot disagree about it.
    """
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
    # `ext`, not `pages`: see load_extensions() in the app - a `pages` directory would
    # shadow the app's pages.py module.
    for _name, path in extra_modules:
        files.append((f"ext/{os.path.basename(path)}", path))
    return files


def copy_app(volume, source=None, extra_modules=()):
    """Copy the app onto a mounted badge volume, and remove what no longer belongs.

    `source` may be a precompiled .mpy directory instead of the package's own, which is
    how the CI-built bytecode gets installed.
    """
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
        shutil.copy2(path, destination)
    removed = prune_app(target, {name for name, _ in files})
    return target, [name for name, _ in files], removed


# Only these are the installer's to delete. Anything else in the app directory was put
# there by someone, and a install that eats it is worse than one that leaves litter.
PRUNABLE = (".py", ".mpy", ".png")


def prune_app(target, keep):
    """Delete app files that are no longer part of the install. Returns their names.

    A .py left beside a .mpy takes precedence over it, so a source install followed by
    a bytecode one silently undoes the precompile unless the old sources go. Extension
    modules in ext/ have the same problem: one left behind keeps registering its page.
    """
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


def _existing_app_files(target):
    """Names, relative to the app directory, of what is on the badge now."""
    found = []
    for name in sorted(os.listdir(target)):
        path = os.path.join(target, name)
        if os.path.isdir(path):
            found.extend(f"{name}/{inner}" for inner in sorted(os.listdir(path)))
            continue
        found.append(name)
    return found


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


def hard_reset(port, settle=True):
    """Reset the badge so it boots as it normally would.

    Anything that talks over the REPL interrupts whatever the badge was running to get
    there, which leaves it sitting at a bare prompt on a blank screen. A reset runs
    `main.py` again, so the badge starts whatever it is set up to start.
    """
    try:
        _run(port, "reset", timeout=10)
    except (InstallError, subprocess.TimeoutExpired):
        # Expected: the port goes away mid-command and mpremote never gets a reply.
        pass
    if not settle:
        return None
    return wait_for_enumeration(previous=port)


def wait_for_enumeration(previous=None, timeout=15):
    """Wait for the badge's port to come back, without talking to it.

    wait_for_port confirms the REPL answers, which is right when something is about to
    be written but not after a reset meant to hand the badge back: the check would
    interrupt whatever has just started. So this only watches enumeration.
    """
    deadline = time.time() + timeout
    # Wait for the old path to go first, or a port still present from before the reset
    # would look like the badge already being back.
    if previous:
        while time.time() < deadline and previous in find_ports():
            time.sleep(0.2)
    while time.time() < deadline:
        ports = find_ports()
        if ports:
            return ports[0]
        time.sleep(0.2)
    return None


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
