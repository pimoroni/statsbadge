"""The badge side of the clock extension.

Installed into the app's `ext/` directory by `statsbadge install`, and registers itself on
import.

`lcd` is DSEG7 Classic Bold, under the SIL Open Font License.
"""

import machine
import math
import time

import draw
import look
import pages

WEATHER_FONT = "weather"
ICON_SIZE = 32


def _high_low(weather):
    """The day's range as "H 24 L 12", or None."""
    unit = weather.get("temp_unit") or ""
    parts = []
    for mark, key in (("H", "high"), ("L", "low")):
        value = weather.get(key)
        if value is not None:
            parts.append(f"{mark} {value:.0f}\u00b0{unit}")
    return "   ".join(parts) or None

# Packed --wide --cap-from 8: DSEG7 has no H to measure a cap height from.
LCD_FONT = "lcd"
LCD_FILE = "lcd.af"

# Lexend's digits and colon, packed --wide. The app's narrow copy visibly flattens the
# counter of a nought at the 84pt this draws. Thirteen glyphs, 3KB: a space, ten digits,
# the colon, and an H that is never drawn. The H stands 648 units, the cap the font was
# packed to, so tools/read_af.py can check it.
DIGITS_FONT = "digits"
DIGITS_FILE = "digits.af"

CENTRE = look.DIAL_C
RADIUS = look.DIAL_OUTER

# Keys beyond the colours:
#
#   plate          what the dial sits on: "disc", "squircle", or None for the background
#   marks_style    "bars" for the railway's blocks, "dots" for a dotted minute track
#   star           a spike opposite each hand, as the Koppel hub has
#   *_mark/*_hand  (length, half-width), as fractions of RADIUS
FACES = {
    # Hilfiker's station clock in the Mondaine colourway.
    "railway": {
        "label": "Railway",
        "face": (245, 245, 242), "marks": (16, 16, 18),
        "hands": (222, 32, 28), "second": (24, 24, 26),
        "plate": "disc", "marks_style": "bars", "star": False,
        "hour_mark": (0.19, 0.055), "min_mark": (0.095, 0.019),
        "hour_hand": (0.55, 0.062), "min_hand": (0.86, 0.048),
        "sec_hand": (0.76, 0.011), "tail": 0.13, "hub": 4,
    },
    # Koppel's dial for Georg Jensen.
    "dots": {
        "label": "Dots",
        "face": (250, 250, 248), "marks": (18, 18, 20),
        "hands": (18, 18, 20), "second": (18, 18, 20),
        "plate": "disc", "marks_style": "dots", "star": True,
        "hour_mark": (0.0, 0.042), "min_mark": (0.0, 0.017),
        "hour_hand": (0.52, 0.019), "min_hand": (0.88, 0.015),
        "sec_hand": (0.88, 0.009), "tail": 0.24, "hub": 7,
    },
    # Colours from the theme: a fixed dark plate lands within a few counts of a dark
    # theme's background.
    "squircle": {
        "label": "Squircle",
        "face": None, "marks": None,
        "hands": None, "second": None,
        "plate": "squircle", "marks_style": "bars", "star": False,
        "hour_mark": (0.16, 0.030), "min_mark": (0.06, 0.012),
        "hour_hand": (0.52, 0.040), "min_hand": (0.84, 0.030),
        "sec_hand": (0.80, 0.010), "tail": 0.16, "hub": 5,
    },
}
DEFAULT_FACE = "railway"

# `ghost` is the unlit pair drawn behind the digits, a pair of eights for seven segments.
DIGITAL = {
    "digital": {"label": "Digital", "font": DIGITS_FONT, "file": DIGITS_FILE,
                "ghost": None, "colon": "dots"},
    "lcd": {"label": "Digital LCD", "font": LCD_FONT, "file": LCD_FILE,
            "ghost": "88", "colon": "glyph"},
}

# Cleared on a theme change: keyed by theme, ten themes would hold forty dials at 113KB each.
_face_cache = {}
_hands_cache = {}
_baked_for = None


def _colours(spec, theme):
    return {
        "face": color.rgb(*spec["face"]) if spec["face"] else theme.panel,
        "marks": color.rgb(*spec["marks"]) if spec["marks"] else theme.dim,
        "hands": color.rgb(*spec["hands"]) if spec["hands"] else theme.ink,
        "second": color.rgb(*spec["second"]) if spec["second"] else theme.accent,
    }


def _bar(inner, outer, half_width):
    """A blunt-ended bar pointing at twelve, from the origin for _aim to place."""
    return shape.rectangle(rect(-half_width, -outer, half_width * 2.0, outer - inner))


def _aim(bar, centre, degrees):
    """Point a bar at a clock angle. Translate before rotate, since each call right-multiplies.

    Re-aiming a baked bar beats rebuilding it, 653us against 958us a draw, but only while
    shape and mat3 each fit one GC block: MicroPython advances its free-block hint on
    single-block allocations only (py/gc.c, n_free == 1). True at 32-byte blocks and a
    six-float mat3. See tools/bench_clockface.py.
    """
    bar.transform = mat3().translate(centre[0], centre[1]).rotate(degrees)
    return bar


def _dot(radius_at, size):
    """A dot on the minute track, at twelve, for _aim to place."""
    return shape.circle(vec2(0, -radius_at), size)


def _bake_face(spec, pens):
    """The dial, baked per face: sixty anti-aliased marks cost most of a frame, every frame.

    Timed by tools/bench_clockface.py.
    """
    size = RADIUS * 2 + 4
    face = image(size, size)
    face.antialias = image.X4
    face.pen = brush.erase()
    face.rectangle(rect(0, 0, size, size))

    middle = (size / 2.0, size / 2.0)
    face.pen = pens["face"]
    if spec["plate"] == "squircle":
        face.shape(shape.squircle(vec2(*middle), RADIUS, 4))
    elif spec["plate"] == "disc":
        face.shape(shape.circle(vec2(*middle), RADIUS))

    face.pen = pens["marks"]
    hour_len, hour_half = spec["hour_mark"]
    min_len, min_half = spec["min_mark"]
    if spec["marks_style"] == "dots":
        track = RADIUS * 0.85
        big, small = _dot(track, RADIUS * hour_half), _dot(track, RADIUS * min_half)
        for tick in range(60):
            face.shape(_aim(big if tick % 5 == 0 else small, middle, tick * 6.0))
    else:
        hour_mark = _bar(RADIUS * (1.0 - hour_len), RADIUS * 0.97, RADIUS * hour_half)
        minute_mark = _bar(RADIUS * (1.0 - min_len), RADIUS * 0.97, RADIUS * min_half)
        for tick in range(60):
            face.shape(_aim(hour_mark if tick % 5 == 0 else minute_mark, middle,
                            tick * 6.0))

    return face


def _bake_hands(spec):
    tail = spec["tail"]
    return tuple(
        _bar(-RADIUS * tail, RADIUS * length, RADIUS * half)
        for length, half in (spec["hour_hand"], spec["min_hand"], spec["sec_hand"])
    )


def _face(name, theme):
    """(spec, pens, dial, hands), baking on first use."""
    global _baked_for
    if _baked_for != theme.key:
        _face_cache.clear()
        _hands_cache.clear()
        _baked_for = theme.key
    spec = FACES.get(name) or FACES[DEFAULT_FACE]
    pens = _colours(spec, theme)
    key = spec["label"]
    if key not in _face_cache:
        _face_cache[key] = _bake_face(spec, pens)
        _hands_cache[key] = _bake_hands(spec)
    return spec, pens, _face_cache[key], _hands_cache[key]


def _hand(bar, degrees, pen):
    screen.pen = pen
    screen.shape(_aim(bar, CENTRE, degrees))


def _register_font():
    """The installed copy first, then this module's directory for a checkout over
    `mpremote mount`."""
    here = globals().get("__file__") or ""
    beside = here.rsplit("/", 1)[0] + "/icons.af" if "/" in here else "icons.af"
    draw.add_font(WEATHER_FONT, look.APP_DIR + "/ext/icons.af", beside)


WIDEST_TIME = "44:44"

COLON_W, COLON_DOT = 0.20, 0.061
# Measured off the seven-segment face's colon with tools/read_af.py.
COLON_AT = (0.309, 0.720)

# Alpha floor at the half second, of 255.
COLON_DIM = 90


def _colon_alpha():
    lit = 0.5 + 0.5 * math.cos(_local_time()[2] % 1.0 * math.pi * 2.0)
    return int(COLON_DIM + (255 - COLON_DIM) * lit)


def _digits_font(spec):
    """The font name for a digital face, loading it on first use."""
    wanted = spec["font"]
    if not draw.has_font(wanted):
        here = globals().get("__file__") or ""
        beside = (here.rsplit("/", 1)[0] + "/" + spec["file"] if "/" in here
                  else spec["file"])
        draw.add_font(wanted, look.APP_DIR + "/ext/" + spec["file"], beside)
    return wanted if draw.has_font(wanted) else draw.TEXT


def _digital(clock, weather, label, theme, spec):
    """The band with no dial, laid out as a desk clock.

    Hours and minutes are drawn as separate strings, so a proportional font cannot kern the
    colon into them.
    """
    left, right = look.PAD + 2, look.W - look.PAD - 2
    top = look.BODY_TOP + 6

    if clock.get("date"):
        draw.blit_label(clock["date"], look.SIZE_VALUE, theme.dim, left, top)
    if label:
        draw.blit_label(label, look.SIZE_VALUE, theme.accent, right, top, align=2)

    text = clock.get("time") or "--:--"
    hours, _, minutes = text.partition(":")
    gap = 8
    digits_top = look.BODY_TOP + 26
    room = (look.BODY_TOP + look.BODY_H - 38) - digits_top
    # A digit stands draw.CAP of the requested size; the sprite is size * 1.35.
    size = int(room / draw.CAP)
    name = _digits_font(spec)
    dots = spec["colon"] == "dots"
    span = right - left
    widest = draw.text_width(WIDEST_TIME, size, name) + gap * 2
    if dots:
        widest += int(size * draw.CAP * COLON_W) - draw.text_width(":", size, name)
    if widest > span:
        size = int(size * span / widest)
    left_w = draw.text_width(hours, size, name)
    right_w = draw.text_width(minutes or "--", size, name)
    ink = int(size * draw.CAP)
    colon_w = int(ink * COLON_W) if dots else draw.text_width(":", size, name)
    # Left-justified, so the digits hold position from one minute to the next.
    x = left
    minutes_x = right - right_w
    y = digits_top + (room - ink) // 2 - (size - ink)
    ghosting = spec["ghost"] and name == spec["font"]
    if ghosting:
        draw.blit_label(spec["ghost"], size, theme.grid, x, y, name=name)
        draw.blit_label(spec["ghost"], size, theme.grid, minutes_x, y, name=name)
    draw.blit_label(hours, size, theme.ink, x, y, name=name)
    draw.blit_label(minutes or "--", size, theme.ink, minutes_x, y, name=name)
    colon_x = (x + left_w + minutes_x) / 2.0
    if dots:
        ink_top = y + size - ink
        screen.pen = theme.accent
        screen.alpha = _colon_alpha()
        for at in COLON_AT:
            screen.shape(shape.circle(vec2(colon_x, ink_top + ink * at), ink * COLON_DOT))
    else:
        colon_left = colon_x - colon_w / 2.0
        if ghosting:
            draw.blit_label(":", size, theme.grid, colon_left, y, name=name)
        screen.alpha = _colon_alpha()
        draw.blit_label(":", size, theme.accent, colon_left, y, name=name)
    screen.alpha = 255

    y = look.BODY_TOP + look.BODY_H - 34
    x = left
    icon = weather.get("icon")
    if icon:
        drawn = draw.blit_label(icon, ICON_SIZE, theme.ink, x,
                                draw.icon_baseline(y, look.SIZE_BIG, ICON_SIZE),
                                name=WEATHER_FONT)
        x += (drawn or 0) + 8
    if weather.get("temp") is not None:
        unit = weather.get("temp_unit") or ""
        x += draw.blit_label("{:.0f}\u00b0{}".format(weather["temp"], unit),
                             look.SIZE_BIG, theme.ink, x, y) + 12
    span = _high_low(weather)
    if weather.get("condition"):
        draw.blit_label(weather["condition"], look.SIZE_SMALL, theme.dim, x,
                        y + (2 if span else 10))
    if span:
        draw.blit_label(span, look.SIZE_SMALL, theme.dim, x,
                        y + (look.SIZE_SMALL + 6 if weather.get("condition") else 10))
    if weather.get("wind") is not None:
        draw.blit_label("wind {:.0f} {}".format(weather["wind"],
                                                weather.get("wind_unit") or ""),
                        look.SIZE_SMALL, theme.dim, right, y + 10, align=2)
    if not weather:
        draw.blit_label("no location set", look.SIZE_SMALL, theme.dim, right, y + 10,
                        align=2)


def render(page, frame, _history, theme):
    # `places` holds a clock and weather per location, keyed by page id.
    host = frame.get("clock") or {}
    here = (frame.get("places") or {}).get((page or {}).get("id"))
    # A location has no hour until its forecast lands.
    clock = here if (here or {}).get("hour") is not None else host
    weather = here or frame.get("weather") or {}
    label = here.get("place") if here else None

    _register_font()
    chosen = ((page or {}).get("face") or DEFAULT_FACE)
    if chosen in DIGITAL:
        # The colon beats on the badge's clock, which has to be set for it to beat in step.
        _resync(host, frame.get("seq"))
        _digital(clock, weather, label, theme, DIGITAL[chosen])
        return

    spec, pens, dial, hands = _face(chosen, theme)
    size = dial.width
    screen.blit(dial, vec2(int(CENTRE[0] - size / 2), int(CENTRE[1] - size / 2)))

    if clock.get("hour") is None:
        draw.blit_label("no time", look.SIZE_VALUE, theme.dim,
                        CENTRE[0], CENTRE[1] - 8, align=1)
    else:
        # One hardware clock, so it holds host time and a page elsewhere adds _zone_offset.
        _resync(host, frame.get("seq"))
        hour, minute, second = _local_time(_zone_offset(host, here))
        hour_hand, minute_hand, second_hand = hands
        _hand(hour_hand, (hour % 12) * 30.0 + minute * 0.5, pens["hands"])
        _hand(minute_hand, minute * 6.0 + second * 0.1, pens["hands"])
        _hand(second_hand, second * 6.0, pens["second"])
        screen.pen = pens["second"]
        screen.shape(shape.circle(vec2(*CENTRE), spec["hub"]))

    # The readout takes theme colours, down the app's column.
    x = look.READOUT_X
    y = draw.column_lines((
        (clock.get("time"), look.SIZE_BIG, theme.ink),
        (label, look.SIZE_SMALL, theme.accent),
        (clock.get("date"), look.SIZE_SMALL, theme.dim),
    ))
    y += 6

    icon = weather.get("icon")
    if weather.get("temp") is not None or icon:
        drawn = draw.blit_label(icon or "", ICON_SIZE, theme.ink, x,
                                draw.icon_baseline(y, look.SIZE_BIG, ICON_SIZE),
                                name=WEATHER_FONT)
        if weather.get("temp") is not None:
            unit = weather.get("temp_unit") or ""
            draw.blit_label("{:.0f}\u00b0{}".format(weather["temp"], unit),
                            look.SIZE_BIG, theme.ink,
                            x + (drawn + 8 if drawn else 0), y)
        y += ICON_SIZE + 4

    span = _high_low(weather)
    if span:
        draw.blit_label(span, look.SIZE_SMALL, theme.dim, x, y)
        y += look.SIZE_SMALL + 6

    wind = None
    if weather.get("wind") is not None:
        wind = "wind {:.0f} {}".format(weather["wind"], weather.get("wind_unit") or "")
    elif not weather:
        wind = "no location set"
    draw.column_lines(((weather.get("condition"), look.SIZE_SMALL, theme.dim),
                       (wind, look.SIZE_SMALL, theme.dim)), top=y)


# A PCF85063A drifts a second or two a day, about as stale as a reading by the time it
# lands.
RESYNC_S = 30

_synced = False
# The seq the last sync was measured against.
_synced_seq = None

# When the local second last changed, for the fraction within it.
_phase_second = None
_phase_at = 0


def _zone_offset(host, there):
    """Seconds between the host's local time and the location a page shows, within twelve
    hours of zero."""
    if not host or not there or there.get("hour") is None or host.get("hour") is None:
        return 0
    theirs = there["hour"] * 3600 + there["minute"] * 60 + there.get("seconds", 0)
    ours = host["hour"] * 3600 + host["minute"] * 60 + host.get("seconds", 0)
    return (theirs - ours + 43200) % 86400 - 43200


def _local_time(offset=0):
    """Hour, minute and a fractional second, off the badge's hardware clock.

    Whole seconds from time.localtime(), which costs 14us; the fraction from ticks since that
    second changed, clamped at one.
    """
    global _phase_second, _phase_at
    parts = time.localtime()
    whole = parts[5]
    now = time.ticks_ms()
    if whole != _phase_second:
        _phase_second = whole
        _phase_at = now
    fraction = min(1.0, time.ticks_diff(now, _phase_at) / 1000.0)
    at = (parts[3] * 3600 + parts[4] * 60 + whole + offset) % 86400
    return at // 3600, (at % 3600) // 60, at % 60 + fraction


def _resync(clock, seq=None):
    """Set the badge's clock from the host's: the first reading, then only past RESYNC_S.

    Setting it lands the sub-second at zero, which shows as a stumble in the sweep. `seq`
    gates on new readings, one poll's time being redrawn forty-five times a second.
    """
    global _synced, _synced_seq
    if _synced and seq == _synced_seq:
        return
    _synced_seq = seq
    hour = clock.get("hour")
    minute = clock.get("minute")
    second = clock.get("seconds")
    if hour is None or minute is None or second is None:
        return
    parts = time.localtime()
    theirs = hour * 3600 + minute * 60 + second
    ours = parts[3] * 3600 + parts[4] * 60 + parts[5]
    # Within twelve hours of zero.
    drift = (theirs - ours + 43200) % 86400 - 43200
    if _synced and -RESYNC_S <= drift <= RESYNC_S:
        return
    # (year, month, day, weekday, hour, minute, second, subsecond). The weekday is recomputed
    # from the date, so what goes in that slot does not matter.
    machine.RTC().datetime((parts[0], parts[1], parts[2], parts[6],
                            hour, minute, second, 0))
    _synced = True


pages.EXTRA["clockface"] = render
# Register the clock face as an animated page, so it is redrawn every frame.
pages.ANIMATED.add("clockface")
