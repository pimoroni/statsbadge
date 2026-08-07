"""Shoot the waterfall page with its plot full.

    mpremote connect PORT mount . run tools/waterfall_shot.py
    python3 tools/shots.py build/shots

Every other page draws itself from one frame, so `tools/live_shots.py` can shoot it in one
render. This one cannot: it puts down one column a frame and the plot is 278 columns wide,
seeded with 24 off the host's history ring, so a single render shows a sliver of data and a
screen of background.

So this drives it the way a badge does, a poll's worth of frames between samples with
the page interpolating across them, until the plot is full.

The load is a model and not a reading. The plot holds ten polls, so what it shows has to
happen inside ten seconds, which a real machine obliges rarely and never on request.
Twelve threads: idle, then joining a few at a time, then a shifting plateau, with two
pegged and one waiting on something.
"""

import math
import os
import sys

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import draw
import look
import pages as pages_module

for directory in ("/remote/build", "/remote/build/shots"):
    try:
        os.mkdir(directory)
    except OSError:
        pass

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)
draw.prepare()

THEME = look.get(look.DEFAULT)
PAGE = {"id": "cores", "kind": "waterfall", "title": "Cores", "field": "cpu.cores"}
HOST = "workshop-pc"

# What the badge draws between polls, at the rate it draws them: the app polls once a second
# and this page is animated, so a poll is about 28 frames.
FRAMES_PER_POLL = 28
CORES = 12
POLLS = 18
# Which threads stay pegged, and which one waits on something the whole time. Without them a
# busy machine draws as a solid block.
PEGGED = (3, 9)
WAITING = 5
# When the threads start joining, late enough that the idle before it is still on the plot.
JOINS = 9


def jitter(seed):
    """A repeatable wobble in -3..3, so the shot is the same shot every time.

    Its own arithmetic because MicroPython's `random` has no `gauss`, and a shot that moves
    between runs is a shot that cannot be compared with the last one.
    """
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    return (seed % 600) / 100.0 - 3.0


def load_at(poll, core):
    """One thread's load, as a percentage."""
    if core == WAITING:
        return 6.0 + (poll * 7 % 11)
    joins = JOINS + (core % 5) * 0.6
    if poll < joins:
        return 5.0 + (poll * 13 + core * 7) % 12
    if core in PEGGED:
        return 93.0 + (poll * 3 + core) % 7
    settled = min(1.0, (poll - joins) / 1.5)
    wave = 20.0 * math.sin(core * 0.9 + poll * 0.55)
    return 22.0 + settled * (62.0 + wave)


frame = {"v": 1, "seq": 0, "cpu": {}, "sys": {"host": HOST}}
pages_module.PLOT_ANIMATION = True

for poll in range(POLLS):
    row = [max(2.0, min(100.0, load_at(poll, core) + jitter(poll * 31 + core)))
           for core in range(CORES)]
    frame["seq"] = poll
    frame["cpu"] = {"pct": sum(row) / len(row), "cores": row}
    for _ in range(FRAMES_PER_POLL):
        pages_module.render(PAGE, frame, {}, THEME, 4, 9, HOST)
        badge.update()

with open("/remote/build/shots/waterfall.raw", "wb") as handle:
    handle.write(screen.raw)
print(f"filled the plot over {POLLS} polls, {POLLS * FRAMES_PER_POLL} frames")
print("WATERFALL SHOT: done")
