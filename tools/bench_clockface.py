"""Time the ways of drawing the clock's marks and hands, on a badge.

    mpremote connect PORT run tools/bench_clockface.py

The dial is a rectangle per mark placed by a mat3, costing a render() per mark. The
alternative is sixty four-point contours collected into two shape.custom calls under
NON_ZERO, which the rasteriser takes as two render() calls for the whole set.

Two things this exists to check:

- The face image is allocated once, outside every timed region. Allocating a 168x168
  image per iteration measures garbage collection, not drawing.
- Whether re-aiming one cached shape beats building a fresh one depends on the firmware,
  and the last section measures that. MicroPython only advances its free-block hint
  for single-block allocations (py/gc.c, n_free == 1), so a loop that allocates nothing
  but multi-block objects rescans the allocation table from a stale index every time, and
  the per-draw cost climbs without bound. With 32-byte GC blocks and a six-float mat3 the
  boxed mat3 is one block and the cached path is both flat and fastest, which is why
  clockface.py caches. On a build with either of those changed, a fresh shape box - one
  block, so it keeps the hint moving - is the faster way round.

Both implementations are inlined rather than imported from clockface, so they can be
compared side by side. Keep the geometry in step with clockface.py if that changes.
"""

import gc
import math
import time

badge.mode(HIRES | VSYNC)
badge.default_clear = None

RADIUS = 82
MARKS = (16, 16, 18)
SIZE = RADIUS * 2 + 4
MIDDLE = (SIZE / 2.0, SIZE / 2.0)

HOUR_IN, HOUR_OUT, HOUR_HALF = RADIUS * 0.81, RADIUS * 0.97, RADIUS * 0.055
MIN_IN, MIN_OUT, MIN_HALF = RADIUS * 0.905, RADIUS * 0.97, RADIUS * 0.019
HAND_IN, HAND_OUT, HAND_HALF = -RADIUS * 0.13, RADIUS * 0.86, RADIUS * 0.048

ROUNDS = 7

# One image, reused by every case
face = image(SIZE, SIZE)
face.antialias = image.X4


def blank():
    face.pen = brush.erase()
    face.rectangle(rect(0, 0, SIZE, SIZE))
    face.pen = color.rgb(*MARKS)


def measure(name, per_round, fn, unit=1000.0, suffix="ms"):
    """Minimum of several rounds: the badge composites as well, so a mean is noise."""
    times = []
    fn(2)
    for _ in range(ROUNDS):
        gc.collect()
        t0 = time.ticks_us()
        fn(per_round)
        times.append(time.ticks_diff(time.ticks_us(), t0) / per_round / unit)
    times.sort()
    print(f"{name:<36} {times[0]:>9.3f} {times[len(times) // 2]:>9.3f} {suffix}")


# ── geometry, the old way and the new ────────────────────────────────────────
def blunt(centre, degrees, inner, outer, half_width):
    radians = math.radians(degrees)
    ax, ay = math.sin(radians), -math.cos(radians)
    bx, by = math.cos(radians), math.sin(radians)
    cx, cy = centre
    return [
        vec2(cx + ax * outer - bx * half_width, cy + ay * outer - by * half_width),
        vec2(cx + ax * outer + bx * half_width, cy + ay * outer + by * half_width),
        vec2(cx + ax * inner + bx * half_width, cy + ay * inner + by * half_width),
        vec2(cx + ax * inner - bx * half_width, cy + ay * inner - by * half_width),
    ]


def bar(centre, degrees, inner, outer, half_width):
    s = shape.rectangle(rect(-half_width, -outer, half_width * 2.0, outer - inner))
    s.transform = mat3().translate(centre[0], centre[1]).rotate(degrees)
    return s


# mat3.trs builds the same transform in one call, so one boxed mat3 instead of a chain
HAS_TRS = hasattr(mat3, "trs")


def bar_trs(centre, degrees, inner, outer, half_width):
    s = shape.rectangle(rect(-half_width, -outer, half_width * 2.0, outer - inner))
    s.transform = mat3.trs(centre[0], centre[1], degrees)
    return s


# ── the dial, four ways ──────────────────────────────────────────────────────
def bake_contours(n):
    for _ in range(n):
        blank()
        face.fill_rule = image.NON_ZERO
        minutes, hours = [], []
        for tick in range(60):
            degrees = tick * 6.0
            if tick % 5 == 0:
                hours.append(blunt(MIDDLE, degrees, HOUR_IN, HOUR_OUT, HOUR_HALF))
            else:
                minutes.append(blunt(MIDDLE, degrees, MIN_IN, MIN_OUT, MIN_HALF))
        face.shape(shape.custom(*minutes))
        face.shape(shape.custom(*hours))
        face.fill_rule = image.EVEN_ODD


def bake_calls(n):
    for _ in range(n):
        blank()
        for tick in range(60):
            if tick % 5 == 0:
                face.shape(bar(MIDDLE, tick * 6.0, HOUR_IN, HOUR_OUT, HOUR_HALF))
            else:
                face.shape(bar(MIDDLE, tick * 6.0, MIN_IN, MIN_OUT, MIN_HALF))


def bake_list(n):
    for _ in range(n):
        blank()
        marks = []
        for tick in range(60):
            if tick % 5 == 0:
                marks.append(bar(MIDDLE, tick * 6.0, HOUR_IN, HOUR_OUT, HOUR_HALF))
            else:
                marks.append(bar(MIDDLE, tick * 6.0, MIN_IN, MIN_OUT, MIN_HALF))
        face.shape(marks)


def bake_reaimed(n):
    # Two shapes re-aimed sixty times: the fewest allocations, and what clockface.py does
    for _ in range(n):
        blank()
        hour_mark = shape.rectangle(rect(-HOUR_HALF, -HOUR_OUT, HOUR_HALF * 2.0, HOUR_OUT - HOUR_IN))
        minute_mark = shape.rectangle(rect(-MIN_HALF, -MIN_OUT, MIN_HALF * 2.0, MIN_OUT - MIN_IN))
        for tick in range(60):
            mark = hour_mark if tick % 5 == 0 else minute_mark
            mark.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate(tick * 6.0)
            face.shape(mark)


def bake_list_trs(n):
    for _ in range(n):
        blank()
        marks = []
        for tick in range(60):
            if tick % 5 == 0:
                marks.append(bar_trs(MIDDLE, tick * 6.0, HOUR_IN, HOUR_OUT, HOUR_HALF))
            else:
                marks.append(bar_trs(MIDDLE, tick * 6.0, MIN_IN, MIN_OUT, MIN_HALF))
        face.shape(marks)


def bake_reaimed_trs(n):
    for _ in range(n):
        blank()
        hour_mark = shape.rectangle(rect(-HOUR_HALF, -HOUR_OUT, HOUR_HALF * 2.0, HOUR_OUT - HOUR_IN))
        minute_mark = shape.rectangle(rect(-MIN_HALF, -MIN_OUT, MIN_HALF * 2.0, MIN_OUT - MIN_IN))
        for tick in range(60):
            mark = hour_mark if tick % 5 == 0 else minute_mark
            mark.transform = mat3.trs(MIDDLE[0], MIDDLE[1], tick * 6.0)
            face.shape(mark)


print(f"{'dial bake, 60 marks':<36} {'min':>9} {'med':>9}")
measure("contours + trig, 2 draws", 10, bake_contours)
measure("mat3, fresh shapes, 60 draws", 10, bake_calls)
measure("mat3, fresh shapes, 1 list draw", 10, bake_list)
measure("mat3, 2 shapes re-aimed", 10, bake_reaimed)
if HAS_TRS:
    measure("mat3.trs, fresh shapes, 1 list draw", 10, bake_list_trs)
    measure("mat3.trs, 2 shapes re-aimed", 10, bake_reaimed_trs)

# ── one hand, the per-frame path ─────────────────────────────────────────────
CACHED = shape.rectangle(rect(-HAND_HALF, -HAND_OUT, HAND_HALF * 2.0, HAND_OUT - HAND_IN))


def hand_fresh(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        face.shape(bar(MIDDLE, i % 60 * 6.0, HAND_IN, HAND_OUT, HAND_HALF))


def hand_contour(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        face.shape(shape.custom(blunt(MIDDLE, i % 60 * 6.0, HAND_IN, HAND_OUT, HAND_HALF)))


def hand_cached(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        CACHED.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate(i % 60 * 6.0)
        face.shape(CACHED)


def hand_trs(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        face.shape(bar_trs(MIDDLE, i % 60 * 6.0, HAND_IN, HAND_OUT, HAND_HALF))


def hand_cached_trs(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        CACHED.transform = mat3.trs(MIDDLE[0], MIDDLE[1], i % 60 * 6.0)
        face.shape(CACHED)


print()
print(f"{'one hand drawn':<36} {'min':>9} {'med':>9}")
measure("fresh shape each draw", 300, hand_fresh, 1.0, "us")
measure("contour rebuilt each draw", 300, hand_contour, 1.0, "us")
measure("cached shape re-aimed", 300, hand_cached, 1.0, "us")
if HAS_TRS:
    measure("fresh shape, mat3.trs", 300, hand_trs, 1.0, "us")
    measure("cached shape, mat3.trs", 300, hand_cached_trs, 1.0, "us")

# ── whether a cached shape's per-draw cost stays flat ────────────────────────
BLOCK = 300
BLOCKS = 12


def in_blocks(fn):
    out = []
    for b in range(BLOCKS):
        t0 = time.ticks_us()
        fn(b)
        out.append(time.ticks_diff(time.ticks_us(), t0) / BLOCK)
    return out


def run_cached(b):
    face.pen = color.rgb(*MARKS)
    for i in range(BLOCK):
        CACHED.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate((b * BLOCK + i) % 60 * 6.0)
        face.shape(CACHED)


def run_fresh(b):
    face.pen = color.rgb(*MARKS)
    for i in range(BLOCK):
        face.shape(bar(MIDDLE, (b * BLOCK + i) % 60 * 6.0, HAND_IN, HAND_OUT, HAND_HALF))


print()
print(f"us/draw over {BLOCKS} blocks of {BLOCK} draws, with no collect between them")
for label, fn in (("cached, re-aimed", run_cached), ("fresh each draw", run_fresh)):
    gc.collect()
    times = in_blocks(fn)
    print(f"  {label:<18} " + " ".join(f"{t:.0f}" for t in times))
    print(f"  {'':18} first {times[0]:.0f}us, last {times[-1]:.0f}us, ratio {times[-1] / times[0]:.2f}")

print("BENCH: done")
