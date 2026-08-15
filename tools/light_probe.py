"""Sample the light sensor under real conditions, for tuning auto-brightness.

    mpremote connect PORT mount . run tools/light_probe.py

A line a second. Hold it under a pillow, in the room, at a window and under a torch, a
few seconds in each. The panel is driven from the reading throughout, so the log shows
what the app would do. Ctrl-C to stop.

    raw      what badge.light_level() reads, meaned over a burst
    spread   high minus low across that burst, which is the sensor's noise
    ambient  where the follower has got to on 0-1
    duty     what the panel is actually lit at
    aims     times a second the panel was told to change, which is 0 for a settled room

What to look for: `aims` falling to 0 once a condition has settled, and `duty` under a pillow
being low enough to want in the dark.

Importing the app would run it - `main()` is called at the end of its __init__ - so the
settings are read out of that file and the follower is stepped here. `look` is imported flat,
the way tools/probe.py does, so ambient_fraction is the app's own.
"""

import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import look

badge.mode(HIRES | VSYNC)
badge.default_clear = None
BUTTON_HOME.irq(None)

# Something to light. A dark screen would understate what the panel contributes to its own
# reading, which is the thing worth knowing about under a pillow.
screen.pen = color.rgb(255, 255, 255)
screen.clear()
badge.update()

WANTED = ("BACKLIGHT_STEP", "BACKLIGHT_MS", "LIGHT_FOLLOW", "LIGHT_READS",
          "LIGHT_EVERY_MS")

# The app's own settings, so this cannot drift from what it ships with.
SETTINGS = {}
with open("/remote/src/statsbadge/badge_app/app.py") as handle:
    for line in handle:
        name, _, rest = line.partition(" = ")
        if name in WANTED:
            SETTINGS[name] = eval(rest.strip(), dict(SETTINGS))  # noqa: S307
missing = [name for name in WANTED if name not in SETTINGS]
if missing:
    raise SystemExit("not in the app any more, so update this probe: " + ", ".join(missing))

STEP = SETTINGS["BACKLIGHT_STEP"]
# The configured brightness this is sampling against, which is the app's default.
BRIGHTNESS = 0.8


def duty(fraction):
    """What the panel is lit at, for a 0-1 brightness. The panel resolves a byte of it and
    raises that to the power of 2.8 for the PWM level."""
    return (int(fraction * 255) / 255.0) ** 2.8
print(f"light floor {look.LIGHT_FLOOR:.2f}, {SETTINGS['LIGHT_READS']} reads a sample, "
      f"{SETTINGS['LIGHT_FOLLOW'] * 100:.0f}% of the gap every "
      f"{SETTINGS['LIGHT_EVERY_MS']}ms, brightness {BRIGHTNESS:.1f}")
print()
print(f"{'raw':<8} {'spread':<7} {'ambient':<8} {'duty%':<7} aims")

ambient = None
want = None
raw = spread = aims = 0
said = poll = time.ticks_ms()

while True:
    now = time.ticks_ms()
    if time.ticks_diff(now, poll) >= SETTINGS["LIGHT_EVERY_MS"]:
        poll = now
        total, low, high = 0, 65535, 0
        for _ in range(SETTINGS["LIGHT_READS"]):
            one = badge.light_level()
            total += one
            low = min(low, one)
            high = max(high, one)
        raw = total // SETTINGS["LIGHT_READS"]
        spread = high - low
        fraction = look.ambient_fraction(raw)
        if ambient is None:
            ambient = fraction
        else:
            ambient += (fraction - ambient) * SETTINGS["LIGHT_FOLLOW"]
        wanted = BRIGHTNESS * (look.LIGHT_FLOOR + (1 - look.LIGHT_FLOOR) * ambient)
        # The dead band the app applies: under one step of the byte the binding casts to,
        # setting the panel would set it to what it is already showing.
        if want is None or abs(wanted - want) >= STEP:
            want = wanted
            aims += 1
            display.backlight(want)

    if time.ticks_diff(now, said) >= 1000:
        said = now
        print(f"{raw:<8} {spread:<7} {ambient:<8.3f} {duty(want) * 100:<7.2f} {aims}")
        aims = 0
    badge.update()
