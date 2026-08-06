"""Recent earthquakes, for the badge to draw on a world map.

The map is the firmware's own - `/system/assets/world.geo.json` ships with badgeware - so
nothing here sends geometry: the events go in the frame and the badge knows where they are.
Data is the USGS feed, which needs no key and no account.

The events are a list, which no built-in page kind can draw, so this ships a page of its own
in `badge/quakemap.py`. The scalars beside them are for anything else that wants a number:
`quakes.biggest` in a text page reads the largest of the set.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from statsbadge.sources.base import Source

HERE = os.path.dirname(os.path.abspath(__file__))

FEED = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# How often the feed is asked. USGS publishes about once a minute and asks callers to be
# reasonable; the map holds on each event for seconds, so nothing here is waiting on it.
INTERVAL = 300.0
# A failure waits this long rather than the whole interval, a connection dropping on a
# laptop being over in seconds, and rather than never.
RETRY_AFTER = 60.0
# How often the fetcher looks for something due.
FETCH_POLL = 1.0
# The last good set, kept so a badge switched on before the network is up draws the quakes
# it knew about instead of an empty world.
EVENTS = "events"
# What the badge is given to draw. USGS place strings run past eighty characters and the band
# they are drawn in holds about forty, so the rest is neither useful nor worth sending every
# second.
PLACE_MAX = 48

# What the setting is called against what the feed calls it.
ORDERS = {"recent": "time", "biggest": "magnitude"}


class Quakes(Source):
    name = "quakes"
    label = "Earthquakes"
    provides = ("quakes",)

    # The scalars, for a page that wants a number rather than a map. `events` is not in
    # here on purpose: it is the list the map draws from, and nothing else can draw it, so
    # offering it as a field would put a row of Python in a text page.
    #
    # Slow, because it is: the feed is asked every five minutes and the badge polls every
    # second. Which only works because `age_s` below is drawn to the minute.
    groups = {"quakes": {"label": "Earthquakes", "slow": True, "fields": {
        "biggest": {"label": "Largest magnitude", "full_scale": 9.0},
        "latest": {"label": "Latest magnitude", "full_scale": 9.0},
        "count": {"label": "How many"},
    }}}

    badge_module = os.path.join(HERE, "badge", "quakemap.py")

    settings = (
        {"key": "min_mag", "label": "Smallest magnitude", "type": "number",
         "default": 4.0,
         "hint": "Below about 4 the feed fills up with events nobody felt: there are "
                 "several thousand a month"},
        {"key": "count", "label": "How many", "type": "number", "default": 10,
         "hint": "How many events the map cycles through, newest first"},
        {"key": "order", "label": "Show the", "type": "choice",
         "options": ["recent", "biggest"], "default": "recent",
         "hint": "recent is the last few hours, biggest is the largest of the past month"},
    )

    # No field slots: the renderer draws from its own group and never reads `fields`.
    badge_page = {
        "kind": "quakemap",
        "title": "Quakes",
        "fields": [],
        "slots": {},
    }

    page_settings = (
        {"key": "hold", "label": "Seconds each", "type": "number", "default": 6,
         "hint": "How long the map stays on one quake before travelling to the next"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        # What the fetcher last brought back, read while sampling and replaced on the
        # fetcher's thread, so both go through the lock.
        self._records = []
        self._lock = threading.Lock()
        self._next = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    def start(self):
        """Take up where the last run left off, then fetch on a thread of its own.

        Nothing in `sample` may wait on a network: every source shares the collector's
        thread and the first sample is taken while the server is still starting up.
        """
        self._records = self.store.get(EVENTS) or []
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-quakes")
            self._fetcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._fetcher is not None:
            self._fetcher.join(timeout=2.0)
            self._fetcher = None

    def _fetch_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as exc:
                # The fetcher must not die, or the map would go on drawing the same set
                # with nothing ever replacing it.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _read_settings(self):
        try:
            self.min_mag = float(self.config.get("min_mag") or 4.0)
        except (TypeError, ValueError):
            self.min_mag = 4.0
        try:
            self.count = int(self.config.get("count") or 10)
        except (TypeError, ValueError):
            self.count = 10
        self.count = max(1, min(20, self.count))
        self.order = self.config.get("order") or "recent"
        if self.order not in ORDERS:
            self.order = "recent"

    def configure(self, settings):
        """Take settings while running, and fetch again rather than waiting out the interval.

        A magnitude typed in the browser changes which events are in the set, so it should
        show up on the badge and not in five minutes.
        """
        super().configure(settings)
        self._read_settings()
        self._next = 0.0
        self._wake.set()

    def sample(self, frame, dt):
        """Whatever the fetcher has already brought back. Nothing here touches the network."""
        with self._lock:
            records = list(self._records)
        # Aged against the minute just gone rather than against this instant. The badge
        # never draws an age finer than a minute, and this group is declared slow, so what
        # matters is how often the set *changes*: rounding each age to its own minute is
        # ten events crossing ten boundaries at ten unrelated moments, which moved the
        # revision about ten times a minute. Moving the clock instead moves all of them
        # together, once, and the whole feed goes out once a minute instead of sixty times.
        now = int(time.time()) // 60 * 60
        events = []
        for record in records:
            # The age is what the badge draws and the timestamp is only what it is worked
            # out from, so the timestamp stays here.
            event = dict(record)
            event["age_s"] = max(0, now - event.pop("at"))
            events.append(event)
        frame["quakes"] = {
            "events": events,
            "count": len(events),
            # For anything that wants one number out of the set: a dial or a text page can
            # read these without knowing what an event looks like.
            "biggest": max((event["mag"] for event in events), default=None),
            "latest": events[0]["mag"] if events else None,
        }

    def _refresh(self):
        if time.monotonic() < self._next:
            return
        try:
            records = self._fetch()
        except Exception as exc:
            self._next = time.monotonic() + RETRY_AFTER
            self.note_fault(exc)
            return
        with self._lock:
            self._records = records
        # Kept for the next launch, not as a cache for this one: the badge should have
        # something to draw before the first fetch lands.
        self.store.set(EVENTS, records)
        self._next = time.monotonic() + INTERVAL
        self.note_ok()

    def _fetch(self):
        query = urllib.parse.urlencode({
            "format": "geojson",
            "limit": self.count,
            "orderby": ORDERS[self.order],
            "minmagnitude": self.min_mag,
        })
        with urllib.request.urlopen(f"{FEED}?{query}", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        records = []
        for feature in payload.get("features") or ():
            record = _event(feature)
            if record is not None:
                records.append(record)
        return records


def _event(feature):
    """One feature as the badge wants it, or None if it is not somewhere with a size.

    An event with no magnitude or no coordinates cannot be drawn on a map or measured, and
    the feed does return both: a magnitude is null while it is still being reviewed.
    """
    properties = feature.get("properties") or {}
    coordinates = (feature.get("geometry") or {}).get("coordinates") or ()
    if properties.get("mag") is None or len(coordinates) < 2:
        return None
    place = (properties.get("place") or properties.get("title") or "").strip()
    return {
        "mag": round(float(properties["mag"]), 1),
        "place": place[:PLACE_MAX],
        "lon": round(float(coordinates[0]), 3),
        "lat": round(float(coordinates[1]), 3),
        # Kilometres, and negative for the handful of events placed above sea level.
        "depth": round(float(coordinates[2]), 1) if len(coordinates) > 2 else None,
        # Seconds, since the feed reports milliseconds and the age is worked out per sample.
        "at": int(properties.get("time", 0)) // 1000,
    }
