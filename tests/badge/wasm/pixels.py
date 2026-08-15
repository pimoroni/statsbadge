"""Sampling the screen, for tests that measure what was drawn rather than read it.

Staged with the tests by tools/wasm/run.mjs. Not a test module itself: the runner runs
what is named test_*.py.
"""

import look


def body_pixels():
    """The page band, sampled. RGB only: alpha does not move here."""
    raw = screen.raw  # noqa: F821
    stride = look.W * 4
    out = bytearray()
    for y in range(look.BODY_TOP, look.BODY_TOP + look.BODY_H, 2):
        row = y * stride
        for x in range(0, look.W, 4):
            index = row + x * 4
            out.append(raw[index])
            out.append(raw[index + 1])
            out.append(raw[index + 2])
    return bytes(out)


def differing(first, second):
    """How much of the sampled band the two disagree on, 0 to 1."""
    moved = 0
    for index in range(0, len(first), 3):
        if first[index:index + 3] != second[index:index + 3]:
            moved += 1
    return moved / (len(first) / 3)
