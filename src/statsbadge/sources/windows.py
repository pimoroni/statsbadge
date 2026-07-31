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
    def available(cls):
        try:
            _fetch(DEFAULT_URL, timeout=1.0)
            return True
        except Exception:
            # Not running is the common case, and this source is optional.
            return False

    def __init__(self, config):
        super().__init__(config)
        self.url = config.get("lhm_url") or DEFAULT_URL

    def sample(self, frame, dt):
        try:
            tree = _fetch(self.url, timeout=2.0)
        except Exception as exc:
            self.note_fault(exc)
            return
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
        if any(v is not None for v in (gpu_temp, gpu_load, gpu_power)):
            gpus = frame["gpu"] or [{}]
            if gpu_temp is not None:
                gpus[0].setdefault("temp", gpu_temp)
            if gpu_load is not None:
                gpus[0].setdefault("pct", gpu_load)
            if gpu_power is not None:
                gpus[0].setdefault("power", gpu_power)
            frame["gpu"] = gpus

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


def _pick(readings, branch_words, label_words, unit):
    """First reading under a branch whose label matches, in preference order."""
    for want in label_words:
        for path, label, value, got_unit in readings:
            if got_unit != unit:
                continue
            joined = " ".join(path).lower()
            if not any(word in joined for word in branch_words):
                continue
            if want in label.lower():
                return round(value, 1)
    return None
