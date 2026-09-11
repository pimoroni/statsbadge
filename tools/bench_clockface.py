"""Time the ways of drawing the clock's marks and hands, on a badge."""

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

# One image, reused by every case.
face = image(SIZE, SIZE)
face.antialias = image.X4


def blank():
    face.pen = brush.erase()
    face.rectangle(rect(0, 0, SIZE, SIZE))
    face.pen = color.rgb(*MARKS)


def measure(name, per_round, fn, unit=1000.0, suffix="ms"):
    """Return the minimum of several rounds: the badge composites too, so a mean is noise."""
    times = []
    fn(2)
    for _ in range(ROUNDS):
        gc.collect()
        t0 = time.ticks_us()
        fn(per_round)
        times.append(time.ticks_diff(time.ticks_us(), t0) / per_round / unit)
    times.sort()
    print(f"{name:<36} {times[0]:>9.3f} {times[len(times) // 2]:>9.3f} {suffix}")


# -- geometry, the old way and the new ---------------------------------------
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


# mat3.trs builds the same transform in one call, so one boxed mat3 instead of a chain.
HAS_TRS = hasattr(mat3, "trs")


def bar_trs(centre, degrees, inner, outer, half_width):
    s = shape.rectangle(rect(-half_width, -outer, half_width * 2.0, outer - inner))
    s.transform = mat3.trs(centre[0], centre[1], degrees)
    return s


# -- the dial, four ways ----------------------------------------------------
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
    # Two shapes re-aimed sixty times: the fewest allocations, and what clockface.py does.
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

# -- one hand, the per-frame path -------------------------------------------
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

# -- a multi-part hand, part by part or as one combined shape ---------------
HAS_COMBINE = hasattr(shape, "combine")

SEC_TAIL, SEC_OUT, SEC_HALF = -RADIUS * 0.13, RADIUS * 0.86, RADIUS * 0.012
RING_AT, RING_OUT, RING_BAND = 0.62, 0.10, 0.022
RING_MID = SEC_OUT * RING_AT
RING_HOLE = RADIUS * (RING_OUT - RING_BAND)

# The second hand of the Station face: two bars and the ring between them.
RING_PARTS = (
    shape.rectangle(rect(-SEC_HALF, -(RING_MID - RING_HOLE),
                         SEC_HALF * 2.0, (RING_MID - RING_HOLE) - SEC_TAIL)),
    shape.rectangle(rect(-SEC_HALF, -SEC_OUT, SEC_HALF * 2.0, SEC_OUT - (RING_MID + RING_HOLE))),
    shape.arc(vec2(0, -RING_MID), RING_HOLE, RADIUS * RING_OUT, 0, 360),
)


def parts_reaimed(n):
    face.pen = color.rgb(*MARKS)
    for i in range(n):
        for part in RING_PARTS:
            part.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate(i % 60 * 6.0)
            face.shape(part)


def parts_combined(n):
    face.pen = color.rgb(*MARKS)
    # NON_ZERO fills the union; under EVEN_ODD the bars and the ring hollow each other out.
    face.fill_rule = image.NON_ZERO
    for i in range(n):
        COMBINED_HAND.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate(i % 60 * 6.0)
        face.shape(COMBINED_HAND)
    face.fill_rule = image.EVEN_ODD


if HAS_COMBINE:
    COMBINED_HAND = shape.combine(list(RING_PARTS))
    print()
    print(f"{'one 3-part hand drawn':<36} {'min':>9} {'med':>9}")
    measure("each part re-aimed, 3 draws", 300, parts_reaimed, 1.0, "us")
    measure("combined, 1 aim and 1 draw", 300, parts_combined, 1.0, "us")


# -- the dial as one combined shape, against baking it into an image --------
# The rasteriser batches a shape into 1024 edges (MAX_EDGES) and abandons one that does
# not fit, so an over-long combine draws nothing at all rather than part of itself.
# Sixty bars fit; sixty dots or ovals do not. `ink` is the check.
HOUR_TRACK, MIN_TRACK = RADIUS * 0.85, RADIUS * 0.95

MARK_STYLES = (
    ("bars",
     lambda: shape.rectangle(rect(-HOUR_HALF, -HOUR_OUT, HOUR_HALF * 2.0, HOUR_OUT - HOUR_IN)),
     lambda: shape.rectangle(rect(-MIN_HALF, -MIN_OUT, MIN_HALF * 2.0, MIN_OUT - MIN_IN))),
    ("dots",
     lambda: shape.circle(vec2(0, -HOUR_TRACK), HOUR_HALF),
     lambda: shape.circle(vec2(0, -HOUR_TRACK), MIN_HALF)),
    ("ovals",
     lambda: shape.rounded_rectangle(rect(-HOUR_HALF, -HOUR_OUT, HOUR_HALF * 2.0, HOUR_OUT - HOUR_IN), HOUR_HALF),
     lambda: shape.circle(vec2(0, -MIN_TRACK), MIN_HALF)),
)


def dial_marks(big, small):
    marks = []
    for tick in range(60):
        mark = big() if tick % 5 == 0 else small()
        mark.transform = mat3().translate(MIDDLE[0], MIDDLE[1]).rotate(tick * 6.0)
        marks.append(mark)
    return marks


def ink(drawn, rule):
    """Alpha-carrying pixels left behind, to catch a shape the rasteriser abandoned."""
    blank()
    face.fill_rule = rule
    drawn()
    face.fill_rule = image.EVEN_ODD
    raw = bytes(face.raw)
    return sum(1 for i in range(3, len(raw), 4) if raw[i])


if HAS_COMBINE:
    print()
    print(f"{'the dial, 60 marks':<36} {'min':>9} {'med':>9}      ink")
    for name, big, small in MARK_STYLES:
        marks = dial_marks(big, small)
        one = shape.combine(marks)

        def draw_parts(n, marks=marks):
            for _ in range(n):
                blank()
                for mark in marks:
                    face.shape(mark)

        def draw_one(n, one=one):
            for _ in range(n):
                blank()
                face.fill_rule = image.NON_ZERO
                face.shape(one)
                face.fill_rule = image.EVEN_ODD

        apart = ink(lambda marks=marks: [face.shape(m) for m in marks], image.EVEN_ODD)
        joined = ink(lambda one=one: face.shape(one), image.NON_ZERO)
        measure(f"{name}, 60 draws", 10, draw_parts)
        measure(f"{name}, combined, 1 draw", 10, draw_one)
        print(f"{'':36} {'':>9} {'':>9}    {apart} vs {joined} px")


# -- the baked dial blitted, against drawing the combined one ---------------
# clockface.py bakes the dial once and blits it every frame. This is what that blit costs
# against rasterising the same marks, and the 110KB each cached dial holds.
if HAS_COMBINE:
    _big, _small = MARK_STYLES[0][1], MARK_STYLES[0][2]
    baked = image(SIZE, SIZE)
    baked.antialias = image.X4
    baked.pen = color.rgb(*MARKS)
    for _mark in dial_marks(_big, _small):
        baked.shape(_mark)
    BAKED_AT = vec2(0, 0)
    BARS = shape.combine(dial_marks(_big, _small))

    def blit_baked(n):
        for _ in range(n):
            screen.blit(baked, BAKED_AT)

    def draw_bars(n):
        screen.pen = color.rgb(*MARKS)
        screen.fill_rule = image.NON_ZERO
        for _ in range(n):
            screen.shape(BARS)
        screen.fill_rule = image.EVEN_ODD

    screen.antialias = image.X4
    print()
    print(f"{'the dial onto the screen':<36} {'min':>9} {'med':>9}")
    measure("blit the baked image", 100, blit_baked, 1.0, "us")
    measure("draw the combined bars", 100, draw_bars, 1.0, "us")
    print(f"{'':36} {SIZE * SIZE * 4 // 1024:>9}KB held per baked dial")


# -- whether a cached shape's per-draw cost stays flat ----------------------
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
