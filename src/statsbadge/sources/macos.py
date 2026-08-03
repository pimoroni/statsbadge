"""macOS sources.

Apple exposes very little without privileges. What is readable as a normal user:

- GPU utilisation and memory, from IOAccelerator's PerformanceStatistics via ioreg.
- Thermal pressure and any CPU speed limit, from pmset.

Die temperatures, fan RPM and package power all live behind the SMC or
powermetrics, which needs root. `MacPowermetrics` covers that and is opt-in, so the
default install asks for no password and simply reports no temperature.

Asked for with `--powermetrics` and not permitted, it prints the sudoers rule to add and
carries on without those fields: a flag that quietly does nothing is worse than no flag.
"""

import getpass
import plistlib
import re
import shutil
import subprocess
import sys
import threading

from .base import Source

MB = 1024 * 1024

# The one command this source runs as root, as one list, so the sudoers rule a user is told to
# paste is the argv that will actually be run. sudoers matches the whole command line, so a rule
# written for anything else is a rule that does not work.
POWERMETRICS = "/usr/bin/powermetrics"
POWERMETRICS_ARGS = ("--samplers", "cpu_power,gpu_power,thermal", "-i", "1000", "-f", "plist")


def powermetrics_argv():
    """The command, with this machine's own path to it."""
    return [shutil.which("powermetrics") or POWERMETRICS, *POWERMETRICS_ARGS]


def sudoers_line():
    """The rule that allows that command and nothing else, for this user and this machine."""
    return "{} ALL=(root) NOPASSWD: {}".format(getpass.getuser(),
                                               " ".join(powermetrics_argv()))


def sudoers_advice():
    """What to do about it, ready to paste. One command allowed, not a blanket rule."""
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
    def available(cls):
        return shutil.which("ioreg") is not None

    def __init__(self, config):
        super().__init__(config)
        self._names = {}

    def sample(self, frame, dt):
        try:
            gpus = self._read_accelerators()
        except Exception as exc:
            self.note_fault(exc)
            gpus = []
        if gpus:
            frame["gpu"] = _merge_gpus(frame["gpu"], gpus)
        try:
            self._read_thermal(frame)
        except Exception as exc:
            self.note_fault(exc)

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
        """pmset reports thermal pressure and any speed limit, both without sudo."""
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
    """Package power, GPU power and die temperatures, via a root powermetrics.

    Opt-in: it needs to run as root, so it is only started when the config asks and sudoers
    permits that one command without a password. One long-lived process sampling on an
    interval, read on a thread, because spawning powermetrics per frame costs about a second.
    """

    name = "macos-powermetrics"
    provides = ("cpu", "gpu", "power", "fans")

    @classmethod
    def available(cls):
        return shutil.which("powermetrics") is not None

    def __init__(self, config):
        super().__init__(config)
        self._enabled = bool(config.get("powermetrics"))
        self._proc = None
        self._thread = None
        self._latest = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        if not self._enabled:
            return
        if not self._permitted():
            # The advice goes to the terminal the flag was typed at; the fault is one line,
            # being what the config UI and `probe` show.
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
    def _permitted():
        """Whether sudo will run *this* command without a password.

        Asked of the command itself rather than of sudo in general. A rule that allows
        powermetrics and nothing else - which is the rule to write - does not allow `sudo -n
        true`, so testing with that would refuse the very setup worth having.
        """
        try:
            return subprocess.run(["sudo", "-n", "-l", *powermetrics_argv()],
                                  capture_output=True, timeout=3).returncode == 0
        except Exception:
            return False

    def _pump(self):
        """Read the plist stream. powermetrics emits one plist per sample, back to
        back, so split on the document header rather than trying to stream-parse."""
        buf = b""
        head = b"<?xml"
        while not self._stop.is_set() and self._proc and self._proc.stdout:
            chunk = self._proc.stdout.read(8192)
            if not chunk:
                # Nothing more coming. A rule that is permitted but does not match the argv, or
                # a powermetrics that refuses for its own reasons, ends up here rather than in
                # the check above, so what it said on the way out becomes the fault.
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
        """Record why the reader stopped, if it stopped badly.

        Not while shutting down: terminate() is how this is meant to end.
        """
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
        watts = latest.get("package_W") or latest.get("Package_W")
        if watts:
            frame["power"]["package_w"] = round(float(watts), 1)
        gpu_stats = latest.get("gpu") or {}
        if gpu_stats:
            gpus = frame["gpu"] or [{}]
            if "freq_hz" in gpu_stats:
                gpus[0]["clock"] = round(float(gpu_stats["freq_hz"]) / 1e6)
            gpu_watts = gpu_stats.get("gpu_energy") or latest.get("GPU_W")
            if gpu_watts:
                gpus[0]["power"] = round(float(gpu_watts), 1)
            frame["gpu"] = gpus
        for entry in latest.get("thermal", {}).get("sensors", []) or []:
            name = str(entry.get("name", "")).lower()
            value = entry.get("value")
            if value is None:
                continue
            if "cpu" in name and "cpu" not in frame:
                frame["cpu"].setdefault("temp", round(float(value), 1))


def _gpu_name(entry):
    for key in ("model", "IOGVAName", "CFBundleIdentifier", "IOClass"):
        value = entry.get(key)
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace").rstrip("\x00")
        if value:
            return str(value)
    return "GPU"


def _merge_gpus(existing, found):
    """Fill gaps in already-collected GPUs rather than replacing them, so two
    sources describing the same card produce one entry."""
    if not existing:
        return found
    for i, gpu in enumerate(found):
        if i < len(existing):
            for key, value in gpu.items():
                existing[i].setdefault(key, value)
        else:
            existing.append(gpu)
    return existing
