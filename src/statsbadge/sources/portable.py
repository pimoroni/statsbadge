"""Everything psutil can answer on any platform. CPU, memory, disk, network and battery.

Rates are computed here from counter deltas, because the badge should not have to
remember the previous frame to draw a network graph.
"""

import os
import platform
import socket
import time

import psutil

from .base import Source

MB = 1024 * 1024


class Portable(Source):
    name = "psutil"
    provides = ("cpu", "mem", "disk", "net", "power", "sys")

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._net_prev = None
        self._disk_prev = None
        self._boot = psutil.boot_time()
        self._host = socket.gethostname().split(".")[0]
        self._cpu_name = _cpu_name()
        self._iface = config.get("iface")
        # cpu_percent needs a prior call to have an interval to compare against.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    def sample(self, frame, dt):
        cpu = frame["cpu"]
        cpu["pct"] = round(psutil.cpu_percent(interval=None), 1)
        cpu["cores"] = [round(v, 1) for v in psutil.cpu_percent(interval=None, percpu=True)]
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                cpu["freq"] = round(freq.current)
        except Exception:
            pass
        if hasattr(psutil, "getloadavg"):
            try:
                cpu["load"] = [round(v, 2) for v in psutil.getloadavg()]
            except OSError:
                pass
        try:
            cpu["procs"] = len(psutil.pids())
        except Exception:
            pass

        vm = psutil.virtual_memory()
        mem = frame["mem"]
        mem["pct"] = round(vm.percent, 1)
        mem["used_mb"] = round((vm.total - vm.available) / MB)
        mem["total_mb"] = round(vm.total / MB)
        try:
            sw = psutil.swap_memory()
            mem["swap_pct"] = round(sw.percent, 1)
            mem["swap_used_mb"] = round(sw.used / MB)
        except Exception:
            pass

        disk = frame["disk"]
        try:
            usage = psutil.disk_usage(self.config.get("disk_path") or default_disk())
            disk["pct"] = round(usage.percent, 1)
            disk["used_mb"] = round(usage.used / MB)
            disk["total_mb"] = round(usage.total / MB)
        except Exception:
            pass
        try:
            io = psutil.disk_io_counters()
            if io:
                if self._disk_prev and dt > 0:
                    disk["read_bps"] = _rate(io.read_bytes, self._disk_prev.read_bytes, dt)
                    disk["write_bps"] = _rate(io.write_bytes, self._disk_prev.write_bytes, dt)
                self._disk_prev = io
        except Exception:
            pass

        self._sample_net(frame["net"], dt)

        try:
            bat = psutil.sensors_battery()
        except Exception:
            bat = None
        if bat is not None:
            power = frame["power"]
            power["battery_pct"] = round(bat.percent)
            power["charging"] = bool(bat.power_plugged)
            if bat.secsleft is not None and bat.secsleft >= 0:
                power["secs_left"] = int(bat.secsleft)

        frame["sys"].update(
            host=self._host,
            os=f"{platform.system()} {platform.release()}",
            arch=platform.machine(),
            uptime_s=int(time.time() - self._boot),
            cpu_name=self._cpu_name,
        )

    def _sample_net(self, net, dt):
        try:
            if self._iface:
                counters = psutil.net_io_counters(pernic=True).get(self._iface)
                iface = self._iface
            else:
                counters, iface = _busiest_iface()
        except Exception:
            return
        if counters is None:
            return
        net["iface"] = iface
        net["up_total_mb"] = round(counters.bytes_sent / MB)
        net["down_total_mb"] = round(counters.bytes_recv / MB)
        prev = self._net_prev
        if prev and prev[0] == iface and dt > 0:
            net["up_bps"] = _rate(counters.bytes_sent, prev[1].bytes_sent, dt)
            net["down_bps"] = _rate(counters.bytes_recv, prev[1].bytes_recv, dt)
        self._net_prev = (iface, counters)


def default_disk():
    """The filesystem "how full is my disk" means.

    On macOS that is not "/". The root is a sealed, read-only system volume, and shares
    an APFS container with the data volume, so it reports the system's 12G against the
    container's size: 9% on a disk that is 86% full.

    Both volumes report the container's free space, so the data volume is the one whose
    `used` is the answer.
    """
    if platform.system() == "Darwin":
        for candidate in ("/System/Volumes/Data", "/"):
            if os.path.isdir(candidate):
                return candidate
    return "/"


def _rate(now, before, dt):
    """Bytes per second, clamped at zero so a counter reset reads as idle."""
    delta = now - before
    if delta < 0:
        return 0
    return round(delta / dt)


def _busiest_iface():
    """The interface with the most traffic that is up and not loopback.

    Guessing beats making the user name their interface, and on a laptop that moves
    between wifi and ethernet the guess is the one they want.
    """
    stats = psutil.net_if_stats()
    best = None
    for name, counters in psutil.net_io_counters(pernic=True).items():
        info = stats.get(name)
        if not info or not info.isup:
            continue
        if name.startswith(("lo", "utun", "gif", "stf", "awdl", "llw", "bridge", "veth", "docker")):
            continue
        total = counters.bytes_sent + counters.bytes_recv
        if best is None or total > best[0]:
            best = (total, name, counters)
    if best is None:
        return None, None
    return best[2], best[1]


def _cpu_name():
    system = platform.system()
    try:
        if system == "Darwin":
            import subprocess
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                 capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        elif system == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()
