"""Find the dimmest backlight this panel is still readable at."""

import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import draw

badge.mode(HIRES | VSYNC)
badge.default_clear = None
BUTTON_HOME.irq(None)

draw.prepare()

# Every byte from here up. Below it the PWM level is under a thousandth and nothing has
# ever lit; above it the panel is plainly on.
FIRST = 8
LAST = 140
EVERY_MS = 500

INK = color.rgb(255, 255, 255)
PAPER = color.rgb(0, 0, 0)


def duty(byte):
    return (byte / 255.0) ** 2.8


def show(byte):
    """Draw something with fine detail and something with none, so "visible" and
    "readable" can be told apart.
    """
    screen.pen = PAPER
    screen.clear()
    draw.blit_label(f"{byte}", 44, INK, screen.width // 2, 24, 1)
    draw.blit_label(f"{duty(byte) * 100:.2f}% duty", 15, INK, screen.width // 2, 76, 1)
    draw.blit_label("A visible   B readable", 13, INK, screen.width // 2, 98, 1)
    screen.pen = INK
    screen.rectangle(rect(20, 118, screen.width - 40, 10))
    badge.update()


def sweep():
    """Step up a byte at a time until both presses are in, or the range runs out."""
    visible = readable = None
    for byte in range(FIRST, LAST + 1):
        display.backlight(byte / 255.0)
        end = time.ticks_add(time.ticks_ms(), EVERY_MS)
        while True:
            show(byte)
            if visible is None and badge.pressed(BUTTON_A):
                visible = byte
                print(f"  visible at byte {byte}, input {byte / 255.0:.3f}, "
                      f"{duty(byte) * 100:.2f}% duty")
            if badge.pressed(BUTTON_B):
                readable = byte
                print(f"  readable at byte {byte}, input {byte / 255.0:.3f}, "
                      f"{duty(byte) * 100:.2f}% duty")
                return visible, readable
            if time.ticks_diff(end, time.ticks_ms()) <= 0:
                break
    return visible, readable


print(f"stepping up from byte {FIRST}, {EVERY_MS}ms each, "
      f"so {(LAST - FIRST) * EVERY_MS // 1000}s if it runs to the end. "
      f"A when visible, B when readable. B ends the sweep.")
while True:
    print()
    visible, readable = sweep()
    if visible is None:
        print("  ran out of range without an A press")
    else:
        print(f"  this panel lights at byte {visible} of 255, "
              f"{duty(visible) * 100:.2f}% duty")
        print(f"  so the bottom {visible / 255 * 100:.0f}% of the input range does nothing")
        if readable is not None and readable > visible:
            print(f"  and it is readable from byte {readable}")
    print("  C to sweep again, HOME to stop")
    display.backlight(1.0)
    while True:
        if badge.pressed(BUTTON_C):
            break
        if badge.pressed(BUTTON_HOME):
            raise SystemExit
        badge.update()
