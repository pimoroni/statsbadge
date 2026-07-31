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
ICON_SIZE = 40

FACE = (245, 245, 242)
MARKS = (16, 16, 18)
HANDS = (222, 32, 28)
SECOND = (24, 24, 26)

CENTRE = (look.W // 2 - 62, look.BODY_TOP + look.BODY_H // 2)
RADIUS = 82

# Proportions of the radius. Hilfiker's dial is mostly about the weight of the marks
# against the width of the hands, so these are the whole design.
HOUR_MARK_LEN, HOUR_MARK_HALF = 0.19, 0.055
MIN_MARK_LEN, MIN_MARK_HALF = 0.095, 0.019
HOUR_HAND_LEN, HOUR_HAND_HALF = 0.55, 0.062
MIN_HAND_LEN, MIN_HAND_HALF = 0.86, 0.048
SEC_HAND_LEN, SEC_HAND_HALF = 0.76, 0.011
TAIL = 0.13

_face_cache = None
_hands_cache = None


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


def _bake_face():
    """The dial: white disc and sixty marks. Static, so it is baked once and blitted.

    Sixty anti-aliased bars costs milliseconds, which would be most of a frame every
    frame. Baked, the dial costs one small blit and only the hands are drawn live.
    Timed by tools/bench_clockface.py.
    """
    size = RADIUS * 2 + 4
    face = image(size, size)
    face.antialias = image.X4
    face.pen = brush.erase()
    face.rectangle(rect(0, 0, size, size))

    middle = (size / 2.0, size / 2.0)
    face.pen = color.rgb(*FACE)
    face.shape(shape.circle(vec2(*middle), RADIUS))

    # Two bars, re-aimed and drawn sixty times between them
    face.pen = color.rgb(*MARKS)
    hour_mark = _bar(RADIUS * (1.0 - HOUR_MARK_LEN), RADIUS * 0.97, RADIUS * HOUR_MARK_HALF)
    minute_mark = _bar(RADIUS * (1.0 - MIN_MARK_LEN), RADIUS * 0.97, RADIUS * MIN_MARK_HALF)
    for tick in range(60):
        face.shape(_aim(hour_mark if tick % 5 == 0 else minute_mark, middle, tick * 6.0))

    return face


def _bake_hands():
    """Hand geometry never changes, only the angle it is drawn at."""
    return (
        _bar(-RADIUS * TAIL, RADIUS * HOUR_HAND_LEN, RADIUS * HOUR_HAND_HALF),
        _bar(-RADIUS * TAIL, RADIUS * MIN_HAND_LEN, RADIUS * MIN_HAND_HALF),
        _bar(-RADIUS * TAIL, RADIUS * SEC_HAND_LEN, RADIUS * SEC_HAND_HALF),
    )


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


def render(_page, frame, _history, theme):
    global _face_cache, _hands_cache
    clock = frame.get("clock") or {}
    weather = frame.get("weather") or {}

    if _face_cache is None:
        _register_font()
        _face_cache = _bake_face()
        _hands_cache = _bake_hands()
    size = _face_cache.width
    screen.blit(_face_cache, vec2(int(CENTRE[0] - size / 2),
                                  int(CENTRE[1] - size / 2)))

    if clock.get("hour") is None:
        draw.blit_label("no time", look.SIZE_VALUE, theme.dim,
                        CENTRE[0], CENTRE[1] - 8, align=1)
    else:
        _resync(clock)
        hour, minute, second = _local_time()
        hour_hand, minute_hand, second_hand = _hands_cache
        _hand(hour_hand, (hour % 12) * 30.0 + minute * 0.5, HANDS)
        _hand(minute_hand, minute * 6.0 + second * 0.1, HANDS)
        _hand(second_hand, second * 6.0, SECOND)
        screen.pen = color.rgb(*SECOND)
        screen.shape(shape.circle(vec2(*CENTRE), 4))

    # The readouts beside the dial, in the badge's theme rather than the clock's.
    x = look.READOUT_X - 4
    y = look.BODY_TOP + 12
    if clock.get("time"):
        draw.blit_label(clock["time"], look.SIZE_BIG, theme.ink, x, y)
        y += 32
    if clock.get("date"):
        draw.blit_label(clock["date"], look.SIZE_SMALL, theme.dim, x, y)
        y += 24

    if weather.get("temp") is not None:
        # The scale comes with the reading; without one a number is just a number.
        unit = weather.get("temp_unit") or ""
        draw.blit_label("{:.0f}\u00b0{}".format(weather["temp"], unit),
                        look.SIZE_BIG, theme.ink, x, y)
        y += 30

    # The symbol, with the words for it underneath.
    condition = weather.get("condition")
    if condition:
        drawn = draw.blit_label(weather.get("icon") or "", ICON_SIZE, theme.ink,
                                x, y, name=WEATHER_FONT)
        if drawn:
            y += ICON_SIZE + 2
        draw.blit_label(condition, look.SIZE_SMALL, theme.dim, x, y)
        y += 19

    if weather.get("wind") is not None:
        draw.blit_label("wind {:.0f} {}".format(weather["wind"],
                                                weather.get("wind_unit") or ""),
                        look.SIZE_SMALL, theme.dim, x, y)
    elif not weather:
        draw.blit_label("no location set", look.SIZE_SMALL, theme.dim, x, y)


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
