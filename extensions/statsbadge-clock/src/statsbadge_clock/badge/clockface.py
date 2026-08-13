"""The badge side of the clock extension: a Swiss railway station clock.

Installed into the app's `ext/` directory by `statsbadge install`, and registers itself
on import.

Code and not pictures: the second hand sweeps at the badge's frame rate off one reading
a second, where an image over the wire would tick once and cost a fetch each time.

The dials keep their own liveries, being pictures of particular objects; the readouts
beside them stay themed. `lcd` is DSEG7 Classic Bold, under the SIL Open Font License.

The badge's clock is set from the host once and then left alone; see RESYNC_S.
"""

import machine
import math
import time

import draw
import look
import pages

# The weather symbols this extension ships, under a name of their own: the sprite cache
# would otherwise hand one to the app's icons.af.
WEATHER_FONT = "weather"
# The symbol shares a row with the temperature on every face, and is sized to what the
# two of them have room for together.
ICON_SIZE = 32


def _high_low(weather):
    """The day's range, or None where the forecast carried neither end.

    One end alone is still worth drawing: a high with no low is a forecast that reaches
    this far, and blanking the line leaves the day with no shape on it at all.
    """
    unit = weather.get("temp_unit") or ""
    parts = []
    for mark, key in (("H", "high"), ("L", "low")):
        value = weather.get(key)
        if value is not None:
            parts.append(f"{mark} {value:.0f}\u00b0{unit}")
    return "   ".join(parts) or None

# DSEG7 Classic Bold, packed --wide --cap-from 8: a face with no H to measure a cap
# height from. Its digits stand where a capital does, so it drops in at the same size.
LCD_FONT = "lcd"
LCD_FILE = "lcd.af"

# Lexend's ten digits and a colon, packed --wide. The app's copy is narrow, which
# visibly flattens the counter of a nought at the 84pt this draws. Thirteen glyphs, 3KB.
DIGITS_FONT = "digits"
DIGITS_FILE = "digits.af"

# The app's split layout: paging from a dial to a clock leaves the subject put.
CENTRE = look.DIAL_C
RADIUS = look.DIAL_OUTER

# The dials, each a palette and proportions of the radius.
#
#   plate    what the dial sits on: "disc", "squircle" or None for the page background
#   marks    "bars" for the railway's blocks, "dots" for a dotted minute track
#   star     a spike opposite each hand, as the Koppel hub has
FACES = {
    # Hilfiker's station clock, in the Mondaine colourway: red hands over a black
    # second. The original had black hands throughout.
    "railway": {
        "label": "Railway",
        "face": (245, 245, 242), "marks": (16, 16, 18),
        "hands": (222, 32, 28), "second": (24, 24, 26),
        "plate": "disc", "marks_style": "bars", "star": False,
        "hour_mark": (0.19, 0.055), "min_mark": (0.095, 0.019),
        "hour_hand": (0.55, 0.062), "min_hand": (0.86, 0.048),
        "sec_hand": (0.76, 0.011), "tail": 0.13, "hub": 4,
    },
    # Koppel's dial for Georg Jensen: a dotted minute track, bigger dots at the five
    # minutes, needle hands with a spike opposite each so the hub reads as a star.
    "dots": {
        "label": "Dots",
        "face": (250, 250, 248), "marks": (18, 18, 20),
        "hands": (18, 18, 20), "second": (18, 18, 20),
        "plate": "disc", "marks_style": "dots", "star": True,
        "hour_mark": (0.0, 0.042), "min_mark": (0.0, 0.017),
        "hour_hand": (0.52, 0.019), "min_hand": (0.88, 0.015),
        "sec_hand": (0.88, 0.009), "tail": 0.24, "hub": 7,
    },
    # Every colour None, so this one is built out of the theme. A fixed dark plate lands
    # within a few counts of a dark theme's background and turns to mud.
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

# The faces with no dial. `ghost` is what an unlit pair looks like, a pair of eights for
# seven segments.
DIGITAL = {
    "digital": {"label": "Digital", "font": DIGITS_FONT, "file": DIGITS_FILE,
                "ghost": None, "colon": "dots"},
    "lcd": {"label": "Digital LCD", "font": LCD_FONT, "file": LCD_FILE,
            "ghost": "88", "colon": "glyph"},
}

# Baked dials and hand geometry, per face: a page each side of the list can ask for a
# different one. Dropped on a theme change rather than kept per theme, which would be
# forty dials at 113KB each across ten themes.
_face_cache = {}
_hands_cache = {}
_baked_for = None


def _colours(spec, theme):
    """A face's colours, with None meaning "whatever the theme says".

    The dials above carry theirs as plain palette data, and are built here. A
    theme's are already `color` objects.

    panel for the plate, because that is what the header and footer are drawn in, so a
    themed dial sits against the page like the rest of the furniture.
    """
    return {
        "face": color.rgb(*spec["face"]) if spec["face"] else theme.panel,
        "marks": color.rgb(*spec["marks"]) if spec["marks"] else theme.dim,
        "hands": color.rgb(*spec["hands"]) if spec["hands"] else theme.ink,
        "second": color.rgb(*spec["second"]) if spec["second"] else theme.accent,
    }


def _bar(inner, outer, half_width):
    """A blunt-ended bar pointing at twelve, measured out from the origin.

    Where it ends up is left to _aim, so one bar serves every angle it is drawn at.
    """
    return shape.rectangle(rect(-half_width, -outer, half_width * 2.0, outer - inner))


def _aim(bar, centre, degrees):
    """Point a bar at a clock angle.

    Angles run clockwise from twelve, which is how a clock is read and which way
    rotate() turns, so the angle goes in as it comes. Translate before rotate, because
    each call right-multiplies: the bar turns about the origin, then moves to centre.

    Bars are built once and re-aimed, never rebuilt, which is 653us against 958us
    a draw. That relies on shape and mat3 both fitting one GC block, since only
    single-block allocations advance MicroPython's free-block hint (py/gc.c,
    n_free == 1). It holds with 32-byte blocks and a six-float mat3; on a build with
    either of those changed, rebuilding each bar is the faster way round.
    See tools/bench_clockface.py.
    """
    bar.transform = mat3().translate(centre[0], centre[1]).rotate(degrees)
    return bar


def _dot(radius_at, size):
    """A dot on the minute track, at twelve, for _aim to point wherever it belongs."""
    return shape.circle(vec2(0, -radius_at), size)


def _bake_face(spec, pens):
    """The dial. Static, so it is baked once per face and blitted.

    Sixty anti-aliased marks costs milliseconds, which would be most of a frame every
    frame. Baked, the dial costs one small blit and only the hands are drawn live.
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
        # A dotted minute track: the five-minute dots larger, everything on one circle.
        track = RADIUS * 0.85
        big, small = _dot(track, RADIUS * hour_half), _dot(track, RADIUS * min_half)
        for tick in range(60):
            face.shape(_aim(big if tick % 5 == 0 else small, middle, tick * 6.0))
    else:
        # Two bars, re-aimed and drawn sixty times between them
        hour_mark = _bar(RADIUS * (1.0 - hour_len), RADIUS * 0.97, RADIUS * hour_half)
        minute_mark = _bar(RADIUS * (1.0 - min_len), RADIUS * 0.97, RADIUS * min_half)
        for tick in range(60):
            face.shape(_aim(hour_mark if tick % 5 == 0 else minute_mark, middle,
                            tick * 6.0))

    return face


def _bake_hands(spec):
    """Hand geometry holds still; only the angle it is drawn at moves."""
    tail = spec["tail"]
    return tuple(
        _bar(-RADIUS * tail, RADIUS * length, RADIUS * half)
        for length, half in (spec["hour_hand"], spec["min_hand"], spec["sec_hand"])
    )


def _face(name, theme):
    """A face's colours, its baked dial and its hands, baking on first use."""
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
    """Point draw at this extension's symbols. Called once, on the first render.

    The installed copy comes first for the reason draw.add_font describes, and this
    module's directory second, for a checkout run over `mpremote mount`.
    """
    here = globals().get("__file__") or ""
    beside = here.rsplit("/", 1)[0] + "/icons.af" if "/" in here else "icons.af"
    draw.add_font(WEATHER_FONT, look.APP_DIR + "/ext/icons.af", beside)


# What the digits are sized against: 4 is the widest in the app's face. The seven-segment
# face is monospaced, where any time measures the same.
WIDEST_TIME = "44:44"

# The dot colon, in fractions of the digit height: the column it takes, and a dot's radius.
COLON_W, COLON_DOT = 0.20, 0.061
# How far below the top of the digits the two dots sit. Measured off the seven-segment
# face's colon with tools/read_af.py, so paging between the digital faces leaves it put.
COLON_AT = (0.309, 0.720)

# How far the colon dims at the half second, of 255. A beat and not a blink, because a
# colon that goes out entirely reads as a fault.
COLON_DIM = 90


def _colon_alpha():
    """How lit the colon is, over one turn of the second.

    A sine off the badge's clock, the same reading the second hand sweeps on, so the beat
    lands on the second and not on whenever the page was first drawn.
    """
    lit = 0.5 + 0.5 * math.cos(_local_time()[2] % 1.0 * math.pi * 2.0)
    return int(COLON_DIM + (255 - COLON_DIM) * lit)


def _digits_font(spec):
    """The font a digital face sets its numbers in, loading it on first use.

    Both faces carry one, shipped beside this module, and an install predating the file
    falls back to the app's text font: the same numbers, coarser or without the segments.
    """
    wanted = spec["font"]
    if not draw.has_font(wanted):
        here = globals().get("__file__") or ""
        beside = (here.rsplit("/", 1)[0] + "/" + spec["file"] if "/" in here
                  else spec["file"])
        draw.add_font(wanted, look.APP_DIR + "/ext/" + spec["file"], beside)
    return wanted if draw.has_font(wanted) else draw.TEXT


def _digital(clock, weather, label, theme, spec):
    """The whole band with no dial, laid out as a desk clock and drawn in the theme.

    The two pairs are drawn as separate strings with the colon between them. A
    proportional font kerns a colon into the digits, and the point here is that the
    numbers line up.

    The seven-segment face has a colon sat where a display puts it. A text face gets two
    dots, its colon being typographic and on the baseline, which between numbers this
    size looks as though it has dropped off them.
    """
    left, right = look.PAD + 2, look.W - look.PAD - 2
    top = look.BODY_TOP + 6

    if clock.get("date"):
        draw.blit_label(clock["date"], look.SIZE_VALUE, theme.dim, left, top)
    if label:
        draw.blit_label(label, look.SIZE_VALUE, theme.accent, right, top, align=2)

    text = clock.get("time") or "--:--"
    hours, _, minutes = text.partition(":")
    # Sized by the ink, not the sprite: a digit stands draw.CAP of the size asked for, where
    # the sprite is size * 1.35, mostly room for a descender.
    gap = 8
    digits_top = look.BODY_TOP + 26
    room = (look.BODY_TOP + look.BODY_H - 38) - digits_top
    size = int(room / draw.CAP)
    # Measured against the widest time it could show, not the one it is showing. The digits
    # then hold their size from one minute to the next.
    name = _digits_font(spec)
    dots = spec["colon"] == "dots"
    span = right - left
    widest = draw.text_width(WIDEST_TIME, size, name) + gap * 2
    if dots:
        # Two dots take a column to themselves, narrower than a glyph colon's advance.
        widest += int(size * draw.CAP * COLON_W) - draw.text_width(":", size, name)
    if widest > span:
        size = int(size * span / widest)
    left_w = draw.text_width(hours, size, name)
    right_w = draw.text_width(minutes or "--", size, name)
    ink = int(size * draw.CAP)
    colon_w = int(ink * COLON_W) if dots else draw.text_width(":", size, name)
    # Justified, not centred. In a proportional face 10:09 is 89% of the width of 44:44,
    # which centring would inset 16px each side.
    x = left
    minutes_x = right - right_w
    # The sprite's baseline sits `size` from its top, which the second term takes off.
    y = digits_top + (room - ink) // 2 - (size - ink)
    # The unlit segments first, as a real display shows them.
    ghosting = spec["ghost"] and name == spec["font"]
    if ghosting:
        draw.blit_label(spec["ghost"], size, theme.grid, x, y, name=name)
        draw.blit_label(spec["ghost"], size, theme.grid, minutes_x, y, name=name)
    draw.blit_label(hours, size, theme.ink, x, y, name=name)
    draw.blit_label(minutes or "--", size, theme.ink, minutes_x, y, name=name)
    # Between the two, wherever justifying them left the middle, beating the second.
    colon_x = (x + left_w + minutes_x) / 2.0
    if dots:
        ink_top = y + size - ink
        screen.pen = theme.accent
        screen.alpha = _colon_alpha()
        for at in COLON_AT:
            screen.shape(shape.circle(vec2(colon_x, ink_top + ink * at), ink * COLON_DOT))
    else:
        colon_left = colon_x - colon_w / 2.0
        # Its unlit segments too, so what dims is the colon and not the gap it leaves.
        if ghosting:
            draw.blit_label(":", size, theme.grid, colon_left, y, name=name)
        screen.alpha = _colon_alpha()
        draw.blit_label(":", size, theme.accent, colon_left, y, name=name)
    screen.alpha = 255

    # The weather along the bottom, symbol first.
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
    # The condition names what it is doing, the range how far the day goes, stacked beside
    # the temperature so the row stays as tall as the number it sits next to.
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
    # A place's clock arrives with its weather, keyed by page id; empty falls back to the
    # host's.
    host = frame.get("clock") or {}
    here = (frame.get("places") or {}).get((page or {}).get("id"))
    # The offset comes from the forecast: a place has no time until that lands.
    clock = here if (here or {}).get("hour") is not None else host
    weather = here or frame.get("weather") or {}
    label = here.get("place") if here else None

    _register_font()
    chosen = ((page or {}).get("face") or DEFAULT_FACE)
    if chosen in DIGITAL:
        # The numbers come from the host, but the colon beats on the badge's clock: never
        # set, it keeps 1Hz against a second unrelated to the time on the screen.
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
        # From the host's clock only: there is one hardware clock, and two pages in two zones
        # would each set it to theirs. A page elsewhere adds the difference, which is in the
        # frame already.
        _resync(host, frame.get("seq"))
        hour, minute, second = _local_time(_zone_offset(host, here))
        hour_hand, minute_hand, second_hand = hands
        _hand(hour_hand, (hour % 12) * 30.0 + minute * 0.5, pens["hands"])
        _hand(minute_hand, minute * 6.0 + second * 0.1, pens["hands"])
        _hand(second_hand, second * 6.0, pens["second"])
        screen.pen = pens["second"]
        screen.shape(shape.circle(vec2(*CENTRE), spec["hub"]))

    # In the badge's theme rather than the clock's, down the app's column.
    x = look.READOUT_X
    y = draw.column_lines((
        (clock.get("time"), look.SIZE_BIG, theme.ink),
        (label, look.SIZE_SMALL, theme.accent),
        (clock.get("date"), look.SIZE_SMALL, theme.dim),
    ))
    y += 6

    # Beside the reading: stacked, the column ran to within a few pixels of the page pips.
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

    # The day's range under the number it is the range of, so the reading beside the symbol
    # reads as now and this as the rest of the day. Lined up with the temperature and not
    # the symbol, the symbol being taller than the text beside it.
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


# How far the clocks may disagree before the badge's is set again. A PCF85063A drifts a
# second or two a day, which is also how stale a reading is by the time it lands.
RESYNC_S = 30

_synced = False
# The reading the last sync was considered against; a redraw does not count as one.
_synced_seq = None

# Where the local second last changed, for working out a fraction of it.
_phase_second = None
_phase_at = 0


def _zone_offset(host, there):
    """Seconds between the host's local time and the place a page is showing.

    Worked out from the two readings in the frame and not from a stored offset, so it
    costs nothing and cannot go stale, and the shortest way round the day so that either side
    of midnight is not twenty-three hours.
    """
    if not host or not there or there.get("hour") is None or host.get("hour") is None:
        return 0
    theirs = there["hour"] * 3600 + there["minute"] * 60 + there.get("seconds", 0)
    ours = host["hour"] * 3600 + host["minute"] * 60 + host.get("seconds", 0)
    return (theirs - ours + 43200) % 86400 - 43200


def _local_time(offset=0):
    """Hour, minute and a fractional second, off the badge's clock.

    The hands run on hardware, not on the frame. `time.localtime()` costs 14us and its
    seconds arrive 1000ms apart, where a reading comes once a second at best and only when
    a poll spent itself on stats and not on history or a layout. Reading the local
    clock is what makes the sweep even, and it keeps time if the host goes away.

    Whole seconds come from the clock and the fraction from the ticks since that second
    was seen to change. Clamped at one, so a clock that stops parks the hand instead of
    running it past a second that never arrived.
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
    """Set the badge's clock from the host's, on the first reading and rarely after.

    The host is the authority, being the machine with a network time source, but a reading
    is stale and unevenly so by the time it arrives. Setting the clock from every one of
    them walks it backwards a second at a time, which is a hand that sweeps smoothly and
    then jumps: the correction, not the drift, is what is visible.

    So the first reading sets it and the rest are ignored until they disagree by RESYNC_S,
    which no amount of pipeline latency can account for.

    Only new readings count. A frame is drawn forty-five times a second and holds the time
    it was polled at throughout, so with the host away the badge's clock runs on against a
    frozen one. Left alone, the disagreement reaches RESYNC_S after half a minute and the
    hands are dragged back to where the last poll left them, every thirty seconds after.

    Setting the clock also lands the sub-second at zero, restarting the sweep part-way
    through a second, so a sync nobody needed shows as a stumble.
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
    # Shortest way round the day, so either side of midnight is not a 24 hour drift
    drift = (theirs - ours + 43200) % 86400 - 43200
    if _synced and -RESYNC_S <= drift <= RESYNC_S:
        return
    # (year, month, day, weekday, hour, minute, second, subsecond). The weekday is
    # recomputed from the date, so what goes in that slot does not matter.
    machine.RTC().datetime((parts[0], parts[1], parts[2], parts[6],
                            hour, minute, second, 0))
    _synced = True


pages.EXTRA["clockface"] = render
# A sweeping hand needs a frame even when no new data arrived, so the app has to know
# not to skip the redraw.
pages.ANIMATED.add("clockface")
