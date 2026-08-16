"""Linux sensors, straight out of sysfs.

psutil's sensors_temperatures already reads hwmon, so this fills the gaps it leaves:
picking a sensible "the CPU temperature" out of a pile of labelled sensors, and fan
RPM.
"""

import glob
import os

import psutil

from .base import Source

# Labels that mean "the package", best first. A machine offers several, and the one to
# report is the hottest meaningful aggregate.
CPU_LABELS = (
    "package id 0", "tctl", "tdie", "cpu", "k10temp", "coretemp",
    "soc_thermal", "cpu_thermal", "acpitz",
)


class LinuxHwmon(Source):
    name = "linux-hwmon"
    provides = ("cpu", "fans")

    @classmethod
    def available(cls, _config=None):
        return os.path.isdir("/sys/class/hwmon")

    def sample(self, frame, dt):
        worked = True
        try:
            temp = self._cpu_temp()
            if temp is not None:
                frame["cpu"].setdefault("temp", temp)
        except Exception as exc:
            self.note_fault(exc)
            worked = False
        try:
            fans = self._fans()
            if fans:
                frame["fans"] = frame["fans"] or fans
        except Exception as exc:
            self.note_fault(exc)
            worked = False
        if worked:
            self.note_ok()

    def _cpu_temp(self):
        try:
            groups = psutil.sensors_temperatures()
        except Exception:
            groups = {}
        best = None
        for chip, entries in groups.items():
            for entry in entries:
                label = (entry.label or chip or "").lower()
                if entry.current is None:
                    continue
                rank = next(
                    (i for i, want in enumerate(CPU_LABELS) if want in label),
                    len(CPU_LABELS),
                )
                # Best-named first, then hottest. Several sensors rank the same where the
                # labels are blank and every entry falls back to one chip name. Taking
                # the first of those is taking core 0.
                if best is None or (rank, -entry.current) < (best[0], -best[1]):
                    best = (rank, entry.current)
        if best and best[0] < len(CPU_LABELS):
            return round(float(best[1]), 1)
        return None

    def _fans(self):
        fans = []
        try:
            for entry in psutil.sensors_fans().values():
                for fan in entry:
                    if fan.current:
                        fans.append({"name": fan.label or "fan", "rpm": int(fan.current)})
        except Exception:
            pass
        if fans:
            return fans
        # psutil misses PWM-only fans that report no RPM input.
        for path in sorted(glob.glob("/sys/class/hwmon/hwmon*/fan*_input")):
            try:
                with open(path) as handle:
                    rpm = int(handle.read().strip())
            except (OSError, ValueError):
                continue
            if rpm:
                fans.append({"name": os.path.basename(path).split("_")[0], "rpm": rpm})
        return fans
