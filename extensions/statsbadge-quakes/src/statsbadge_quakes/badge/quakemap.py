"""The badge side of the quakes extension: a world map with the last few on it.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
which is when it registers itself.

The map is the firmware's own `/system/assets/world.geo.json`, parsed once into a shape per
polygon and then only re-aimed: a `mat3` moves 177 countries a frame for the price of the
transform, where rebuilding the paths would cost the parse again every time the camera moved.

Why this is code and not a picture over the wire: the map travels. It closes in on the event
it is naming and pulls back out to cross an ocean, at the badge's own frame rate, off a list
that arrives once every five minutes.
"""

import gc
import json
import math
import time

import draw
import look
import pages

MAP_FILE = "/system/assets/world.geo.json"

# The map takes the page's band less a strip at the bottom that names what it is pointing at.
# The header and footer stay where every other page has them.
BAND_H = 34
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
MAP_MID = (look.W // 2, MAP_TOP + MAP_H // 2)
BAND_TOP = MAP_TOP + MAP_H

# A degree of latitude is drawn taller than a degree of longitude, which is most of what makes
# an equirectangular map read as the world rather than as a squashed one.
ASPECT = 1.3

# Pixels per degree, close in and pulled out. The camera closes in when the next event is
# nearby and pulls out when it is on the other side of the planet, so the travel between two
# says how far apart they are.
SCALE_NEAR = 1.9
SCALE_FAR = 1.05
# Degrees of separation the change is centred on, and how sharply it happens there.
SCALE_KNEE = 10.0
SCALE_RATE = 0.4
# The camera's time constant in ms, so a travel takes as long whatever the page is costing to
# draw: this one is heavier than a gauge and would ease visibly slower for a per-frame share.
EASE_MS = 420

# Land is the ramp colour for its latitude, thinned to this much of it over the page: enough
# to read as land, not enough to compete with what is drawn on top.
LAND_ALPHA = 104
# Where the ramp is anchored, in degrees from the equator. The tropics take the hot end and
# the ice caps the cold one.
LAND_SPAN = 90.0

# The magnitudes the ramp is stretched over. Under 3 is not in the feed at all, and over 7
# there is no ramp left to say it with.
MAG_LOW = 3.0
MAG_HIGH = 7.0
# How far the magnitude has to sit from the band it is written on, in counts of the firmware's
# own difference, where black to white is 100. The same floor draw.py holds a graph series to.
# Measured across the themes: the cold end of a pale palette's ramp comes within 5 of its own
# panel, so a small quake would be written in a colour that is not there.
READABLE = 20
# Steps toward the ink to try before giving up on the hue and taking the ink itself.
TOWARD_INK = (255, 128)

# The reticle: rings a third of a turn apart, each fading as it grows, reaching this many
# degrees so a closed-in map draws a bigger one.
RING_MS = 2000
RINGS = 3
RING_SPAN = 20.0
# Below this a ring is a blob on top of the epicentre rather than a ring around it.
RING_MIN_PX = 4.0

# The parsed map: one entry per polygon, holding the shape and the box it covers in degrees.
# Built on the first frame that needs it, so a badge with no map page never pays for it.
_shapes = None
# Whether a frame has already said the map is coming, so the parse happens in the frame after
# the notice rather than in place of it.
_asked = False
# A pen per polygon, and the theme they were worked out for.
_pens = None
_pens_for = None
# Where each page's camera is and which event it is on, keyed by page id: two map pages hold
# their own places rather than fighting over one.
_cameras = {}


def _map():
    """Every polygon as a shape in degrees, with its bounds, parsed once.

    The shape holds (lon, -lat) so a single `mat3` per frame both scales the map and places
    it. The bounds are kept because most of the world is off screen at any zoom, and skipping
    a polygon costs six comparisons where drawing one costs its edges.
    """
    global _shapes
    if _shapes is not None:
        return _shapes
    built = []
    try:
        with open(MAP_FILE) as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        print(f"quakemap: no map in {MAP_FILE}: {exc}")
        _shapes = ()
        return _shapes
    for country in data:
        for polygon in country.get("polygons") or ():
            if len(polygon) < 3:
                continue
            path = []
            lon_min = lat_min = 1000.0
            lon_max = lat_max = -1000.0
            for lon, lat in polygon:
                path.append(vec2(lon, -lat))
                lon_min = min(lon_min, lon)
                lon_max = max(lon_max, lon)
                lat_min = min(lat_min, lat)
                lat_max = max(lat_max, lat)
            built.append((shape.custom(path), (lon_min + lon_max) * 0.5,
                          (lat_min + lat_max) * 0.5, lon_min, lon_max, lat_min, lat_max))
    _shapes = tuple(built)
    # The parsed file is a list per point and outlives its usefulness the moment the shapes
    # are built, so it goes before the first frame is drawn rather than at the next collect.
    del data, built
    gc.collect()
    return _shapes


def _land(theme):
    """One pen per polygon: the ramp colour for its latitude, over the page.

    Worked out per theme rather than per frame - a pen apiece is 288 ramp lookups and 288
    composites - and dropped when the theme changes.
    """
    global _pens, _pens_for
    if _pens is not None and _pens_for == theme.name:
        return _pens
    _pens = tuple(theme.at(1.0 - min(1.0, abs(entry[2]) / LAND_SPAN))
                  .with_alpha(LAND_ALPHA).over(theme.bg)
                  for entry in _map())
    _pens_for = theme.name
    return _pens


def _mag_fraction(mag):
    if mag is None:
        return 0.0
    return max(0.0, min(1.0, (float(mag) - MAG_LOW) / (MAG_HIGH - MAG_LOW)))


def _readable(pen, theme):
    """The ramp colour if it can be read on the band, otherwise the same hue nearer the ink.

    The magnitude is the number on the page and it is written in the colour of the reading,
    which on a pale theme's cold end is the panel again. Hue first, legibility over it.
    """
    for alpha in TOWARD_INK:
        candidate = pen if alpha == 255 else pen.with_alpha(alpha).over(theme.ink)
        if theme.panel.difference(candidate) >= READABLE:
            return candidate
    return theme.ink


def _shortest(degrees):
    """A difference in longitude taken the short way round, so nothing crosses the date line."""
    return degrees - 360.0 * math.floor(degrees / 360.0 + 0.5)


def _at(lon, lat, camera):
    """Where a point in degrees lands on the screen."""
    scale = camera["scale"]
    return (MAP_MID[0] + _shortest(lon - camera["lon"]) * scale,
            MAP_MID[1] - (lat - camera["lat"]) * scale * ASPECT)


def _ago(seconds):
    if seconds is None:
        return None
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _camera(page):
    key = (page or {}).get("id") or "quakes"
    camera = _cameras.get(key)
    if camera is None:
        now = time.ticks_ms()
        # Opens on the whole world with nothing selected, which is where it sits until the
        # first list arrives.
        camera = _cameras[key] = {"index": 0, "held": now, "drawn": now,
                                  "lon": 0.0, "lat": 15.0, "scale": SCALE_FAR}
    return camera


def _travel(camera, active, elapsed):
    """Move the camera toward the active event, and set the zoom from how far that is.

    Exponential approach on a time constant, so the ease is the same at 20fps and at 45.
    """
    dlon = _shortest(active["lon"] - camera["lon"])
    dlat = active["lat"] - camera["lat"]
    away = math.sqrt(dlon * dlon + dlat * dlat)
    target = SCALE_FAR + (SCALE_NEAR - SCALE_FAR) / (
        1.0 + math.exp((away - SCALE_KNEE) * SCALE_RATE))
    step = max(0.0, min(1.0, elapsed / EASE_MS))
    camera["lon"] = _shortest(camera["lon"] + dlon * step)
    camera["lat"] += dlat * step
    camera["scale"] += (target - camera["scale"]) * step


def _coastlines(theme, camera):
    """Every polygon in view, drawn where the camera is looking.

    Clipped to the map band, so nothing reaches the header, the footer or the reading band.
    """
    shapes = _map()
    if not shapes:
        draw.blit_label("no map data", look.SIZE_VALUE, theme.dim,
                        MAP_MID[0], MAP_MID[1] - 8, align=1)
        return
    pens = _land(theme)
    scale = camera["scale"]
    cam_lon, cam_lat = camera["lon"], camera["lat"]
    half_lon = (look.W * 0.5) / scale
    half_lat = (MAP_H * 0.5) / (scale * ASPECT)
    base_y = cam_lat * scale * ASPECT + MAP_MID[1]
    was = screen.clip
    screen.clip = rect(0, MAP_TOP, look.W, MAP_H)
    # Looked up once: this runs 288 times a frame, and the attribute lookup is not free.
    local_floor = math.floor
    for index, entry in enumerate(shapes):
        outline, lon_mid, _lat_mid, lon_min, lon_max, lat_min, lat_max = entry
        # Each polygon is drawn at whichever whole turn of longitude puts it nearest the
        # camera, which is what makes the map wrap rather than end at the date line.
        turn = 360.0 * local_floor((cam_lon - lon_mid) / 360.0 + 0.5)
        if lon_min + turn - cam_lon > half_lon or cam_lon - lon_max - turn > half_lon:
            continue
        if lat_min - cam_lat > half_lat or cam_lat - lat_max > half_lat:
            continue
        screen.pen = pens[index]
        outline.transform = mat3().translate(
            (turn - cam_lon) * scale + MAP_MID[0], base_y).scale(scale, scale * ASPECT)
        screen.shape(outline)
    screen.clip = was


def _others(theme, events, active, camera):
    """The rest of the set, sized by magnitude: what else has happened, and where."""
    screen.pen = theme.dim
    for index, event in enumerate(events):
        if index == active:
            continue
        x, y = _at(event["lon"], event["lat"], camera)
        if not (0 <= x < look.W and MAP_TOP <= y < BAND_TOP):
            continue
        screen.shape(shape.circle(vec2(x, y), 1.5 + _mag_fraction(event["mag"]) * 2.0))


def _reticle(theme, event, camera):
    """Rings leaving the epicentre, in the magnitude's own colour off the ramp."""
    x, y = _at(event["lon"], event["lat"], camera)
    pen = theme.at(_mag_fraction(event["mag"]))
    now = time.ticks_ms()
    reach = RING_SPAN * camera["scale"]
    width = max(2, int(2.0 * camera["scale"]))
    was = screen.clip
    screen.clip = rect(0, MAP_TOP, look.W, MAP_H)
    for ring in range(RINGS):
        progress = ((now + ring * (RING_MS // RINGS)) % RING_MS) / RING_MS
        radius = progress * reach
        if radius < RING_MIN_PX:
            continue
        # Squared, so a ring is bright where it leaves and gone well before the edge.
        screen.pen = pen.with_alpha(int((1.0 - progress) ** 2 * 255))
        screen.shape(shape.circle(vec2(x, y), radius).stroke(width))
    # The epicentre breathes, so it reads as something happening rather than as a printed dot.
    pulse = 2.5 + math.sin(now / 1000.0 * math.pi * 2.0)
    screen.pen = pen
    screen.shape(shape.circle(vec2(x, y), max(3.0, pulse * min(camera["scale"], 2.0))))
    screen.pen = theme.ink
    screen.shape(shape.circle(vec2(x, y), 1.5))
    screen.clip = was


def _band(theme, event, index, total, note="waiting for the feed"):
    """The strip under the map: how big, where, how deep and how long ago."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, BAND_TOP, look.W, BAND_H))
    # The same rule the header draws, in the same colour, so the band reads as furniture.
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, BAND_TOP, look.W, 1))
    if event is None:
        # Nothing to say where the map itself is carrying the message.
        if note:
            draw.blit_label(note, look.SIZE_VALUE, theme.dim, look.PAD, BAND_TOP + 9)
        return

    magnitude = f"M {event['mag']:.1f}"
    draw.blit_label(magnitude, look.SIZE_BIG,
                    _readable(theme.at(_mag_fraction(event["mag"])), theme),
                    look.PAD, BAND_TOP + 3)
    left = look.PAD + draw.text_width(magnitude, look.SIZE_BIG) + 10

    where = f"{index + 1}/{total}"
    draw.blit_label(where, look.SIZE_SMALL, theme.dim, look.W - look.PAD, BAND_TOP + 4,
                    align=2)

    room = look.W - left - look.PAD * 2 - draw.text_width(where, look.SIZE_SMALL)
    place = event.get("place") or "somewhere unnamed"
    draw.blit_label(draw.fit(place, look.SIZE_LABEL, room), look.SIZE_LABEL, theme.ink,
                    left, BAND_TOP + 3)

    detail = []
    if event.get("depth") is not None:
        detail.append(f"{event['depth']:.0f} km deep")
    aged = _ago(event.get("age_s"))
    if aged:
        detail.append(aged)
    if detail:
        draw.blit_label(", ".join(detail), look.SIZE_SMALL, theme.dim, left, BAND_TOP + 19)


def render(page, frame, _history, theme):
    global _asked
    events = (frame.get("quakes") or {}).get("events") or []
    if _shapes is None and not _asked:
        # Reading and parsing 215KB of coastline and building 288 shapes out of it is over a
        # second, and the frame it happens in is a frame that does not arrive. This one says
        # what it is waiting for; the next does the work, the page being animated.
        _asked = True
        draw.blit_label("loading the map", look.SIZE_VALUE, theme.dim,
                        MAP_MID[0], MAP_MID[1] - 8, align=1)
        _band(theme, None, 0, 0, note=None)
        return

    camera = _camera(page)
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, camera["drawn"])
    camera["drawn"] = now

    # No interaction: the point of the page is that it moves on by itself, and a button on a
    # map wants panning and zooming rather than a step to the next event.
    hold = max(1.0, float((page or {}).get("hold") or 6))
    if events and time.ticks_diff(now, camera["held"]) > int(hold * 1000.0):
        camera["index"] += 1
        camera["held"] = now

    active = None
    if events:
        # Taken modulo the set each frame, so a list that came back shorter cannot leave the
        # page pointing past the end of it.
        camera["index"] %= len(events)
        active = events[camera["index"]]
        _travel(camera, active, elapsed)

    _coastlines(theme, camera)
    if events:
        _others(theme, events, camera["index"], camera)
        _reticle(theme, active, camera)
    _band(theme, active, camera["index"], len(events))


pages.EXTRA["quakemap"] = render
# The rings grow and the camera travels between readings, so this page wants every frame it
# can have rather than one a second.
pages.ANIMATED.add("quakemap")
