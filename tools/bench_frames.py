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
    "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512,
            "cores": [40.0 + (i * 7) % 55 for i in range(16)]},
    "sys": {"host": "workshop-pc"},
    "clock": {"time": "10:09", "date": "Fri 31 Jul", "hour": 10, "minute": 9,
              "seconds": 36},
    "weather": {"temp": 16.0, "condition": "overcast", "wind": 14.0},
}

# What the host's rings look like: as many points as `graph_points` defaults to, and not a
# straight line, a curve through equal samples costing nothing to interpolate.
HISTORY = {
    "cpu.pct": [30.0 + (i * 13) % 60 for i in range(48)],
    "cpu.temp": [50.0 + (i * 7) % 35 for i in range(48)],
}

PAGES = (
    {"id": "clock", "kind": "clockface", "title": "Clock",
     "fields": ["clock.time", "clock.date"]},
    {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
     "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]},
    # The only built-in kind in pages.ANIMATED, so this one really does draw every frame.
    {"id": "cores", "kind": "waterfall", "title": "Cores", "field": "cpu.cores"},
    # Smoothed and walking, which is the path that interpolates a curve per frame.
    {"id": "graph", "kind": "graph", "title": "Graph",
     "fields": ["cpu.pct", "cpu.temp"]},
)

FRAMES = 300
theme = look.get(look.DEFAULT)

# A graph only walks between polls when the host asked for it, and that is the case worth
# timing: without it the page redraws once a second and the curve costs nothing.
pages_module.PLOT_ANIMATION = True
pages_module.note_spacing(1000, 1000)

# A poll lands about this often, which the waterfall eases between and what moves
# a walking plot along.
POLL_FRAMES = 90


def spread(name, samples):
    samples = sorted(samples)
    n = len(samples)
    print(f"  {name:<16} min {samples[0] / 1000:6.2f}  "
          f"median {samples[n // 2] / 1000:6.2f}  "
          f"p90 {samples[int(n * 0.9)] / 1000:6.2f}  max {samples[-1] / 1000:6.2f} ms")


for page in PAGES:
    draw.clear_cache()
    # One warm-up pass so the label cache and any page-side bake are paid for already
    pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES), "workshop-pc")
    badge.update()

    renders = []
    updates = []
    whole = []
    for i in range(FRAMES):
        FRAME["clock"]["seconds"] = i % 60
        if i and not i % POLL_FRAMES:
            FRAME["seq"] += 1
        top = time.ticks_us()
        pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES), "workshop-pc")
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
