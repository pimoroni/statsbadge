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

import math

import draw
import look
import pages

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


def _blunt(centre, degrees, inner, outer, half_width):
    """A blunt-ended bar along a clock angle, as one four-point contour.

    Angles run clockwise from twelve, which is how a clock is read, so no conversion
    is needed. Built from points rather than a rotated rectangle because a mat3 chain
    allocates once per call and this runs sixty times while baking the dial.
    """
    radians = math.radians(degrees)
    along_x, along_y = math.sin(radians), -math.cos(radians)
    across_x, across_y = math.cos(radians), math.sin(radians)
    cx, cy = centre
    return [
        vec2(cx + along_x * outer - across_x * half_width,
             cy + along_y * outer - across_y * half_width),
        vec2(cx + along_x * outer + across_x * half_width,
             cy + along_y * outer + across_y * half_width),
        vec2(cx + along_x * inner + across_x * half_width,
             cy + along_y * inner + across_y * half_width),
        vec2(cx + along_x * inner - across_x * half_width,
             cy + along_y * inner - across_y * half_width),
    ]


def _bake_face():
    """The dial: white disc and sixty marks. Static, so it is baked once and blitted.

    Sixty anti-aliased bars is about 15ms, which would be most of a frame every frame.
    Baked, the dial costs one small blit and only the hands are drawn live.
    """
    size = RADIUS * 2 + 4
    face = image(size, size)
    face.antialias = image.X4
    face.pen = brush.erase()
    face.rectangle(rect(0, 0, size, size))

    middle = (size / 2.0, size / 2.0)
    face.pen = color.rgb(*FACE)
    face.shape(shape.circle(vec2(*middle), RADIUS))

    # All sixty marks in one contour set under NON_ZERO: one shape, one setup cost.
    face.fill_rule = image.NON_ZERO
    face.pen = color.rgb(*MARKS)
    minutes = []
    hours = []
    for tick in range(60):
        degrees = tick * 6.0
        if tick % 5 == 0:
            hours.append(_blunt(middle, degrees,
                                RADIUS * (1.0 - HOUR_MARK_LEN), RADIUS * 0.97,
                                RADIUS * HOUR_MARK_HALF))
        else:
            minutes.append(_blunt(middle, degrees,
                                  RADIUS * (1.0 - MIN_MARK_LEN), RADIUS * 0.97,
                                  RADIUS * MIN_MARK_HALF))
    face.shape(shape.custom(*minutes))
    face.shape(shape.custom(*hours))
    face.fill_rule = image.EVEN_ODD
    return face


def _hand(centre, degrees, length, half_width, rgb, tail=TAIL):
    screen.pen = color.rgb(*rgb)
    screen.shape(shape.custom(_blunt(centre, degrees, -RADIUS * tail,
                                     RADIUS * length, RADIUS * half_width)))


def render(_page, frame, _history, theme):
    global _face_cache
    clock = frame.get("clock") or {}
    weather = frame.get("weather") or {}

    if _face_cache is None:
        _face_cache = _bake_face()
    size = _face_cache.width
    screen.blit(_face_cache, vec2(int(CENTRE[0] - size / 2),
                                  int(CENTRE[1] - size / 2)))

    hour = clock.get("hour")
    minute = clock.get("minute")
    if hour is None or minute is None:
        draw.blit_label("no time", look.SIZE_VALUE, theme.dim,
                        CENTRE[0], CENTRE[1] - 8, align=1)
    else:
        second = _smooth_second(clock.get("seconds"))
        _hand(CENTRE, (hour % 12) * 30.0 + minute * 0.5,
              HOUR_HAND_LEN, HOUR_HAND_HALF, HANDS)
        _hand(CENTRE, minute * 6.0 + second * 0.1,
              MIN_HAND_LEN, MIN_HAND_HALF, HANDS)
        _hand(CENTRE, second * 6.0, SEC_HAND_LEN, SEC_HAND_HALF, SECOND)
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
        draw.blit_label("{:.0f}".format(weather["temp"]), look.SIZE_BIG, theme.ink, x, y)
        y += 30
    if weather.get("condition"):
        draw.blit_label(weather["condition"], look.SIZE_SMALL, theme.dim, x, y)
        y += 19
    if weather.get("wind") is not None:
        draw.blit_label("wind {:.0f}".format(weather["wind"]), look.SIZE_SMALL, theme.dim, x, y)
    elif not weather:
        draw.blit_label("no location set", look.SIZE_SMALL, theme.dim, x, y)


_last_second = None
_last_second_at = 0


def _smooth_second(seconds):
    """Carry the second hand between polls.

    The host reports a whole second once a second. Advancing it locally from the frame
    clock is what makes the hand sweep instead of stepping, and is the reason this page
    is code on the badge. It only ever moves forward within a second, so a late or
    repeated reading cannot make the hand jump backwards.
    """
    global _last_second, _last_second_at
    if seconds is None:
        return 0.0
    if seconds != _last_second:
        _last_second = seconds
        _last_second_at = badge.ticks
    elapsed = min(1.0, max(0.0, (badge.ticks - _last_second_at) / 1000.0))
    return (seconds + elapsed) % 60.0


pages.EXTRA["clockface"] = render
# A sweeping hand needs a frame even when no new data arrived, so the app has to know
# not to skip the redraw.
pages.ANIMATED.add("clockface")
