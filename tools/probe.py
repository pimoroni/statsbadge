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
sys.path.insert(0, "/remote/extensions/statsbadge-clock/src/statsbadge_clock/badge")

import draw
import look
import pages as pages_module

# Registers the clockface kind by importing, the same way the app picks it up out of
# ext/. Without this the clock page has no renderer and draws a message saying so.
import clockface  # noqa: F401

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
    # The clock extension's groups. The hands are drawn from hour, minute and seconds
    # and the digits from time, so these have to agree or the shot shows a clock
    # disagreeing with itself.
    "clock": {"time": "10:09", "date": "Fri 31 Jul", "hour": 10, "minute": 9,
              "seconds": 36},
    "weather": {"temp": 16.0, "feels": 14.0, "humidity": 78, "wind": 14.0,
                "condition": "overcast", "code": 3,
                # Units travel with the numbers, and the icon is a character in the
                # extension's own icons.af.
                "temp_unit": "C", "wind_unit": "km/h", "icon": "f"},
}


def ramp(n, peak=100.0):
    """A plausible wiggle for a graph, without needing a server's history."""
    import math
    return [peak * (0.35 + 0.3 * math.sin(i / 4.0) + 0.2 * math.sin(i / 1.7))
            for i in range(n)]


def core_ramp(cores, n):
    """A ring of per-core samples, the shape the host's history sends for a list field."""
    import math
    return [[max(0.0, min(100.0, 50 + 45 * math.sin(i / 5.0 + c * 0.8)))
             for c in range(cores)] for i in range(n)]


HISTORY = {
    "cpu.cores": core_ramp(12, 40),
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
    {"id": "gauges", "kind": "dials", "title": "Load",
     "fields": ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct"]},
    {"id": "gauges3", "kind": "dials", "title": "System",
     "fields": ["cpu.pct", "mem.pct", "disk.pct"]},
    {"id": "gauges2", "kind": "dials", "title": "Processor",
     "fields": ["cpu.pct", "cpu.temp"]},
    {"id": "net", "kind": "graph", "title": "Network",
     "fields": ["net.down_bps", "net.up_bps"]},
    {"id": "thermal", "kind": "graph", "title": "Thermals",
     "fields": ["cpu.temp", "gpu.temp"]},
    {"id": "disk", "kind": "grid", "title": "Disk",
     "fields": ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]},
    {"id": "host", "kind": "text", "title": "Host",
     "fields": ["sys.host", "sys.os", "sys.cpu_name", "sys.uptime_s",
                "power.battery_pct", "power.package_w"]},
    {"id": "rings", "kind": "rings", "title": "Load",
     "fields": ["cpu.pct", "mem.pct", "gpu.pct", "disk.pct"]},
    {"id": "spark", "kind": "spark", "title": "At a glance",
     "fields": ["cpu.pct", "cpu.temp", "mem.pct", "gpu.pct", "net.down_bps",
                "disk.read_bps"]},
    {"id": "radar", "kind": "radar", "title": "Shape",
     "fields": ["cpu.pct", "mem.pct", "gpu.pct", "disk.pct", "gpu.temp"]},
    {"id": "trend", "kind": "trend", "title": "CPU", "field": "cpu.pct"},
    {"id": "waterfall", "kind": "waterfall", "title": "Cores", "field": "cpu.cores"},
    # The clock extension's page. The id names the shot the README shows.
    {"id": "swiss_clock", "kind": "clockface", "title": "Clock",
     "fields": ["clock.time", "clock.date", "weather.temp", "weather.condition"]},
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
theme = look.get("dark")
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
for name in ("dark", "light", "frost", "mono", "red", "green", "cyan",
             "amber", "blueprint", "vapor"):
    theme = look.get(name)
    draw.clear_cache()
    per_frame = time_page(PAGES[0], theme)
    pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), "workshop-pc")
    badge.update()
    shot(f"theme_{name}")
    print(f"  {name:<12} {per_frame:6.2f} ms/frame")

# Missing data must read as "unknown", not as zero.
draw.clear_cache()
theme = look.get("dark")
sparse = {"v": 1, "cpu": {"pct": 12.0}, "mem": {}, "gpu": [], "net": {},
          "disk": {}, "power": {}, "fans": [], "sys": {"host": "quiet"}}
pages_module.render(PAGES[0], sparse, {}, theme, 0, len(PAGES), "quiet")
badge.update()
shot("sparse")
print("\nsparse frame drew without raising")


# Every screen that is not a page, so the shots in the README cannot drift from what
# the app draws. The wording is not repeated here: these call the app's own drawing.
import setup as setup_ui    # noqa: E402
import splash               # noqa: E402

SCREENS = (
    ("splash", splash.show),
    ("banner", lambda: draw.banner(theme, "Not paired", "B to set up",
                                   "or run: statsbadge install")),
    ("err_rejected", lambda: draw.banner(theme, "Not recognised", "workshop-pc",
                                         "B to pair again")),
    ("err_noserver", lambda: draw.banner(theme, "Connecting", "workshop-pc:8420",
                                         "no server answering")),
    ("setup_looking", lambda: draw.banner(theme, "Looking",
                                          "for a host on the network",
                                          "4s  -  HOME to cancel")),
    ("setup_nohost", lambda: draw.banner(theme, "No host", "nothing answered",
                                         "B retry   HOME quit")),
    ("setup_choose", lambda: setup_ui.draw_hosts(theme, DISCOVERED, 0, {"pc-1": {}})),
    ("setup_code", lambda: setup_ui.draw_code(theme, "7F3A9C", "workshop-pc")),
    ("setup_refused", lambda: draw.banner(theme, "Refused", "already pairing",
                                          "B retry   A back   HOME quit")),
    ("setup_paired", lambda: draw.banner(theme, "Paired", "workshop-pc",
                                         "2 host(s) known")),
    ("menu_hosts", lambda: setup_ui.draw_rows(theme, MENU_ROWS, 0)),
)

DISCOVERED = [
    {"id": "pc-1", "name": "workshop-pc", "host": "10.10.1.40", "port": 8420},
    {"id": "mac-1", "name": "studio-mac", "host": "10.10.1.51", "port": 8420},
]

MENU_ROWS = [
    {"kind": "known", "label": "workshop-pc", "detail": "10.10.1.40:8420",
     "note": "active"},
    {"kind": "known", "label": "studio-mac", "detail": "10.10.1.51:8420",
     "note": "here"},
    {"kind": "known", "label": "linux-box", "detail": "10.10.1.62:8420",
     "note": "not seen"},
    {"kind": "new", "label": "spare-pi", "detail": "10.10.1.77:8420", "note": "add"},
    {"kind": "rescan", "label": "Look again", "detail": "", "note": ""},
    {"kind": "exit", "label": "Leave the app", "detail": "", "note": ""},
]

print()
theme = look.get("dark")
for name, render in SCREENS:
    draw.clear_cache()
    render()
    badge.update()
    shot(name)
    print(f"  {name}")

gc.collect()
print(f"free memory: {gc.mem_free() // 1024} KB")
print("\ndone; shots are in ./shots")
