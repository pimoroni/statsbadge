"""The badge side of the quakes extension: a world map with the last few on it.

Installed into the app's `ext/` directory by `statsbadge install` and imported by the app,
which is when it registers itself.

The map itself is `worldmap`, which is the app's: the firmware ships the coastlines and two
pages want them, so the shapes and their pens live there and this draws what goes on top.

Why this is code and not a picture over the wire: the map travels. It closes in on the event
it is naming and pulls back out to cross an ocean, at the badge's own frame rate, off a list
that arrives once every five minutes.
"""

import math
import time

import draw
import look
import pages
import worldmap

# The map takes the page's band less a strip at the bottom that names what it is pointing at.
# The header and footer stay where every other page has them.
BAND_H = 34
MAP_TOP = look.BODY_TOP
MAP_H = look.BODY_H - BAND_H
BAND_TOP = MAP_TOP + MAP_H

# Pixels per degree, close in and pulled out. The camera closes in when the next event is
# nearby and pulls out when it is on the other side of the planet, so the travel between two
# says how far apart they are.
SCALE_NEAR = 1.9
SCALE_FAR = 1.05
# Degrees of separation the change is centred on, and how sharply it happens there.
SCALE_KNEE = 10.0
SCALE_RATE = 0.4
# The camera's time constant in ms.
EASE_MS = 420

# The magnitudes the ramp is stretched over. Under 3 is not in the feed at all, and over 7
# there is no ramp left to say it with.
MAG_LOW = 3.0
MAG_HIGH = 7.0

# The reticle: rings a third of a turn apart, each fading as it grows, reaching this many
# degrees so a closed-in map draws a bigger one.
RING_MS = 2000
RINGS = 3
RING_SPAN = 20.0
# Below this a ring is a blob on top of the epicentre rather than a ring around it.
RING_MIN_PX = 4.0

# Where each page is looking and which event it is on, keyed by page id: two map pages hold
# their own places rather than fighting over one.
_state = {}


def _mag_fraction(mag):
    if mag is None:
        return 0.0
    return max(0.0, min(1.0, (float(mag) - MAG_LOW) / (MAG_HIGH - MAG_LOW)))


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


def _page_state(page):
    key = (page or {}).get("id") or "quakes"
    state = _state.get(key)
    if state is None:
        now = time.ticks_ms()
        # Opens on the whole world with nothing selected, which is where it sits until the
        # first list arrives.
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
    """The rest of the set, sized by magnitude: what else has happened, and where."""
    was = screen.clip
    screen.clip = view.box
    screen.pen = theme.dim
    for index, event in enumerate(events):
        if index == active:
            continue
        x, y = view.at(event["lon"], event["lat"])
        if not view.holds(x, y):
            continue
        screen.shape(shape.circle(vec2(x, y), 1.5 + _mag_fraction(event["mag"]) * 2.0))
    screen.clip = was


def _reticle(theme, view, event):
    """Rings leaving the epicentre, in the magnitude's own colour off the ramp."""
    x, y = view.at(event["lon"], event["lat"])
    pen = theme.at(_mag_fraction(event["mag"]))
    now = time.ticks_ms()
    reach = RING_SPAN * view.scale
    width = max(2, int(2.0 * view.scale))
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
    # The epicentre breathes, so it reads as something happening rather than as a printed dot.
    pulse = 2.5 + math.sin(now / 1000.0 * math.pi * 2.0)
    screen.pen = pen
    screen.shape(shape.circle(vec2(x, y), max(3.0, pulse * min(view.scale, 2.0))))
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
    aged = _ago(event.get("age_s"))
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

    # No interaction: the point of the page is that it moves on by itself, and a button on a
    # map wants panning and zooming rather than a step to the next event.
    hold = max(1.0, float((page or {}).get("hold") or 6))
    if events and time.ticks_diff(now, state["held"]) > int(hold * 1000.0):
        state["index"] += 1
        state["held"] = now

    active = None
    if events:
        # Taken modulo the set each frame, so a list that came back shorter cannot leave the
        # page pointing past the end of it.
        state["index"] %= len(events)
        active = events[state["index"]]
        _travel(view, active, elapsed)

    view.land(theme)
    if events:
        _others(theme, view, events, state["index"])
        _reticle(theme, view, active)
    _band(theme, active, state["index"], len(events))


pages.EXTRA["quakemap"] = render
# The rings grow and the camera travels between readings, so this page wants every frame it
# can have rather than one a second.
pages.ANIMATED.add("quakemap")
