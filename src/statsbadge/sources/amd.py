"""AMD GPUs on Linux, from amdgpu's sysfs nodes.

No extra package needed: the driver publishes utilisation, VRAM, temperature and
power under /sys/class/drm/card*/device. On Windows an AMD card is read through
LibreHardwareMonitor instead.
"""

import glob
import os

from .base import Source

MB = 1024 * 1024


class AmdGpu(Source):
    name = "amdgpu"
    provides = ("gpu",)

    @classmethod
    def available(cls, _config=None):
        return bool(_cards())

    def sample(self, frame, dt):
        gpus = []
        for device in _cards():
            gpu = {"name": _name(device)}
            busy = _read_int(os.path.join(device, "gpu_busy_percent"))
            if busy is not None:
                gpu["pct"] = float(busy)
            used = _read_int(os.path.join(device, "mem_info_vram_used"))
            total = _read_int(os.path.join(device, "mem_info_vram_total"))
            if used is not None:
                gpu["mem_used_mb"] = round(used / MB)
            if used is not None and total:
                gpu["mem_pct"] = round(100.0 * used / total, 1)
            hwmon = _hwmon(device)
            if hwmon:
                temp = _read_int(os.path.join(hwmon, "temp1_input"))
                if temp is not None:
                    gpu["temp"] = round(temp / 1000.0, 1)
                power = _read_int(os.path.join(hwmon, "power1_average"))
                if power is not None:
                    gpu["power"] = round(power / 1e6, 1)
                clock = _read_int(os.path.join(hwmon, "freq1_input"))
                if clock is not None:
                    gpu["clock"] = round(clock / 1e6)
                pwm = _read_int(os.path.join(hwmon, "pwm1"))
                if pwm is not None:
                    gpu["fan_pct"] = round(100.0 * pwm / 255.0, 1)
            gpus.append(gpu)
        if gpus:
            frame["gpu"] = _merge(frame["gpu"], gpus)


def _cards():
    return [
        path for path in sorted(glob.glob("/sys/class/drm/card[0-9]/device"))
        if os.path.exists(os.path.join(path, "gpu_busy_percent"))
    ]


def _hwmon(device):
    found = sorted(glob.glob(os.path.join(device, "hwmon", "hwmon*")))
    return found[0] if found else None


def _name(device):
    # The marketing name needs a PCI id database; the driver's product name will do.
    for candidate in ("product_name", "vbios_version"):
        text = _read_text(os.path.join(device, candidate))
        if text:
            return text
    return "AMD GPU"


def _read_int(path):
    text = _read_text(path)
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _merge(existing, found):
    if not existing:
        return found
    for i, gpu in enumerate(found):
        if i < len(existing):
            for key, value in gpu.items():
                existing[i].setdefault(key, value)
        else:
            existing.append(gpu)
    return existing
