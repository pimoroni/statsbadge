"""Stats sources.

A source fills in part of a frame. `available()` gates whether it loads at all, so
a machine without an NVIDIA card never imports pynvml. Sources run in registration
order and later ones may fill gaps the earlier ones left, which is how a
platform-specific source adds temperatures psutil cannot see.
"""

import platform

from .base import Source


def discover(config=None):
    """Every source that works on this machine, in the order they should run."""
    from . import portable

    candidates = [portable.Portable]

    system = platform.system()
    if system == "Darwin":
        from . import macos
        candidates += [macos.MacPowermetrics, macos.MacIOKit]
    elif system == "Linux":
        from . import linux
        candidates.append(linux.LinuxHwmon)
    elif system == "Windows":
        from . import windows
        candidates.append(windows.LibreHardwareMonitor)

    from . import nvidia
    candidates.append(nvidia.Nvidia)

    from . import amd
    candidates.append(amd.AmdGpu)

    loaded = []
    for cls in candidates:
        try:
            if cls.available(config or {}):
                loaded.append(cls(config or {}))
        except Exception:
            # A source that cannot even answer available() counts as absent.
            continue
    return loaded


__all__ = ["Source", "discover"]
