"""The mark the app shows while it is still importing.

Shapes only, with no font and no icon file. This has to be on screen before draw, pages
and net are compiled, which is about 500ms from flash, and font.load another 107ms.

A module to itself, so the launcher icon can be generated from these numbers and a
screenshot taken without running the app. tools/icon.py reads them; tools/probe.py calls show().
"""

import look

OUTER = 62
INNER = 45
SWEEP = 0.7                       # how far round the dial the fill goes
BAR_W = 11
BAR_GAP = 7
BAR_HEIGHTS = (17, 30, 23)
BASE_BELOW_CENTRE = 14


def show():
    theme = look.get(look.DEFAULT)
    screen.pen = theme.bg
    screen.clear()

    centre = vec2(look.W // 2, look.H // 2)
    screen.pen = theme.grid
    screen.shape(shape.arc(centre, INNER, OUTER, look.DIAL_FROM, look.DIAL_TO))
    screen.pen = theme.accent
    sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * SWEEP
    screen.shape(shape.arc(centre, INNER, OUTER, look.DIAL_FROM, sweep))

    screen.pen = theme.ink
    span = 3 * BAR_W + 2 * BAR_GAP
    left = look.W // 2 - span // 2
    base = look.H // 2 + BASE_BELOW_CENTRE
    for i, height in enumerate(BAR_HEIGHTS):
        screen.rectangle(rect(left + i * (BAR_W + BAR_GAP), base - height,
                              BAR_W, height))
    display.update()
