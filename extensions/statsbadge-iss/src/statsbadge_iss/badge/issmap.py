"""The badge side of the ISS extension: the station, its track, and the terminator.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
at which point it registers itself.

The map and the day and night wash are the app's `worldmap`. This draws the station: an
orbit of ground track with the flown half behind it, a marker saying whether it is in
sunlight, and the readouts under the map.
"""

from array import array

import draw
import look
import pages
import worldmap

BAND_H = 34
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
BAND_TOP = MAP_TOP + MAP_H

# 360 degrees across 320 pixels. The poles fall outside the band, cropping off the polar
# ice caps; the station never leaves 51.6 degrees.
SCALE_WORLD = look.W / 360.0
SCALE_FOLLOW = 1.6

TRACK_W = 1.6
# Points drawn between each pair the host sent. Five minutes of orbit is twenty degrees of
# longitude, so four is a chord of five.
TRACK_STEPS = 4
# Alpha for the part already flown.
FLOWN_ALPHA = 96
# Alpha for the part in shadow.
DARK_ALPHA = 110

# The marker, in degrees of arc so it holds its size on screen whatever the zoom.
BODY = 3.0
PANEL_LONG = 7.0
PANEL_SHORT = 1.6
HALO = 6.0

# Keyed by page id.
_state = {}
# The track projected to the screen, x and y a point, grown once and reused every frame.
_path = array("f", b"")
_parts = None


def _page_state(page):
    key = (page or {}).get("id") or "iss"
    state = _state.get(key)
    if state is None:
        state = _state[key] = {
            "view": worldmap.View(MAP_TOP, MAP_H, lon=0.0, lat=0.0, scale=SCALE_WORLD),
        }
    return state


# How much of the gap to the track's position is closed each draw. The lag left is a quarter
# of a pixel.
CATCH_UP = 0.25
# Past this the gap is a jump: a first draw, or a page returned to after an orbit.
CATCH_UP_MAX = 5.0


def eased(held, target):
    """`held` moved a share of the way to `target`, the short way round the date line."""
    if held is None:
        return target
    lon = worldmap.shortest(target[0] - held[0])
    if abs(lon) > CATCH_UP_MAX or abs(target[1] - held[1]) > CATCH_UP_MAX:
        return target
    return ((held[0] + lon * CATCH_UP + 180.0) % 360.0 - 180.0,
            held[1] + (target[1] - held[1]) * CATCH_UP,
            target[2])


def flown_at(dense, flown):
    """Where along the track `flown` falls, as (lon, lat, sunlit).

    Interpolated between two dense points, which are a quarter of a five minute step apart.
    Longitude comes back unwrapped, as the spline needed it, and is returned to range.
    """
    if not dense:
        return None
    at = flown * TRACK_STEPS
    first = max(0, min(len(dense) - 1, int(at)))
    second = min(len(dense) - 1, first + 1)
    part = at - int(at)
    lon = dense[first][0] + (dense[second][0] - dense[first][0]) * part
    lat = dense[first][1] + (dense[second][1] - dense[first][1]) * part
    return ((lon + 180.0) % 360.0 - 180.0, lat, dense[first][2])


def _marker(theme, view, lon, lat, sunlit):
    """The station: a lit marker in sunlight, a quiet one in shadow, over a soft halo."""
    x, y = view.at(lon, lat)
    pen = theme.accent if sunlit else theme.dim
    was = screen.clip
    screen.clip = view.box

    # Built once and re-aimed: a circle is a path of thirty-odd points to allocate for the
    # sake of moving it a pixel.
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
    """The ISS ground track, resampled to a curve through every point the host sent.

    Five minutes of orbit is twenty-odd degrees of longitude, and the samples drawn as chords
    show as a fan of straight lines. Catmull-Rom, as the graph pages use.

    Longitude is unwrapped first: the run crosses the date line, and a spline through 179
    then -179 would swing back round the whole world to get there.
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
    # A point takes the light of the sample it came from, so a crossing lands on a sample.
    return [(dense_lon[index], dense_lat[index],
             lit[min(len(lit) - 1, index // TRACK_STEPS)])
            for index in range(len(dense_lon))]


def _curve(state, points):
    """The smoothed track, rebuilt only when the host sends a new run."""
    key = (len(points), points[0], points[-1])
    if state.get("curve_for") != key:
        state["curve_for"] = key
        state["curve"] = _smoothed(points)


def _track(theme, view, state, points, flown):
    """The ground track, split at `flown`, the index the host currently reports.

    Drawn as a stroked path per stretch: one open contour is 0.08ms plus its edges, where
    seventy-six lines are 0.08ms each.
    """
    if len(points) < 2:
        return
    _curve(state, points)
    dense = state["curve"]
    cut = max(0, min(len(dense) - 1, int(flown * TRACK_STEPS)))

    # The stroked shapes are kept until the run, the split or the projection moves - the
    # camera to the nearest pixel, since below that it is the same picture. Stroking is the
    # dear half: an open contour of 77 points becomes an outline of four times that, six
    # times a frame.
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
    """The ground track as stroked shapes, one per run drawn the same way.

    Projected into one buffer that outlives the frame and stroked out of slices of it. A vec2
    a point and a list a run would be 77 objects. Same idiom as draw.line.
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
        # A comparable tuple, since two pens cannot be compared.
        want = (index >= cut, bool(sunlit))
        x, y = view.at(lon, lat)
        # A jump wider than half the screen is the date-line seam.
        seam = previous_x is not None and abs(x - previous_x) > look.W * 0.5
        if at and (seam or want != stretch):
            bounds.append((start, at, stretch))
            # A change of colour repeats the last point so the join is drawn. A seam
            # starts clean, the two ends not being next to each other.
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


def _band(theme, where, aboard, at=None, note="waiting for the feed"):
    """The strip under the map: how high, how fast, in sun or shadow, and who is aboard."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, BAND_TOP, look.W, BAND_H))
    # The header's underline accent, so the track band reads as part of the furniture.
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

    # Sunlight is the one state among the numbers, so it is drawn in the theme's accent
    # colour.
    sunlit = at[2] if at else where.get("sunlit", True)
    lit = "in sunlight" if sunlit else "in shadow"
    pen = draw.readable(theme.accent, theme.panel, theme.ink) if sunlit else theme.dim
    draw.blit_label(lit, look.SIZE_SMALL, pen, look.W - look.PAD, BAND_TOP + 4, align=2)

    draw.blit_label(_where_text(at[1], at[0]) if at else _where_text(None, None),
                    look.SIZE_LABEL, theme.ink, left, BAND_TOP + 3)

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


def _where_text(lat, lon):
    """"51.6N 30.2E", or "position unknown"."""
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
    track = iss.get("track") or ()
    flown = iss.get("flown") or 0

    # The position off the track the host already sent, moved every draw so the marker walks.
    if len(track) >= 2:
        _curve(state, track)
        state["at"] = eased(state.get("at"), flown_at(state["curve"], flown))
    elif where.get("lon") is not None:
        state["at"] = (where["lon"], where["lat"], where.get("sunlit", True))
    at = state.get("at")

    if (page or {}).get("follow") == "follow" and at:
        view.jump_to(at[0], at[1], SCALE_FOLLOW)
    else:
        view.jump_to(0.0, 0.0, SCALE_WORLD)

    view.land(theme)
    if where.get("solar_lon") is not None:
        view.night(theme, where["solar_lon"], where["solar_lat"])
    _track(theme, view, state, track, flown)
    if at:
        _marker(theme, view, at[0], at[1], at[2])
    _band(theme, where, iss.get("aboard"), at)


pages.EXTRA["issmap"] = render
# Left out of pages.ANIMATED: the station covers 0.06 pixels a second on a
# whole-world map, and a frame is 78ms with all 288 polygons in view.
