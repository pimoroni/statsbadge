"""What the app allocates per frame, and how carved up the heap gets.

    mpremote connect PORT mount . run tools/mem_probe.py

Not instrumentation in the app: the harness makes the churn itself. Every frame here is
drawn with values that have changed, matching what the badge sees once a second. A few
seconds of this covers an hour of sitting on one page.

Three things come out of it, per page kind:

  bytes/frame   `gc.mem_alloc` only grows between collects, so a delta across an
                interval with no collect in it is total allocation. Sampled, and the
                intervals a collect landed in are dropped.
  sprites       how many strings got baked into an image, and how often the cache was dumped.
  max free sz   the largest contiguous free run, off `micropython.mem_info()`. This is the
                fragmentation figure: free memory can be plentiful and still not have a 6KB
                hole in it for the next sprite.
"""

import gc
import micropython
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")
sys.path.insert(0, "/remote/extensions/statsbadge-quakes/src/statsbadge_quakes/badge")
sys.path.insert(0, "/remote/extensions/statsbadge-iss/src/statsbadge_iss/badge")

import draw
import look
import pages as pages_module

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)
draw.prepare()

# The extensions register their kinds on import, and the map parse happens on their first frame.
import issmap  # noqa: E402, F401  imported for the page kind it registers
import quakemap  # noqa: E402, F401

THEME = look.get(look.DEFAULT)


# -- the instruments, priced before they are trusted -------------------------

def price(label, call, rounds=3):
    call()
    times = []
    for _ in range(rounds):
        t0 = time.ticks_us()
        call()
        times.append(time.ticks_diff(time.ticks_us(), t0) / 1000.0)
    times.sort()
    print(f"  {label:<22} {times[0]:8.1f}ms")


print("what the instruments cost")
price("gc.mem_alloc()", gc.mem_alloc)
price("gc.mem_free()", gc.mem_free)
price("gc.collect()", gc.collect)
price("mem_info()", micropython.mem_info)

print("\nwhat mem_info() reports")
micropython.mem_info()


# -- the frames, with something different in them every time ----------------

def frame_at(tick):
    """A frame whose readings have all moved, as they have when a poll lands."""
    return {
        "v": 1, "seq": tick,
        "cpu": {"pct": 20.0 + (tick % 700) / 10.0, "temp": 40.0 + (tick % 400) / 10.0,
                "freq": 3000 + tick % 1200, "procs": 400 + tick % 90,
                "cores": [10.0 + (tick + i * 7) % 80 for i in range(8)]},
        "mem": {"pct": 30.0 + (tick % 600) / 10.0, "used_mb": 8000 + tick % 900,
                "total_mb": 32768, "swap_pct": (tick % 200) / 10.0},
        "gpu": [{"name": "RTX 4070", "pct": 40.0 + (tick % 500) / 10.0,
                 "temp": 50.0 + (tick % 300) / 10.0, "power": 100.0 + tick % 90,
                 "mem_pct": 40.0 + (tick % 400) / 10.0}],
        "net": {"down_bps": 1e6 + tick * 1731, "up_bps": 2e5 + tick * 311},
        "disk": {"pct": 50.0 + (tick % 300) / 10.0, "read_bps": 5e6 + tick * 9173,
                 "write_bps": 1e6 + tick * 613, "used_mb": 400000 + tick},
        "power": {"battery_pct": 50 + tick % 50, "package_w": 20.0 + tick % 60},
        "sys": {"host": "workshop-pc", "os": "Windows 11", "cpu_name": "Ryzen 7 7800X3D",
                "uptime_s": 100000 + tick},
        # The two map pages. A frame here is a second of the badge's time, the harness
        # being faster only for skipping the poll wait. Each of these moves at the rate the
        # host moves it: the station every five seconds, its track every two minutes, the
        # sub-solar point a quarter of a degree a minute.
        "iss": {
            "where": {"lat": ((tick // 5) % 103) - 51.0, "lon": (((tick // 5) * 3) % 360) - 180.0,
                      "altitude": 410.0 + ((tick // 5) % 90) / 10.0,
                      "speed": 27600.0 + (tick // 5) % 40,
                      "sunlit": (tick // 150) % 2 == 0, "unit": "km",
                      "solar_lat": 17.5, "solar_lon": ((tick // 240) % 360) - 180.0,
                      "age_s": tick % 5},
            "track": [((((tick // 120) + i * 22) % 360) - 180.0,
                       51.0 * ((i % 8) - 4) / 4.0, i % 2) for i in range(20)],
            "flown": 9.0 + (tick % 120) / 120.0,
            "aboard": 7,
        },
        "quakes": {
            # A new set every five minutes, matching the host's fetch. The page's six
            # second cycle moves the camera between them meanwhile.
            "events": [{"mag": 4.0 + (((tick // 300) + i) % 30) / 10.0,
                        "place": f"{10 + i * 7} km SSE of Somewhere {i}",
                        "lon": (((tick // 300) * 5 + i * 37) % 360) - 180.0,
                        "lat": (((tick // 300) + i * 23) % 120) - 60.0,
                        "depth": 10.0 + i * 3, "age_s": 600 + tick + i * 900}
                       for i in range(10)],
            "count": 10, "biggest": 6.1, "latest": 4.2,
        },
    }


# The series alone, as render() is handed them. The whole reply left every plot falling back
# to two points of the live value, and the figures for them were a flat line's.
HISTORY = {ref: [20.0 + (i * 13) % 70 for i in range(48)]
           for ref in ("cpu.pct", "cpu.temp", "gpu.temp", "net.down_bps",
                       "net.up_bps", "mem.pct", "disk.pct")}

PAGES = (
    # The control: build the frame, composite, draw nothing. Every row below carries this, since
    # the harness makes a whole frame tree per frame where the app parses one per poll - so a
    # page's own cost is its figure less this one.
    ("nothing", None, 400),
    ("dial", {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
              "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]}, 400),
    ("dials", {"id": "load", "kind": "dials", "title": "Load",
               "fields": ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct"]}, 400),
    ("grid", {"id": "disk", "kind": "grid", "title": "Disk",
              "fields": ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]}, 400),
    ("text", {"id": "host", "kind": "text", "title": "Host",
              "fields": ["sys.host", "sys.os", "sys.cpu_name", "sys.uptime_s"]}, 400),
    ("spark", {"id": "spark", "kind": "spark", "title": "Spark",
               "fields": ["cpu.pct", "cpu.temp", "mem.pct", "disk.pct"]}, 300),
    ("graph", {"id": "net", "kind": "graph", "title": "Network",
               "fields": ["net.down_bps", "net.up_bps"]}, 300),
    # In pages.ANIMATED, so this is the one kind whose figure is per frame and not per poll.
    ("waterfall", {"id": "cores", "kind": "waterfall", "title": "Cores",
                   "field": "cpu.cores"}, 300),
    ("badge", {"id": "badge", "kind": "badge", "title": "Badge"}, 400),
    ("quakemap", {"id": "quakes", "kind": "quakemap", "title": "Quakes", "hold": 6}, 200),
    ("issmap", {"id": "iss", "kind": "issmap", "title": "ISS"}, 150),
)


# How often `gc.mem_alloc` is read during a run. It only grows between collects, so a
# delta is total allocation, and a run this size trips a collect that drops it.

# Sampled, not taken once, so a collect ends one interval instead of hiding a run's worth
# of allocation. 44ms a read, and twenty of them is a second.
SAMPLE_EVERY = 20
# The figure badge_app sets at launch, for the last section to measure the shipped policy.
THRESHOLD = 256 * 1024


def drive(name, page, rounds):
    """Render one page kind `rounds` times, each with a frame that has moved.

    Reports total allocation a frame, the frame time, the sprite cache activity, and the
    heap once this page's garbage has been collected. That last figure is the point of the
    exercise: free memory in runs too small to use is not free.
    """
    gc.collect()
    dumps = 0
    peak_sprites = held = len(draw._labels)  # noqa: SLF001  the cache is being measured
    allocated = 0
    collects = 0
    mark = gc.mem_alloc()
    t0 = time.ticks_ms()
    for tick in range(rounds):
        frame = frame_at(tick)
        if page is not None:
            pages_module.render(page, frame, HISTORY, THEME, 0, len(PAGES), "workshop-pc")
        badge.update()
        now = len(draw._labels)  # noqa: SLF001
        if now < held:
            dumps += 1
        held = now
        if now > peak_sprites:
            peak_sprites = now
        if tick % SAMPLE_EVERY == SAMPLE_EVERY - 1:
            at = gc.mem_alloc()
            if at >= mark:
                allocated += at - mark
            else:
                collects += 1
            mark = at
    took = time.ticks_diff(time.ticks_ms(), t0) - (rounds // SAMPLE_EVERY) * 44
    intervals = rounds // SAMPLE_EVERY
    counted = (intervals - collects) * SAMPLE_EVERY
    # An interval a collect landed in shows a fall, not a rise, and goes uncounted. With a
    # threshold set that is most of them, leaving too little to draw on. The figure is
    # reported only when it was taken over most of the run.
    figure = f"{allocated // counted:>7}B/frame" if counted * 2 > rounds else "      --      "
    gc.collect()
    print(f"  {name:<10} {figure} {took / rounds:6.1f}ms/frame  "
          f"sprites {held:>3} peak {peak_sprites:>3} dumped {dumps}  collects {collects:>2}  ",
          end="")
    micropython.mem_info()


print("\nper page kind, every frame drawn from a reading that moved")
print(f"  {'kind':<10} {'alloc':>13}  {'time':>13}  sprites")
for name, page, rounds in PAGES:
    drive(name, page, rounds)

print("\nthe heap after all of that, uncollected")
micropython.mem_info()
gc.collect()
print("and after a collect")
micropython.mem_info()

# What the shipped policy does to the same run. Left alone the collector only runs when an
# allocation fails, so garbage piles up to whatever is free, leaving the free list in
# pieces. The app sets this at launch; the harness sets it here to measure it.
print(f"\nthe worst two again, with the app's gc.threshold({THRESHOLD // 1024}KB) set")
gc.threshold(THRESHOLD)
for name, page, rounds in PAGES:
    if name in ("quakemap", "issmap"):
        drive(name, page, rounds)
gc.threshold(-1)

print("MEM PROBE: done")
