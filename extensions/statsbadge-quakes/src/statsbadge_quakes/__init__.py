"""Recent earthquakes, for the badge to draw on a world map.

`/system/assets/world.geo.json` ships with badgeware, so no geometry travels: the events go
in the frame with their coordinates. Data is the USGS feed, which needs no key and no
account.

The events are a list, which no built-in page kind can draw, so this ships
`badge/quakemap.py`. The scalars beside them suit anything drawing a number.
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

# USGS publishes about once a minute and asks callers to be reasonable.
INTERVAL = 300.0
RETRY_AFTER = 60.0
# How often the fetch thread wakes to check timers.
FETCH_POLL = 1.0
# Store key: a badge switched on before the network is up still has events to draw.
EVENTS = "events"
# USGS place strings run past eighty characters; the band they are drawn in holds about forty.
PLACE_MAX = 48

# Setting name to the feed's `orderby` value.
ORDERS = {"recent": "time", "biggest": "magnitude"}


class Quakes(Source):
    name = "quakes"
    label = "Earthquakes"
    provides = ("quakes",)

    # `events` is left out: it is the list the map draws from, and offering it as a field
    # would put a row of Python in a text page.
    #
    # Declared slow: the feed is polled every five minutes where the badge polls every
    # second. That works because `age_s` below is drawn to the minute.
    groups = {"quakes": {"label": "Earthquakes", "slow": True, "fields": {
        "biggest": {"label": "Largest magnitude", "full_scale": 9.0},
        "latest": {"label": "Latest magnitude", "full_scale": 9.0},
        "count": {"label": "How many"},
    }}}

    badge_module = os.path.join(HERE, "badge", "quakemap.py")

    settings = (
        {"key": "min_mag", "label": "Smallest magnitude", "type": "number",
         "default": 4.0, "min": 0, "max": 10, "step": 0.1,
         "hint": "Below about 4 the feed fills up with events nobody felt: there are "
                 "several thousand a month"},
        {"key": "count", "label": "How many", "type": "number", "default": 10, "min": 1,
         "hint": "How many events the map cycles through, newest first"},
        {"key": "order", "label": "Show the", "type": "choice",
         "options": ["recent", "biggest"], "default": "recent",
         "hint": "Recent is the last few hours, biggest is the largest of the past month"},
    )

    # `quakemap` draws from the `quakes` group and ignores `fields`.
    badge_page = {
        "kind": "quakemap",
        "title": "Quakes",
        "fields": [],
        "slots": {},
    }

    page_settings = (
        {"key": "hold", "label": "Each quake", "type": "number", "default": 6, "min": 1,
         "unit": "seconds",
         "hint": "How long the map stays on one before travelling to the next"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._records = []
        self._lock = threading.Lock()
        self._next = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    def start(self):
        """Restore the stored events, then fetch USGS on a thread."""
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
                # The fetcher must not die: the map would continue drawing the same set.
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
        """Take settings while running. Zeroing the timer refetches, since a magnitude
        changes which events are in the set."""
        super().configure(settings)
        self._read_settings()
        self._next = 0.0
        self._wake.set()

    def sample(self, frame, dt):
        """The events last stored by the fetcher, aged and sorted."""
        with self._lock:
            records = list(self._records)
        # Aged against the minute just gone: rounding each age to its own minute bumps
        # slow_rev ten times a minute, where moving the clock moves all of them together.
        now = int(time.time()) // 60 * 60
        events = []
        for record in records:
            event = dict(record)
            event["age_s"] = max(0, now - event.pop("at"))
            events.append(event)
        frame["quakes"] = {
            "events": events,
            "count": len(events),
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
    """One feature in the shape the badge draws, or None without a magnitude and coordinates.

    The feed returns a null magnitude while an event is still being reviewed.
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
        # The feed reports milliseconds.
        "at": int(properties.get("time", 0)) // 1000,
    }
