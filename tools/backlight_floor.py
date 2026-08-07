"""Find the dimmest backlight this panel is still readable at.

    mpremote connect PORT mount . run tools/backlight_floor.py

In a dark room, with the sensor covered or the room genuinely dark. It steps the panel up
from off, a byte at a time, half a second each, with something on screen worth trying to
read. Press:

    A   the moment anything at all is visible
    B   when it is comfortable to read

Those two are what BACKLIGHT_FLOOR sits between: it is meant to be the dimmest input that
lights the panel, and everything a brightness setting can ask for is measured up from it.
The driver raises its input to the power of 2.8 for the PWM level, so the byte and the duty
are both printed - the duty is what the panel is lit at, the byte is what to set.

C starts again from the bottom, for a second opinion once your eyes have adjusted.
"""

import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import draw

badge.mode(HIRES | VSYNC)
badge.default_clear = None
BUTTON_HOME.irq(None)

draw.prepare()

# Every byte from here up. Below it the PWM level is under a thousandth and nothing has ever
# lit; above it the panel is plainly on and there is nothing left to find out.
FIRST = 8
LAST = 140
EVERY_MS = 500

INK = color.rgb(255, 255, 255)
PAPER = color.rgb(0, 0, 0)


def duty(byte):
    return (byte / 255.0) ** 2.8


def show(byte):
    """Something with fine detail and something with none, so "visible" and "readable" can
    be told apart: the bar is lit long before the words come up."""
    screen.pen = PAPER
    screen.clear()
    draw.blit_label(f"{byte}", 44, INK, screen.bounds.w // 2, 24, 1)
    draw.blit_label(f"{duty(byte) * 100:.2f}% duty", 15, INK, screen.bounds.w // 2, 76, 1)
    draw.blit_label("A visible   B readable", 13, INK, screen.bounds.w // 2, 98, 1)
    screen.pen = INK
    screen.rectangle(rect(20, 118, screen.bounds.w - 40, 10))
    badge.update()


def sweep():
    visible = readable = None
    for byte in range(FIRST, LAST + 1):
        display.backlight(byte / 255.0)
        show(byte)
        end = time.ticks_add(time.ticks_ms(), EVERY_MS)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            if visible is None and badge.pressed(BUTTON_A):
                visible = byte
                print(f"  visible at byte {byte}, input {byte / 255.0:.3f}, "
                      f"{duty(byte) * 100:.2f}% duty")
            if badge.pressed(BUTTON_B):
                readable = byte
                print(f"  readable at byte {byte}, input {byte / 255.0:.3f}, "
                      f"{duty(byte) * 100:.2f}% duty")
                return visible, readable
    return visible, readable


print(f"stepping up from byte {FIRST}, {EVERY_MS}ms each. "
      f"A when visible, B when readable.")
while True:
    print()
    visible, readable = sweep()
    if readable is None:
        print("  ran out of range without a B press")
    else:
        print(f"  so BACKLIGHT_FLOOR wants to be about {readable / 255.0:.2f}")
    print("  C to sweep again, HOME to stop")
    display.backlight(1.0)
    while True:
        if badge.pressed(BUTTON_C):
            break
        if badge.pressed(BUTTON_HOME):
            raise SystemExit
        badge.update()
