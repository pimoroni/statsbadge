"""Run the app's drawing on a badge without installing it, time it, and dump frames.

    mpremote connect PORT mount . run tools/probe.py

Mount the repo root, not the app directory: shots go to /remote/shots. Draws every
page kind against a canned frame, so it needs no server, then times a real poll if
the badge happens to be paired.
"""

import gc
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import draw
import look
import pages as pages_module

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

t0 = time.ticks_us()
draw.prepare()
print("font.load + prepare: %.1f ms" % (time.ticks_diff(time.ticks_us(), t0) / 1000))

# A frame with everything filled in, so no page draws "--" by accident.
FRAME = {
    "v": 1, "seq": 7, "layout_rev": 3,
    "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512,
            "cores": [31.0, 88.2, 12.5, 74.1, 20.0, 95.5, 60.2, 5.0,
                      44.0, 18.0, 66.0, 9.0]},
    "mem": {"pct": 71.2, "used_mb": 23330, "total_mb": 32768, "swap_pct": 12.0},
    "gpu": [{"name": "RTX 4070", "pct": 88.0, "temp": 67.0, "mem_pct": 54.0,
             "power": 182.5, "clock": 2610, "fan_pct": 62.0}],
    "net": {"iface": "en0", "up_bps": 1258291, "down_bps": 11534336,
            "up_total_mb": 1952, "down_total_mb": 8710},
    "disk": {"pct": 74.2, "read_bps": 52428800, "write_bps": 8388608,
             "used_mb": 703840, "total_mb": 948584},
    "power": {"battery_pct": 91, "charging": True, "package_w": 44.2},
    "fans": [{"name": "cpu", "rpm": 1820}],
    "sys": {"host": "workshop-pc", "os": "Windows 11", "arch": "AMD64",
            "cpu_name": "Ryzen 7 7800X3D", "uptime_s": 271830},
}


def ramp(n, peak=100.0):
    """A plausible wiggle for a graph, without needing a server's history."""
    import math
    return [peak * (0.35 + 0.3 * math.sin(i / 4.0) + 0.2 * math.sin(i / 1.7))
            for i in range(n)]


HISTORY = {
    "cpu.pct": ramp(48), "gpu.pct": ramp(48, 90),
    "cpu.temp": ramp(48, 80), "gpu.temp": ramp(48, 70),
    "net.down_bps": ramp(48, 11534336), "net.up_bps": ramp(48, 1258291),
}

PAGES = [
    {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
     "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]},
    {"id": "cores", "kind": "bars", "title": "Cores", "field": "cpu.cores"},
    {"id": "gpu", "kind": "dial", "title": "GPU", "field": "gpu.pct",
     "readouts": ["gpu.temp", "gpu.power", "gpu.mem_pct"]},
    {"id": "mem", "kind": "dial", "title": "Memory", "field": "mem.pct",
     "readouts": ["mem.used_mb", "mem.total_mb", "mem.swap_pct"]},
    {"id": "net", "kind": "graph", "title": "Network",
     "fields": ["net.down_bps", "net.up_bps"]},
    {"id": "thermal", "kind": "graph", "title": "Thermals",
     "fields": ["cpu.temp", "gpu.temp"]},
    {"id": "disk", "kind": "grid", "title": "Disk",
     "fields": ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]},
    {"id": "host", "kind": "text", "title": "Host",
     "fields": ["sys.host", "sys.os", "sys.cpu_name", "sys.uptime_s",
                "power.battery_pct", "power.package_w"]},
]


def shot(name):
    with open(f"/remote/shots/{name}.raw", "wb") as handle:
        handle.write(screen.raw)


def time_page(page, theme, n=12):
    """ms per frame excluding display.update, after one warm-up frame."""
    pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES))
    gc.collect()
    t = time.ticks_us()
    for _ in range(n):
        pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES))
    return time.ticks_diff(time.ticks_us(), t) / n / 1000


print()
theme = look.get("afterburner")
for index, page in enumerate(PAGES):
    per_frame = time_page(page, theme)
    pages_module.render(page, FRAME, HISTORY, theme, index, len(PAGES),
                        FRAME["sys"]["host"])
    badge.update()
    shot(page["id"])
    print(f"{page['id']:<8} {page['kind']:<6} {per_frame:6.2f} ms/frame")

# A page that has to redraw its furniture is the worst case; measure a page turn.
draw.clear_cache()
t = time.ticks_us()
pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), "host")
print("\nfirst draw of a page, cold cache: %.1f ms"
      % (time.ticks_diff(time.ticks_us(), t) / 1000))

print("\nevery theme, on the CPU dial:")
for name in ("afterburner", "mono", "amber", "blueprint", "vapor"):
    theme = look.get(name)
    draw.clear_cache()
    per_frame = time_page(PAGES[0], theme)
    pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), "workshop-pc")
    badge.update()
    shot(f"theme_{name}")
    print(f"  {name:<12} {per_frame:6.2f} ms/frame")

# Missing data must read as "unknown", not as zero.
draw.clear_cache()
theme = look.get("afterburner")
sparse = {"v": 1, "cpu": {"pct": 12.0}, "mem": {}, "gpu": [], "net": {},
          "disk": {}, "power": {}, "fans": [], "sys": {"host": "quiet"}}
pages_module.render(PAGES[0], sparse, {}, theme, 0, len(PAGES), "quiet")
badge.update()
shot("sparse")
print("\nsparse frame drew without raising")

draw.banner(theme, "Not paired", "Hold C for setup", "or run: statsbadge install")
badge.update()
shot("banner")

gc.collect()
print(f"free memory: {gc.mem_free() // 1024} KB")
print("\ndone; shots are in ./shots")
