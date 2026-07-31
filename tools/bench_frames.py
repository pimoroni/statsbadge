"""Time the app's own frame loop on a badge, phase by phase.

    mpremote connect PORT mount . run tools/bench_frames.py

Renders a real page against a canned frame the way __init__.py's loop does, and reports
the distribution of the interval rather than a mean: a hand that sweeps unevenly is a
tail, and a mean hides it. Nothing here talks to a server, so what it measures is drawing
and display, not polling.
"""

import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")
sys.path.insert(0, "/remote/extensions/statsbadge-clock/src/statsbadge_clock/badge")

import draw
import look
import pages as pages_module

import clockface  # noqa: F401

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

draw.prepare()

FRAME = {
    "v": 1, "seq": 1, "layout_rev": 1,
    "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512},
    "sys": {"host": "workshop-pc"},
    "clock": {"time": "10:09", "date": "Fri 31 Jul", "hour": 10, "minute": 9,
              "seconds": 36},
    "weather": {"temp": 16.0, "condition": "overcast", "wind": 14.0},
}

PAGES = (
    {"id": "clock", "kind": "clockface", "title": "Clock",
     "fields": ["clock.time", "clock.date"]},
    {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
     "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]},
)

FRAMES = 300
theme = look.get(look.DEFAULT)


def spread(name, samples):
    samples = sorted(samples)
    n = len(samples)
    print(f"  {name:<16} min {samples[0] / 1000:6.2f}  "
          f"median {samples[n // 2] / 1000:6.2f}  "
          f"p90 {samples[int(n * 0.9)] / 1000:6.2f}  max {samples[-1] / 1000:6.2f} ms")


for page in PAGES:
    draw.clear_cache()
    # One warm-up pass so the label cache and any page-side bake are paid for already
    pages_module.render(page, FRAME, {}, theme, 0, len(PAGES), "workshop-pc")
    badge.update()

    renders = []
    updates = []
    whole = []
    for i in range(FRAMES):
        FRAME["clock"]["seconds"] = i % 60
        top = time.ticks_us()
        pages_module.render(page, FRAME, {}, theme, 0, len(PAGES), "workshop-pc")
        mid = time.ticks_us()
        badge.update()
        end = time.ticks_us()
        renders.append(time.ticks_diff(mid, top))
        updates.append(time.ticks_diff(end, mid))
        whole.append(time.ticks_diff(end, top))

    print(f"{page['kind']}, {FRAMES} frames")
    spread("render", renders)
    spread("badge.update", updates)
    spread("frame", whole)
    slow = sum(1 for v in whole if v > 2 * sorted(whole)[len(whole) // 2])
    print(f"  frames over 2x the median: {slow}")
    print()

print("BENCH: done")
