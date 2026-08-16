"""Where the space station is, for the badge to draw on a world map.

Two feeds, neither needing a key or an account:

    wheretheiss.at   where it is now, where it will be, and the sub-solar point.
    open-notify.org  who is aboard.

`/positions` returns a position per timestamp, so the ground track is fetched as twenty
positions either side of now. `flown` is where now falls in that run.
"""

import json
import os
import threading
import time
import urllib.request

from statsbadge.sources.base import Source

HERE = os.path.dirname(os.path.abspath(__file__))

WHERE = "https://api.wheretheiss.at/v1/satellites/25544"
CREW = "http://api.open-notify.org/astros.json"

# The station covers 0.065 degrees a second: a pixel of a whole-world map every seventeen
# seconds, and one of the followed camera every ten.
POSITION_EVERY = 300.0
TRACK_EVERY = 600.0
CREW_EVERY = 3600.0
RETRY_AFTER = 30.0
# How often the fetch thread wakes to check timers.
FETCH_POLL = 1.0

# The endpoint takes ten timestamps a request, so twenty points is two requests.
# Ninety-five minutes is about one orbit.
TRACK_STEP_S = 300
TRACK_BACK = 9
TRACK_AHEAD = 10
# Store key: a badge switched on before the network is up still has a position to draw.
LAST = "last"


class ISS(Source):
    name = "iss"
    provides = ("iss",)

    badge_module = os.path.join(HERE, "badge", "issmap.py")

    settings = (
        {"key": "units", "label": "Distances in", "type": "choice",
         "options": ["kilometres", "miles"], "default": "kilometres",
         "hint": "Altitude and speed. The map is in degrees either way"},
        {"key": "crew", "label": "Show the crew", "type": "bool", "default": True,
         "hint": "How many people are aboard, from open-notify.org"},
    )

    # `issmap` draws from the `iss` group and ignores `fields`.
    badge_page = {
        "kind": "issmap",
        "title": "ISS",
        "fields": [],
        "slots": {},
    }

    page_settings = (
        {"key": "follow", "label": "Camera", "type": "choice",
         "options": ["whole world", "follow"], "default": "whole world",
         "hint": "The whole world with the station crossing it, or closed in and travelling "
                 "with it"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._where = {}
        self._track = []
        self._track_from = 0
        self._crew = []
        self._lock = threading.Lock()
        self._next_where = 0.0
        self._next_track = 0.0
        self._next_crew = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    def start(self):
        """Restore the last position, then fetch the track and crew on a thread."""
        self._where = self.store.get(LAST) or {}
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-iss")
            self._fetcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._fetcher is not None:
            self._fetcher.join(timeout=2.0)
            self._fetcher = None

    def _fetch_loop(self):
        while not self._stop.is_set():
            for what in (self._refresh_where, self._refresh_track, self._refresh_crew):
                if self._stop.is_set():
                    break
                try:
                    what()
                except Exception as exc:
                    # The fetcher must not die: the page would continue drawing the last
                    # position.
                    self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _read_settings(self):
        self.units = self.config.get("units") or "kilometres"
        if self.units not in ("kilometres", "miles"):
            self.units = "kilometres"
        self.crew_wanted = self.config.get("crew", True) is not False

    def configure(self, settings):
        super().configure(settings)
        self._read_settings()
        self._next_where = 0.0
        self._next_track = 0.0
        self._wake.set()

    def sample(self, frame, dt):
        """The position, ground track and crew last stored by the fetcher."""
        with self._lock:
            where = dict(self._where)
            track = list(self._track)
            track_from = self._track_from
            crew = list(self._crew)
        # Where now sits in the track, as a fractional index into it.
        flown = (time.time() - track_from) / TRACK_STEP_S if track else 0.0
        if where:
            # Seconds since the reading, for the badge to extrapolate from.
            where["age_s"] = max(0, int(time.time() - where.get("at", 0)))
        frame["iss"] = {
            "where": where,
            "track": track,
            "flown": round(flown, 2),
            "aboard": len(crew) if self.crew_wanted else None,
            "lat": where.get("lat"),
            "lon": where.get("lon"),
            "altitude": where.get("altitude"),
            "speed": where.get("speed"),
        }

    def _refresh_where(self):
        if time.monotonic() < self._next_where:
            return
        try:
            payload = _get(WHERE)
        except Exception:
            self._next_where = time.monotonic() + RETRY_AFTER
            raise
        where = {
            "lat": round(float(payload["latitude"]), 3),
            "lon": round(float(payload["longitude"]), 3),
            "altitude": _distance(payload.get("altitude"), self.units),
            "speed": _distance(payload.get("velocity"), self.units),
            "sunlit": payload.get("visibility") != "eclipsed",
            "unit": "km" if self.units == "kilometres" else "mi",
            "solar_lat": round(float(payload["solar_lat"]), 2),
            "solar_lon": round(float(payload["solar_lon"]), 2),
            "at": int(payload.get("timestamp") or time.time()),
        }
        with self._lock:
            self._where = where
        self.store.set(LAST, where)
        self._next_where = time.monotonic() + POSITION_EVERY
        self.note_ok()

    def _refresh_track(self):
        if time.monotonic() < self._next_track:
            return
        now = int(time.time())
        wanted = [now + step * TRACK_STEP_S
                  for step in range(-TRACK_BACK, TRACK_AHEAD + 1)]
        points = []
        try:
            for start in range(0, len(wanted), 10):
                chunk = wanted[start:start + 10]
                stamps = ",".join(str(when) for when in chunk)
                for entry in _get(f"{WHERE}/positions?timestamps={stamps}") or ():
                    points.append((round(float(entry["longitude"]), 2),
                                   round(float(entry["latitude"]), 2),
                                   0 if entry.get("visibility") == "eclipsed" else 1))
        except Exception:
            self._next_track = time.monotonic() + RETRY_AFTER
            raise
        with self._lock:
            self._track = points
            self._track_from = wanted[0]
        self._next_track = time.monotonic() + TRACK_EVERY
        self.note_ok()

    def _refresh_crew(self):
        if not self.crew_wanted or time.monotonic() < self._next_crew:
            return
        try:
            payload = _get(CREW)
        except Exception:
            self._next_crew = time.monotonic() + RETRY_AFTER
            raise
        aboard = [person.get("name") for person in payload.get("people") or ()
                  if person.get("craft") == "ISS"]
        with self._lock:
            self._crew = [name for name in aboard if name]
        self._next_crew = time.monotonic() + CREW_EVERY
        self.note_ok()


def _get(url):
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _distance(value, units):
    """The feed returns kilometres; converted to miles on request."""
    if value is None:
        return None
    value = float(value)
    return round(value if units == "kilometres" else value * 0.621371, 1)
