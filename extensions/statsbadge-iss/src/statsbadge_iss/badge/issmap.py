"""The badge side of the ISS extension: the station, its track, and the terminator.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
which is when it registers itself.

The map and the day and night wash are `worldmap`, which is the app's. What is here is the
station: an orbit of ground track with the flown half drawn behind it, a marker that says
whether it is in sunlight, and the readouts under the map.

Why this is code and not a picture over the wire: the station moves 0.066 degrees a second
and the terminator a quarter of a degree a minute, so both are carried forward from a reading
that arrives every five seconds. A picture would cost a fetch a frame to do the same.
"""

from array import array

import draw
import look
import pages
import worldmap

# The map takes the page's band less a strip that says where the station is. The header and
# footer stay where every other page has them.
BAND_H = 34
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
BAND_TOP = MAP_TOP + MAP_H

# Pixels per degree. The whole world is 360 degrees across 320 pixels, and the poles go past
# the band either way: the station never leaves 51.6 degrees, so what is cropped is ice.
SCALE_WORLD = look.W / 360.0
# Closed in, for the camera that travels with it.
SCALE_FOLLOW = 1.6

# How wide the track is drawn, flown and still to come.
TRACK_W = 1.6
# How many points the curve is drawn with between each pair the host sent. Five minutes of
# orbit is twenty degrees of longitude, so four is a chord of five: past the point where a
# curve reads as one.
TRACK_STEPS = 4
# What is left of the flown half, against the accent the part ahead is drawn in. It has been
# and gone, so it is there to give the marker somewhere to have come from.
FLOWN_ALPHA = 96
# What the part in shadow keeps, so the track says where the station is in daylight and the
# terminator says why.
DARK_ALPHA = 110

# The marker: a body and two solar panels, in degrees of arc so it holds its size on screen
# whatever the zoom. Drawn rather than blitted, so it takes the theme and can point along the
# track.
BODY = 3.0
PANEL_LONG = 7.0
PANEL_SHORT = 1.6
# The halo behind it, which is what makes a 12px marker read as the subject of the page.
HALO = 6.0

# Where each page is looking, keyed by page id.
_state = {}
# The track projected to the screen, x and y a point, grown once and reused every frame.
_path = array("f", b"")
# The marker's four shapes, built on the first frame that draws them.
_parts = None


def _page_state(page):
    key = (page or {}).get("id") or "iss"
    state = _state.get(key)
    if state is None:
        state = _state[key] = {
            "view": worldmap.View(MAP_TOP, MAP_H, lon=0.0, lat=0.0, scale=SCALE_WORLD),
        }
    return state


def _marker(theme, view, where):
    """The station: a lit marker in sunlight, a quiet one in shadow, over a soft halo."""
    x, y = view.at(where["lon"], where["lat"])
    sunlit = where.get("sunlit", True)
    pen = theme.accent if sunlit else theme.dim
    was = screen.clip
    screen.clip = view.box

    # Built once and re-aimed: the marker is the same size every frame, and a circle is a path
    # of thirty-odd points to allocate for the sake of moving it a pixel.
    global _parts
    if _parts is None:
        _parts = (shape.circle(vec2(0, 0), HALO),
                  shape.rectangle(rect(-PANEL_LONG, -PANEL_SHORT * 0.5,
                                       PANEL_LONG * 2.0, PANEL_SHORT)),
                  shape.circle(vec2(0, 0), BODY),
                  shape.circle(vec2(0, 0), BODY * 0.45))
    halo, panels, body, pupil = _parts
    at = mat3().translate(x, y)
    for part in _parts:
        part.transform = at

    screen.pen = pen.with_alpha(60)
    screen.shape(halo)
    screen.pen = pen
    screen.shape(panels)
    screen.shape(body)
    screen.pen = theme.bg if sunlit else theme.panel
    screen.shape(pupil)
    screen.clip = was


def _smoothed(points):
    """The track resampled to a curve through every point the host sent.

    Five minutes of orbit is twenty-odd degrees of longitude, so the samples drawn as chords
    read as a fan of straight lines. Catmull-Rom passes through each of them, which is the
    same curve the graph pages are drawn with, and an orbit is exactly the smooth thing it
    suits: nothing moves, the corners just stop being corners.

    Longitude is unwrapped first. The run crosses the date line, and a spline through 179 then
    -179 would swing back round the whole world to get there.
    """
    lons, lats, lit = [], [], []
    turns = 0.0
    for lon, lat, sunlit in points:
        if lons and abs(lon + turns - lons[-1]) > 180.0:
            turns += 360.0 if lon + turns < lons[-1] else -360.0
        lons.append(lon + turns)
        lats.append(lat)
        lit.append(sunlit)
    if len(lons) < 3:
        return list(zip(lons, lats, lit))
    dense_lon = draw.curve(lons, TRACK_STEPS)
    dense_lat = draw.curve(lats, TRACK_STEPS)
    # A point takes the light of the sample it came from, so a crossing lands on a sample
    # rather than somewhere the host never reported.
    return [(dense_lon[index], dense_lat[index],
             lit[min(len(lit) - 1, index // TRACK_STEPS)])
            for index in range(len(dense_lon))]


def _track(theme, view, state, points, flown):
    """The ground track, an orbit of it, with the part already flown left behind.

    Split at `flown`, which is where the host says now is in the run: the points are a
    prediction made when it was asked for, so the station walks along them between fetches.

    Drawn as a stroked path per stretch rather than a shape per segment: one open contour is
    0.08ms plus its edges where seventy-six lines are 0.08ms each.
    """
    if len(points) < 2:
        return
    # The curve only changes when the host sends a new run, which is every two minutes.
    key = (len(points), points[0], points[-1])
    if state.get("curve_for") != key:
        state["curve_for"] = key
        state["curve"] = _smoothed(points)
    dense = state["curve"]
    cut = max(0, min(len(dense) - 1, int(flown * TRACK_STEPS)))

    # Nothing about the drawn track changes between most frames: the run arrives every two
    # minutes, the split moves every thirty seconds, and on the whole-world camera the projection
    # never moves at all. So the stroked shapes are kept and only rebuilt when one of those
    # actually moves - the camera to the nearest pixel, since below that it is the same picture.
    # Stroking is the dear half: an open contour of 77 points becomes an outline of four times
    # that, six times a frame.
    drawn_for = (state["curve_for"], cut, int(view.lon * view.scale),
                 int(view.lat * view.scale), int(view.scale * 100.0))
    if state.get("runs_for") != drawn_for:
        state["runs_for"] = drawn_for
        state["runs"] = _project(view, dense, cut)

    was = screen.clip
    screen.clip = view.box
    for trace, ahead, sunlit in state["runs"]:
        if ahead:
            screen.pen = theme.accent.with_alpha(255 if sunlit else DARK_ALPHA)
        else:
            screen.pen = theme.accent_b.with_alpha(
                FLOWN_ALPHA if sunlit else FLOWN_ALPHA // 2)
        screen.shape(trace)
    screen.clip = was


def _project(view, dense, cut):
    """The track as stroked shapes, one per run of it that is drawn the same way.

    Projected into one buffer that outlives the frame and stroked out of slices of it: a vec2 a
    point and a list a run would be 77 objects, which is the sort of allocation that leaves a heap
    in pieces. Same idiom as draw.line, which strokes a plot out of `draw._points`.
    """
    global _path
    wanted = len(dense) * 2
    if len(_path) < wanted:
        _path = array("f", bytes(wanted * 4 + 64))
    bounds = []
    stretch = None
    start = 0
    at = 0
    previous_x = None
    for index, (lon, lat, sunlit) in enumerate(dense):
        # What this point is drawn as, rather than the pen itself: a run ends where the answer
        # changes, and comparing two colours is not something a pen can be asked.
        want = (index >= cut, bool(sunlit))
        x, y = view.at(lon, lat)
        # A jump wider than half the screen is the seam, not a move.
        seam = previous_x is not None and abs(x - previous_x) > look.W * 0.5
        if at and (seam or want != stretch):
            bounds.append((start, at, stretch))
            # A change of colour carries the last point over, so the join is drawn; a seam does
            # not, because the two ends are not next to each other.
            start = at if seam else at - 2
        stretch = want
        _path[at] = x
        _path[at + 1] = y
        at += 2
        previous_x = x
    bounds.append((start, at, stretch))

    held = memoryview(_path)
    runs = []
    for start, stop, run in bounds:
        if stop - start < 4 or run is None:
            continue
        trace = shape.custom(held[start:stop])
        trace.stroke(TRACK_W, draw.LINE_FLAGS)
        runs.append((trace, run[0], run[1]))
    return runs


def _band(theme, where, aboard, note="waiting for the feed"):
    """The strip under the map: how high, how fast, in sun or shadow, and who is aboard."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, BAND_TOP, look.W, BAND_H))
    # The same rule the header draws, in the same colour, so the band reads as furniture.
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, BAND_TOP, look.W, 1))
    if not where:
        if note:
            draw.blit_label(note, look.SIZE_VALUE, theme.dim, look.PAD, BAND_TOP + 9)
        return

    unit = where.get("unit") or "km"
    altitude = f"{where['altitude']:.0f} {unit}" if where.get("altitude") else "--"
    draw.blit_label(altitude, look.SIZE_BIG, theme.ink, look.PAD, BAND_TOP + 3)
    left = look.PAD + draw.text_width(altitude, look.SIZE_BIG) + 10

    # Sunlight is the one thing here that is a state rather than a number, so it takes the
    # accent when it is on and the dim when it is not.
    sunlit = where.get("sunlit", True)
    lit = "in sunlight" if sunlit else "in shadow"
    pen = draw.readable(theme.accent, theme.panel, theme.ink) if sunlit else theme.dim
    draw.blit_label(lit, look.SIZE_SMALL, pen, look.W - look.PAD, BAND_TOP + 4, align=2)

    draw.blit_label(_where_text(where), look.SIZE_LABEL, theme.ink, left, BAND_TOP + 3)

    detail = []
    if where.get("speed"):
        detail.append(f"{_grouped(where['speed'])} {unit}/h")
    if aboard:
        detail.append(f"{aboard} aboard")
    if detail:
        draw.blit_label(", ".join(detail), look.SIZE_SMALL, theme.dim, left, BAND_TOP + 19)


def _grouped(value):
    """27600 as "27 600". MicroPython's format has no thousands separator to ask for."""
    digits = f"{value:.0f}"
    out = ""
    while len(digits) > 3:
        out = " " + digits[-3:] + out
        digits = digits[:-3]
    return digits + out


def _where_text(where):
    """"51.6N 30.2E", which is the headline: everything else about the station is constant."""
    lat, lon = where.get("lat"), where.get("lon")
    if lat is None or lon is None:
        return "position unknown"
    return (f"{abs(lat):.1f}{'N' if lat >= 0 else 'S'} "
            f"{abs(lon):.1f}{'E' if lon >= 0 else 'W'}")


def render(page, frame, _history, theme):
    iss = frame.get("iss") or {}
    where = iss.get("where") or {}
    if not worldmap.ready():
        draw.blit_label("loading the map", look.SIZE_VALUE, theme.dim,
                        look.W // 2, MAP_TOP + MAP_H // 2 - 8, align=1)
        _band(theme, None, None, note=None)
        return

    state = _page_state(page)
    view = state["view"]

    if (page or {}).get("follow") == "follow" and where:
        view.jump_to(where["lon"], where["lat"], SCALE_FOLLOW)
    else:
        view.jump_to(0.0, 0.0, SCALE_WORLD)

    view.land(theme)
    if where.get("solar_lon") is not None:
        view.night(theme, where["solar_lon"], where["solar_lat"])
    _track(theme, view, state, iss.get("track") or (), iss.get("flown") or 0)
    if where:
        _marker(theme, view, where)
    _band(theme, where, iss.get("aboard"))


pages.EXTRA["issmap"] = render
# Not animated, unlike the quake map. Nothing here moves between readings: the station covers
# 0.06 pixels a second on a whole-world map, and a frame is 78ms with all 288 polygons in view.
# So it is drawn when a reading lands, and the halo holds still instead of breathing.
