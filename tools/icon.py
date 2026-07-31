"""Draw the 24x24 launcher icon on the badge and write it back to the host.

    mpremote connect PORT mount . run tools/icon.py

A tiny gauge sweep over three bars: the two things the app does, at 24 pixels.
"""

import sys

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

badge.mode(HIRES | VSYNC)

SIZE = 24
BG = (14, 14, 18)
ACCENT = (255, 138, 0)
HOT = (255, 60, 32)
COOL = (0, 190, 255)
INK = (240, 238, 235)

icon = image(SIZE, SIZE)
icon.antialias = image.X4
icon.pen = brush.erase()
icon.rectangle(rect(0, 0, SIZE, SIZE))

icon.pen = color.rgb(*BG)
icon.shape(shape.rounded_rectangle(rect(0, 0, SIZE, SIZE), 5))

# The gauge: a track, then a sweep from cool to hot, angles clockwise from the top.
centre = vec2(12, 13)
icon.pen = color.rgb(60, 60, 68)
icon.shape(shape.arc(centre, 6.5, 9.5, 150, 390))
icon.pen = brush.gradient(brush.LINEAR, 3, 20, 21, 5,
                          ((0.0, color.rgb(*COOL)),
                           (0.6, color.rgb(*ACCENT)),
                           (1.0, color.rgb(*HOT))))
icon.shape(shape.arc(centre, 6.5, 9.5, 150, 330))

# Three bars inside the dial, so it still reads as "stats" and not "a speedometer".
icon.pen = color.rgb(*INK)
for i, height in enumerate((3, 5, 4)):
    icon.rectangle(rect(9 + i * 3, 14 - height, 2, height))

with open("/remote/src/statsbadge/badge_app/icon.raw", "wb") as handle:
    handle.write(icon.raw)
print(f"wrote icon.raw, {SIZE * SIZE * 4} bytes")
