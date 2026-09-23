"""macOS sources."""

import getpass
import plistlib
import re
import shutil
import subprocess
import sys
import threading

from .base import Source

MB = 1024 * 1024

# The one command this source runs as root, so the sudoers rule a user pastes is the
# argv that will be run. sudoers matches the whole command line.
POWERMETRICS = "/usr/bin/powermetrics"
# Where the rule goes, named in the advice and in the Help tab.
SUDOERS_FILE = "/etc/sudoers.d/statsbadge"
POWERMETRICS_ARGS = ("--samplers", "cpu_power,gpu_power,thermal", "-i", "1000", "-f", "plist")


def powermetrics_argv():
    """Return the command, with this machine's path to it."""
    return [shutil.which("powermetrics") or POWERMETRICS, *POWERMETRICS_ARGS]


# What sudoers reads as syntax inside a command, per sudoers(5). The comma is the one
# that bites: it separates commands in a rule, so `--samplers cpu_power,gpu_power` reads
# as three of them.
SUDOERS_SPECIAL = ("\\", ",", ":", "=")


def sudoers_escaped(word):
    """Escape one argument, as a rule has to spell it."""
    for special in SUDOERS_SPECIAL:
        word = word.replace(special, "\\" + special)
    return word


def sudoers_line():
    """Return the rule that allows exactly that command, for this user and this machine."""
    return "{} ALL=(root) NOPASSWD: {}".format(
        getpass.getuser(), " ".join(sudoers_escaped(word) for word in powermetrics_argv()))


def sudoers_advice():
    """Return what to do about it, ready to paste. One command allowed, not a blanket rule."""
    return (
        "statsbadge: --powermetrics was asked for, but sudo will not run powermetrics\n"
        "  without a password, so there will be no temperatures, fan speeds or package\n"
        "  power. Everything else works as it is. To allow that one command and nothing\n"
        "  else:\n\n"
        "    sudo visudo -f /etc/sudoers.d/statsbadge\n\n"
        "  and put this line in it:\n\n"
        f"    {sudoers_line()}\n"
    )


class MacIOKit(Source):
    """GPU and thermals that need no privileges."""

    name = "macos-iokit"
    provides = ("gpu", "cpu")

    @classmethod
    def available(cls, _config=None):
        return shutil.which("ioreg") is not None

    def __init__(self, config):
        super().__init__(config)
        self._names = {}

    def sample(self, frame, dt):
        # Both readings are subprocesses, so either can time out on a machine busy enough
        # to be worth looking at. The next poll has another go.
        worked = True
        try:
            gpus = self._read_accelerators()
        except Exception as exc:
            self.note_fault(exc)
            worked, gpus = False, []
        if gpus:
            frame["gpu"] = _merge_gpus(frame["gpu"], gpus)
        try:
            self._read_thermal(frame)
        except Exception as exc:
            self.note_fault(exc)
            worked = False
        if worked:
            self.note_ok()

    def _read_accelerators(self):
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator", "-a"],
            capture_output=True, timeout=4,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return []
        entries = plistlib.loads(out.stdout)
        gpus = []
        for entry in entries:
            stats = entry.get("PerformanceStatistics") or {}
            if "Device Utilization %" not in stats:
                continue
            gpu = {
                "name": _gpu_name(entry),
                "pct": float(stats.get("Device Utilization %", 0)),
            }
            in_use = stats.get("In use system memory")
            allocated = stats.get("Alloc system memory")
            if in_use is not None:
                gpu["mem_used_mb"] = round(in_use / MB)
            if in_use is not None and allocated:
                gpu["mem_pct"] = round(100.0 * in_use / allocated, 1)
            gpus.append(gpu)
        return gpus

    def _read_thermal(self, frame):
        """Read pmset for thermal pressure and any speed limit, both without sudo."""
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode != 0:
            return
        match = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out.stdout)
        if match:
            limit = int(match.group(1))
            if limit < 100:
                frame["cpu"]["throttle_pct"] = limit


class MacPowermetrics(Source):
    """Package power, GPU power and GPU clock, via a root powermetrics."""

    name = "macos-powermetrics"
    provides = ("gpu", "power")

    @classmethod
    def available(cls, _config=None):
        return shutil.which("powermetrics") is not None

    def __init__(self, config):
        super().__init__(config)
        # Three answers, not two: asked for out loud, left alone, or tried quietly.
        wanted = config.get("powermetrics")
        self._asked = wanted is True
        self._enabled = wanted is not False
        self._proc = None
        self._thread = None
        self._latest = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        if not self._enabled:
            return
        if not self.permitted():
            # Only where it was asked for. Tried by default, a refusal is the ordinary
            # state of a Mac; the Help tab is where the rule to allow it is written out.
            if self._asked:
                print(sudoers_advice(), file=sys.stderr)
                self.note_fault(RuntimeError(
                    "sudo will not run powermetrics without a password: add a rule to "
                    "/etc/sudoers.d/statsbadge"))
            self._enabled = False
            return
        self._proc = subprocess.Popen(
            ["sudo", "-n", *powermetrics_argv()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    @staticmethod
    def permitted():
        """Return whether sudo will run *this* command without a password."""
        try:
            return subprocess.run(["sudo", "-n", "-l", *powermetrics_argv()],
                                  capture_output=True, timeout=3).returncode == 0
        except Exception:
            return False

    def _pump(self):
        """Read the plist stream, splitting on the document header.

        powermetrics emits one plist per sample, back to back.
        """
        buf = b""
        head = b"<?xml"
        while not self._stop.is_set() and self._proc and self._proc.stdout:
            chunk = self._proc.stdout.read(8192)
            if not chunk:
                # Nothing more coming. A rule that is permitted but does not match the
                # argv lands here and not in the check above, so its parting words
                # become the fault.
                self._note_exit()
                break
            buf += chunk.replace(b"\x00", b"")
            while True:
                start = buf.find(head)
                if start < 0:
                    break
                nxt = buf.find(head, start + len(head))
                if nxt < 0:
                    break
                doc, buf = buf[start:nxt], buf[nxt:]
                try:
                    sample = plistlib.loads(doc)
                except Exception:
                    continue
                with self._lock:
                    self._latest = sample

    def _note_exit(self):
        """Record why the reader stopped, if it stopped badly."""
        if self._stop.is_set() or self._proc is None:
            return
        try:
            code = self._proc.poll()
            said = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
        except Exception:
            return
        if code:
            self.note_fault(RuntimeError(said.splitlines()[0] if said
                                         else f"powermetrics exited {code}"))
            self._enabled = False

    def sample(self, frame, dt):
        if not self._enabled:
            return
        with self._lock:
            latest = dict(self._latest)
        if not latest:
            return
        processor = latest.get("processor") or {}
        package_mw = processor.get("combined_power")
        if package_mw is not None:
            frame["power"]["package_w"] = round(float(package_mw) / 1000, 1)
        # MHz, whatever the name says. A CPU cluster's freq_hz is in hertz.
        gpu_mhz = (latest.get("gpu") or {}).get("freq_hz")
        gpu_mw = processor.get("gpu_power")
        if gpu_mhz is not None or gpu_mw is not None:
            gpus = frame["gpu"] or [{}]
            if gpu_mhz is not None:
                gpus[0]["clock"] = round(float(gpu_mhz))
            if gpu_mw is not None:
                gpus[0]["power"] = round(float(gpu_mw) / 1000, 1)
            frame["gpu"] = gpus


def _gpu_name(entry):
    for key in ("model", "IOGVAName", "CFBundleIdentifier", "IOClass"):
        value = entry.get(key)
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace").rstrip("\x00")
        if value:
            return str(value)
    return "GPU"


def _merge_gpus(existing, found):
    """Fill gaps in already-collected GPUs without replacing them."""
    if not existing:
        return found
    for i, gpu in enumerate(found):
        if i < len(existing):
            for key, value in gpu.items():
                existing[i].setdefault(key, value)
        else:
            existing.append(gpu)
    return existing
