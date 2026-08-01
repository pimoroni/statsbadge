"""The badge side of the clock extension: a Swiss railway station clock.

Installed into the app's `ext/` directory by `statsbadge install --with-extensions` and
imported by the app, which is when it registers itself.

This is the reason for shipping code rather than pictures: the second hand sweeps at
the badge's frame rate off one reading a second, where an image over the wire would
tick once a second and cost a fetch each time.

The Swiss station dial, as far as 320x240 allows: white face, sixty marks with the
five-minute bars about three times the width and twice the length of the others, and
blunt-ended hands reaching to the inner edge of the minute track. Red hands over a
black second hand follows the Mondaine colourway; Hilfiker's original had black hands
throughout. The dial keeps its own livery rather than the badge theme, because it is a
picture of a particular object and a black-on-amber railway clock is not that object,
but the readouts beside it stay themed.
"""

import machine
import time

import draw
import look
import pages

# The weather symbols this extension ships, pushed into ext/ beside this module and
# registered with draw under a name of their own: the app has an icons.af too, and a
# sprite cache keyed on the string alone would hand one font's glyph to the other.
WEATHER_FONT = "weather"
# The symbol shares a row with the temperature on every face, so it is sized to what the
# two of them together have room for.
ICON_SIZE = 32

# The app's own split layout: where its single gauge sits and how big it may be, so paging
# from a dial or a ring stack to a clock does not move the thing being looked at.
CENTRE = look.DIAL_C
RADIUS = look.DIAL_OUTER

# The dials, each a palette and a set of proportions of the radius. A dial is mostly the
# weight of its marks against the width of its hands, so these are the whole design.
#
#   plate    what the dial sits on: "disc", "squircle" or None for the page background
#   marks    "bars" for the railway's blocks, "dots" for a dotted minute track
#   star     a spike opposite each hand, which is what makes the Koppel hub a star
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
    # Nothing historic: the badge's own furniture, on the squircle the firmware draws.
    # Every colour None, so the dial is built out of the theme and sits against the page
    # the way the header and footer do - a fixed dark plate lands within a few counts of a
    # dark theme's background and turns to mud.
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

# Baked dials and hand geometry, per face: a page each side of the list can ask for a
# different one, and neither should pay for the other's bake. A themed dial depends on
# the theme, so the bakes are dropped when that changes rather than kept per theme -
# four faces across ten themes would be forty dials at 113KB each.
_face_cache = {}
_hands_cache = {}
_baked_for = None


def _colours(spec, theme):
    """A face's colours, with None meaning "whatever the theme says".

    panel for the plate, because that is what the header and footer are drawn in, so a
    themed dial sits against the page like the rest of the furniture.
    """
    return {
        "face": spec["face"] or theme.panel,
        "marks": spec["marks"] or theme.dim,
        "hands": spec["hands"] or theme.ink,
        "second": spec["second"] or theme.accent,
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

    Bars are built once and re-aimed rather than rebuilt, which is 653us against 958us
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


def _bake_face(spec, rgb):
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
    face.pen = color.rgb(*rgb["face"])
    if spec["plate"] == "squircle":
        face.shape(shape.squircle(vec2(*middle), RADIUS, 4))
    elif spec["plate"] == "disc":
        face.shape(shape.circle(vec2(*middle), RADIUS))

    face.pen = color.rgb(*rgb["marks"])
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
    """Hand geometry never changes, only the angle it is drawn at."""
    tail = spec["tail"]
    return tuple(
        _bar(-RADIUS * tail, RADIUS * length, RADIUS * half)
        for length, half in (spec["hour_hand"], spec["min_hand"], spec["sec_hand"])
    )


def _face(name, theme):
    """A face's colours, its baked dial and its hands, baking on first use."""
    global _baked_for
    if _baked_for != theme.name:
        _face_cache.clear()
        _hands_cache.clear()
        _baked_for = theme.name
    spec = FACES.get(name) or FACES[DEFAULT_FACE]
    rgb = _colours(spec, theme)
    key = spec["label"]
    if key not in _face_cache:
        _face_cache[key] = _bake_face(spec, rgb)
        _hands_cache[key] = _bake_hands(spec)
    return spec, rgb, _face_cache[key], _hands_cache[key]


def _hand(bar, degrees, rgb):
    screen.pen = color.rgb(*rgb)
    screen.shape(_aim(bar, CENTRE, degrees))


def _register_font():
    """Point draw at this extension's symbols. Called once, on the first render.

    The installed copy comes first for the reason draw.add_font describes, and this
    module's own directory second, for a checkout run over `mpremote mount`.
    """
    here = globals().get("__file__") or ""
    beside = here.rsplit("/", 1)[0] + "/icons.af" if "/" in here else "icons.af"
    draw.add_font(WEATHER_FONT, look.APP_DIR + "/ext/icons.af", beside)


def _digital(clock, weather, label, theme):
    """No dial: the whole band, laid out as a desk clock and drawn in the theme.

    The colon is drawn as its own label between the two pairs, because a proportional
    font kerns it into the digits and the whole point is that the numbers line up.
    """
    left, right = look.PAD + 2, look.W - look.PAD - 2
    top = look.BODY_TOP + 6

    if clock.get("date"):
        draw.blit_label(clock["date"], look.SIZE_VALUE, theme.dim, left, top)
    if label:
        draw.blit_label(label, look.SIZE_VALUE, theme.accent, right, top, align=2)

    text = clock.get("time") or "--:--"
    hours, _, minutes = text.partition(":")
    # As large as the band allows between the two rows, worked out rather than picked: a
    # label sprite stands size * 1.35 tall, and there is nothing else competing for the
    # middle of a digital face.
    digits_top = look.BODY_TOP + 26
    room = (look.BODY_TOP + look.BODY_H - 38) - digits_top
    size = int(room / 1.35)
    digits_left = draw.label(hours, size, theme.ink)
    digits_right = draw.label(minutes or "--", size, theme.ink)
    colon = draw.label(":", size, theme.accent)
    gap = 10
    total = digits_left.width + colon.width + digits_right.width + gap * 2
    x = (look.W - total) // 2
    y = digits_top
    screen.blit(digits_left, vec2(int(x), int(y)))
    screen.blit(colon, vec2(int(x + digits_left.width + gap), int(y)))
    screen.blit(digits_right,
                vec2(int(x + digits_left.width + colon.width + gap * 2), int(y)))

    # The weather along the bottom, symbol first so the eye lands on it.
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
    if weather.get("condition"):
        draw.blit_label(weather["condition"], look.SIZE_SMALL, theme.dim, x, y + 10)
    if weather.get("wind") is not None:
        draw.blit_label("wind {:.0f} {}".format(weather["wind"],
                                                weather.get("wind_unit") or ""),
                        look.SIZE_SMALL, theme.dim, right, y + 10, align=2)
    if not weather:
        draw.blit_label("no location set", look.SIZE_SMALL, theme.dim, right, y + 10,
                        align=2)


def render(page, frame, _history, theme):
    # A page can name its own place, and the host sends that location's clock along with
    # its weather - keyed by page id, so this side looks up what it is drawing rather than
    # deriving a key from a name that may not exist. Nothing there falls back to the
    # host's own clock and the default place.
    here = (frame.get("places") or {}).get((page or {}).get("id"))
    clock = here or frame.get("clock") or {}
    weather = here or frame.get("weather") or {}
    label = here.get("place") if here else None

    _register_font()
    chosen = ((page or {}).get("face") or DEFAULT_FACE)
    if chosen == "digital":
        _digital(clock, weather, label, theme)
        return

    spec, rgb, dial, hands = _face(chosen, theme)
    size = dial.width
    screen.blit(dial, vec2(int(CENTRE[0] - size / 2), int(CENTRE[1] - size / 2)))

    if clock.get("hour") is None:
        draw.blit_label("no time", look.SIZE_VALUE, theme.dim,
                        CENTRE[0], CENTRE[1] - 8, align=1)
    else:
        _resync(clock)
        hour, minute, second = _local_time()
        hour_hand, minute_hand, second_hand = hands
        _hand(hour_hand, (hour % 12) * 30.0 + minute * 0.5, rgb["hands"])
        _hand(minute_hand, minute * 6.0 + second * 0.1, rgb["hands"])
        _hand(second_hand, second * 6.0, rgb["second"])
        screen.pen = color.rgb(*rgb["second"])
        screen.shape(shape.circle(vec2(*CENTRE), spec["hub"]))

    # The readouts beside the dial, in the badge's theme rather than the clock's, and down
    # the app's own column so they line up with a gauge page's.
    x = look.READOUT_X
    # Which city this is, since the point of a second page is that it is elsewhere.
    y = draw.column_lines((
        (clock.get("time"), look.SIZE_BIG, theme.ink),
        (label, look.SIZE_SMALL, theme.accent),
        (clock.get("date"), look.SIZE_SMALL, theme.dim),
    ))
    y += 6

    # The symbol beside the reading, not above it: stacked, the column ran on to within a
    # few pixels of the page indicator.
    icon = weather.get("icon")
    if weather.get("temp") is not None or icon:
        drawn = draw.blit_label(icon or "", ICON_SIZE, theme.ink, x,
                                draw.icon_baseline(y, look.SIZE_BIG, ICON_SIZE),
                                name=WEATHER_FONT)
        if weather.get("temp") is not None:
            # The scale comes with the reading; without one a number is just a number.
            unit = weather.get("temp_unit") or ""
            draw.blit_label("{:.0f}\u00b0{}".format(weather["temp"], unit),
                            look.SIZE_BIG, theme.ink,
                            x + (drawn + 8 if drawn else 0), y)
        y += ICON_SIZE + 4

    wind = None
    if weather.get("wind") is not None:
        wind = "wind {:.0f} {}".format(weather["wind"], weather.get("wind_unit") or "")
    elif not weather:
        wind = "no location set"
    draw.column_lines(((weather.get("condition"), look.SIZE_SMALL, theme.dim),
                       (wind, look.SIZE_SMALL, theme.dim)), top=y)


# The badge's clock is set from the host once, and then left alone. A PCF85063A drifts a
# second or two in a day, where a reading is a second or two stale by the time it lands,
# so correcting against one costs more than it buys: the correction is a step backwards
# and the drift it chases is not there. Past this much disagreement something real has
# happened - a timezone change, or a clock that never got set - and it is set again.
RESYNC_S = 30

_synced = False

# Where the local second last changed, so a fraction of it can be worked out.
_phase_second = None
_phase_at = 0


def _local_time():
    """Hour, minute and a fractional second, from the badge's own clock.

    The hands run on hardware, not on the frame. `time.localtime()` costs 14us and its
    seconds arrive 1000ms apart, where a reading comes once a second at best and only when
    a poll spent itself on stats rather than on history or a layout. Reading the local
    clock is what makes the sweep even, and it keeps time if the host goes away.

    Whole seconds come from the clock and the fraction from the ticks since that second
    was seen to change. Clamped at one, so a clock that stops parks the hand rather than
    running it on past a second that never arrived.
    """
    global _phase_second, _phase_at
    parts = time.localtime()
    whole = parts[5]
    now = time.ticks_ms()
    if whole != _phase_second:
        _phase_second = whole
        _phase_at = now
    fraction = time.ticks_diff(now, _phase_at) / 1000.0
    return parts[3], parts[4], whole + min(1.0, fraction)


def _resync(clock):
    """Set the badge's clock from the host's, on the first reading and rarely after.

    The host is the authority, being the machine with a network time source, but a reading
    is stale and unevenly so by the time it arrives. Setting the clock from every one of
    them walks it backwards a second at a time, which is a hand that sweeps smoothly and
    then jumps: the correction, not the drift, is what is visible.

    So the first reading sets it and the rest are ignored until they disagree by RESYNC_S,
    which no amount of pipeline latency can account for.
    """
    global _synced
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
