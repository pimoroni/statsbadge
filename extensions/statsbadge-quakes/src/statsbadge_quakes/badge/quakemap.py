"""The badge side of the quakes extension: a world map showing recent notable quakes.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
at which point it registers itself.

The coastlines and their pens are the app's `worldmap`. This draws an epicentre per event,
reticle rings over the one the camera is on, and the band naming it.
"""

import math
import time

import draw
import look
import pages
import worldmap

BAND_H = 34
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
BAND_TOP = MAP_TOP + MAP_H

# Pixels per degree at the closest and furthest zoom.
SCALE_NEAR = 1.9
SCALE_FAR = 1.05
# _travel sets the zoom on a logistic curve against the degrees to the next event: KNEE is
# the distance where the zoom lands midway between the two scales, RATE how steep it is.
SCALE_KNEE = 10.0
SCALE_RATE = 0.4
# The camera's time constant in ms.
EASE_MS = 420

# The magnitude range _mag_fraction normalises over, for the colour ramp and the dot and
# ring sizes. Clamped, so anything outside lands on an end.
MAG_LOW = 3.0
MAG_HIGH = 7.0

# Rings are offset by RING_MS / RINGS, so they leave one after another.
RING_MS = 2000
RINGS = 3
# How far one reaches, from the low magnitude to the high, in pixels. Sized as a marker: a
# magnitude 5 is strongly felt for some 60km, half a pixel here.
RING_PX_LOW = 3.0
RING_PX_HIGH = 8.0
# Below this a ring is a blob on top of the epicentre.
RING_MIN_PX = 1.0

# The epicentre, from the low magnitude to the high.
DOT_PX_LOW = 1.0
DOT_PX_HIGH = 2.5
DOT_PULSE_PX = 0.5

# Keyed by page id, so two map pages hold separate places.
_state = {}


def _dot_px(mag):
    """How wide an epicentre is drawn, by magnitude."""
    return DOT_PX_LOW + (DOT_PX_HIGH - DOT_PX_LOW) * _mag_fraction(mag)


def _mag_fraction(mag):
    if mag is None:
        return 0.0
    return max(0.0, min(1.0, (float(mag) - MAG_LOW) / (MAG_HIGH - MAG_LOW)))


def _page_state(page):
    key = (page or {}).get("id") or "quakes"
    state = _state.get(key)
    if state is None:
        now = time.ticks_ms()
        state = _state[key] = {
            "index": 0, "held": now, "drawn": now,
            "view": worldmap.View(MAP_TOP, MAP_H, lon=0.0, lat=15.0, scale=SCALE_FAR),
        }
    return state


def _travel(view, active, elapsed):
    """Move the camera toward the active event, and set the zoom from how far that is."""
    dlon = worldmap.shortest(active["lon"] - view.lon)
    dlat = active["lat"] - view.lat
    away = math.sqrt(dlon * dlon + dlat * dlat)
    scale = SCALE_FAR + (SCALE_NEAR - SCALE_FAR) / (
        1.0 + math.exp((away - SCALE_KNEE) * SCALE_RATE))
    view.look_at(active["lon"], active["lat"], scale, elapsed, EASE_MS)


def _others(theme, view, events, active):
    """The rest of the set, sized by magnitude."""
    was = screen.clip
    screen.clip = view.box
    screen.pen = theme.dim
    for index, event in enumerate(events):
        if index == active:
            continue
        x, y = view.at(event["lon"], event["lat"])
        if not view.holds(x, y):
            continue
        screen.shape(shape.circle(vec2(x, y), _dot_px(event["mag"])))
    screen.clip = was


def _reticle(theme, view, event):
    """Rings leaving the epicentre, coloured by magnitude off the theme's colour ramp."""
    x, y = view.at(event["lon"], event["lat"])
    pen = theme.at(_mag_fraction(event["mag"]))
    now = time.ticks_ms()
    reach = RING_PX_LOW + (RING_PX_HIGH - RING_PX_LOW) * _mag_fraction(event["mag"])
    # A ring eight across takes a hairline, where three would be a disc.
    width = 1
    was = screen.clip
    screen.clip = view.box
    for ring in range(RINGS):
        progress = ((now + ring * (RING_MS // RINGS)) % RING_MS) / RING_MS
        radius = progress * reach
        if radius < RING_MIN_PX:
            continue
        # Squared, so a ring is bright where it leaves and gone well before the edge.
        screen.pen = pen.with_alpha(int((1.0 - progress) ** 2 * 255))
        screen.shape(shape.circle(vec2(x, y), radius).stroke(width))
    dot = _dot_px(event["mag"]) + DOT_PULSE_PX * math.sin(now / 1000.0 * math.pi * 2.0)
    screen.pen = pen
    screen.shape(shape.circle(vec2(x, y), dot + 1.0))
    screen.pen = theme.ink
    screen.shape(shape.circle(vec2(x, y), dot * 0.5))
    screen.clip = was


def _band(theme, event, index, total, note="waiting for the feed"):
    """The strip under the map: how big, where, how deep and how long ago."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, BAND_TOP, look.W, BAND_H))
    # Use the header's underline accent colour to make the band look like UI.
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, BAND_TOP, look.W, 1))
    if event is None:
        if note:
            draw.blit_label(note, look.SIZE_VALUE, theme.dim, look.PAD, BAND_TOP + 9)
        return

    magnitude = f"M {event['mag']:.1f}"
    draw.blit_label(magnitude, look.SIZE_BIG,
                    draw.readable(theme.at(_mag_fraction(event["mag"])), theme.panel,
                                  theme.ink),
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
    aged = draw.ago(event.get("age_s"))
    if aged:
        detail.append(aged)
    if detail:
        draw.blit_label(", ".join(detail), look.SIZE_SMALL, theme.dim, left, BAND_TOP + 19)


def render(page, frame, _history, theme):
    events = (frame.get("quakes") or {}).get("events") or []
    if not worldmap.ready():
        draw.blit_label("loading the map", look.SIZE_VALUE, theme.dim,
                        look.W // 2, MAP_TOP + MAP_H // 2 - 8, align=1)
        _band(theme, None, 0, 0, note=None)
        return

    state = _page_state(page)
    view = state["view"]
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, state["drawn"])
    state["drawn"] = now

    hold = max(1.0, float((page or {}).get("hold") or 6))
    if events and time.ticks_diff(now, state["held"]) > int(hold * 1000.0):
        state["index"] += 1
        state["held"] = now

    active = None
    if events:
        # Modulo the set each frame, so a list that came back shorter cannot leave the page
        # pointing past the end of it.
        state["index"] %= len(events)
        active = events[state["index"]]
        _travel(view, active, elapsed)

    view.land(theme)
    if events:
        _others(theme, view, events, state["index"])
        _reticle(theme, view, active)
    _band(theme, active, state["index"], len(events))


pages.EXTRA["quakemap"] = render
# Register as an animated page: the rings grow and the camera travels between readings.
pages.ANIMATED.add("quakemap")
