"""NVIDIA GPUs via NVML, which is the same library nvidia-smi uses.

Works on Windows and Linux and gives everything an Afterburner panel wants without
privileges. Needs `pynvml` (`pip install stats-badge[nvidia]`).
"""

from .base import Source

MB = 1024 * 1024


class Nvidia(Source):
    name = "nvidia"
    provides = ("gpu",)

    @classmethod
    def available(cls):
        try:
            import pynvml
        except ImportError:
            return False
        try:
            pynvml.nvmlInit()
        except Exception:
            return False
        try:
            return pynvml.nvmlDeviceGetCount() > 0
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def __init__(self, config):
        super().__init__(config)
        self._nvml = None
        self._handles = []

    def start(self):
        import pynvml
        pynvml.nvmlInit()
        self._nvml = pynvml
        self._handles = [
            pynvml.nvmlDeviceGetHandleByIndex(i)
            for i in range(pynvml.nvmlDeviceGetCount())
        ]

    def stop(self):
        if self._nvml:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass

    def sample(self, frame, dt):
        if not self._nvml:
            return
        nvml = self._nvml
        gpus = []
        for handle in self._handles:
            gpu = {}
            for field, read in (
                ("name", lambda h: _text(nvml.nvmlDeviceGetName(h))),
                ("pct", lambda h: float(nvml.nvmlDeviceGetUtilizationRates(h).gpu)),
                ("temp", lambda h: float(nvml.nvmlDeviceGetTemperature(h, nvml.NVML_TEMPERATURE_GPU))),
                ("power", lambda h: round(nvml.nvmlDeviceGetPowerUsage(h) / 1000.0, 1)),
                ("fan_pct", lambda h: float(nvml.nvmlDeviceGetFanSpeed(h))),
                ("clock", lambda h: float(nvml.nvmlDeviceGetClockInfo(h, nvml.NVML_CLOCK_GRAPHICS))),
            ):
                try:
                    gpu[field] = read(handle)
                except Exception:
                    # Not every card reports every field; a missing fan is normal.
                    pass
            try:
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                gpu["mem_used_mb"] = round(mem.used / MB)
                gpu["mem_pct"] = round(100.0 * mem.used / mem.total, 1)
            except Exception:
                pass
            gpus.append(gpu)
        if gpus:
            frame["gpu"] = _merge(frame["gpu"], gpus)


def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


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
