"""Windows sensors via LibreHardwareMonitor's web server.

Windows gives a normal process no temperatures, fan speeds or package power: reading
them means a driver. LibreHardwareMonitor already ships one, and its "Remote Web
Server" publishes everything as JSON, so this reads that rather than shipping a
driver of its own.

Run LibreHardwareMonitor, then Options -> Remote Web Server -> Run, and point
`--lhm-url` at it if it is not on the default port 8085.
"""

import json
import urllib.error
import urllib.request

from .base import Source

DEFAULT_URL = "http://127.0.0.1:8085/data.json"


class LibreHardwareMonitor(Source):
    name = "librehardwaremonitor"
    provides = ("cpu", "gpu", "fans", "power")

    @classmethod
    def available(cls, config=None):
        # The address it was given, and not the usual one: a server on another port is
        # exactly the case the setting exists for.
        try:
            _fetch((config or {}).get("lhm_url") or DEFAULT_URL, timeout=1.0)
            return True
        except Exception:
            # Not running is the common case, and this source is optional.
            return False

    def __init__(self, config):
        super().__init__(config)
        self.url = config.get("lhm_url") or DEFAULT_URL

    def reconfigure(self, config):
        """Take a URL typed in the browser without a restart."""
        self.url = config.get("lhm_url") or DEFAULT_URL

    def sample(self, frame, dt):
        try:
            tree = _fetch(self.url, timeout=2.0)
        except Exception as exc:
            # LibreHardwareMonitor being restarted is a gap, not a lasting fault.
            self.note_fault(exc)
            return
        self.note_ok()
        readings = list(_walk(tree, []))

        cpu_temp = _pick(readings, ("cpu",), ("package", "tctl", "tdie", "core average"), "°C")
        if cpu_temp is not None:
            frame["cpu"].setdefault("temp", cpu_temp)
        cpu_power = _pick(readings, ("cpu",), ("package", "total"), "W")
        if cpu_power is not None:
            frame["power"].setdefault("package_w", cpu_power)

        gpu_temp = _pick(readings, ("gpu",), ("core",), "°C")
        gpu_load = _pick(readings, ("gpu",), ("core",), "%")
        gpu_power = _pick(readings, ("gpu",), ("package", "total"), "W")
        # VRAM comes as three figures in MB. The frame carries what is used and how
        # full that leaves it.
        vram_used = _pick(readings, ("gpu",), ("memory used",), "MB")
        vram_total = _pick(readings, ("gpu",), ("memory total",), "MB")
        if any(v is not None for v in (gpu_temp, gpu_load, gpu_power, vram_used)):
            gpus = frame["gpu"] or [{}]
            if gpu_temp is not None:
                gpus[0].setdefault("temp", gpu_temp)
            if gpu_load is not None:
                gpus[0].setdefault("pct", gpu_load)
            if gpu_power is not None:
                gpus[0].setdefault("power", gpu_power)
            if vram_used is not None:
                gpus[0].setdefault("mem_used_mb", round(vram_used))
                if vram_total:
                    gpus[0].setdefault("mem_pct", round(vram_used / vram_total * 100, 1))
            frame["gpu"] = gpus

        # Every voltage the CPU reports, in the order it reports them, with the labels
        # for the lanes beside them. A bar each is what these are for.
        volts = [(_lane(path[-1]), round(value, 3))
                 for path, _label, value, unit in readings
                 if unit == "V" and _under(path, ("cpu",))]
        if volts:
            frame["cpu"].setdefault("volts", [value for _name, value in volts])
            frame["cpu"].setdefault("volts_names", [name for name, _value in volts])

        fans = [
            {"name": path[-1], "rpm": int(value)}
            for path, label, value, unit in readings
            if unit == "RPM" and value
        ]
        if fans and not frame["fans"]:
            frame["fans"] = fans


def _fetch(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _walk(node, path):
    """Flatten LHM's nested tree into (path, label, value, unit) readings."""
    label = str(node.get("Text", ""))
    here = path + [label] if label else path
    raw = node.get("Value")
    if raw:
        parsed = _parse(raw)
        if parsed is not None:
            yield here, label, parsed[0], parsed[1]
    for child in node.get("Children", []) or []:
        yield from _walk(child, here)


def _parse(raw):
    """LHM formats values as "45.0 °C" or "1,234 RPM"."""
    text = str(raw).strip().replace(",", "")
    parts = text.split(" ", 1)
    try:
        value = float(parts[0])
    except ValueError:
        return None
    return value, (parts[1].strip() if len(parts) > 1 else "")


def _under(path, branch_words):
    """Whether a reading sits under one of these branches."""
    joined = " ".join(path).lower()
    return any(word in joined for word in branch_words)


def _lane(label):
    """A voltage's label, short enough for a bar: "CPU Core #1" is "Core #1"."""
    text = str(label).strip()
    return text[4:].strip() if text.lower().startswith("cpu ") else text


def _pick(readings, branch_words, label_words, unit):
    """First reading under a branch whose label matches, in preference order."""
    for want in label_words:
        for path, label, value, got_unit in readings:
            if got_unit != unit:
                continue
            if not _under(path, branch_words):
                continue
            if want in label.lower():
                return round(value, 1)
    return None
