"""Where the space station is, for the badge to draw on a world map.

Three feeds, none of which needs a key or an account:

    wheretheiss.at   where it is now, and where it will be. Its reply carries the sub-solar
                     point as well, which is the whole of the day and night terminator, so
                     nothing here works out where the sun is.
    open-notify.org  who is aboard.

The ground track is asked for rather than integrated: the same endpoint answers a list of
timestamps, so forty-five minutes either side of now comes back as twenty positions and the
badge has a track to draw the moment it turns the page. Keeping a trail of observed positions
instead would have drawn nothing until the app had been up for most of an orbit.
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

# How often each thing is asked for. The station covers 0.066 degrees a second, so five
# seconds is a pixel of movement on a whole-world map: enough to be live, and nothing like
# wheretheiss.at's one request a second.
POSITION_EVERY = 5.0
# The track is a prediction from now, so it goes stale as now moves on.
TRACK_EVERY = 120.0
CREW_EVERY = 3600.0
# A failure waits this long rather than the whole interval, and rather than never.
RETRY_AFTER = 30.0
FETCH_POLL = 1.0

# The track, in five minute steps behind and ahead. Ten timestamps a request is what the
# endpoint takes, so this is two of them, and ninety-five minutes is about one orbit.
TRACK_STEP_S = 300
TRACK_BACK = 9
TRACK_AHEAD = 10
# Where the last position is kept, so a badge switched on before the network is up has
# something to draw.
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

    # No field slots: the renderer draws from its own group and never reads `fields`.
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
        # When the first point of the track is for, so "now" can be placed in it as it moves.
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
        """Take up where the last run left off, then fetch on a thread of its own.

        Nothing in `sample` may wait on a network: every source shares the collector's thread
        and the first sample is taken while the server is still starting up.
        """
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
                    # The fetcher must not die, or the page would go on drawing the last
                    # position with nothing ever replacing it.
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
        """Whatever the fetcher has already brought back. Nothing here touches the network."""
        with self._lock:
            where = dict(self._where)
            track = list(self._track)
            track_from = self._track_from
            crew = list(self._crew)
        # Where "now" sits in the track, as an index into it: the run is a prediction from
        # when it was asked for, so the station moves along it between fetches and the badge
        # draws what is behind differently from what is ahead.
        flown = (time.time() - track_from) / TRACK_STEP_S if track else 0.0
        if where:
            # Seconds since the reading, so the badge can say how live it is and carry the
            # station on from there rather than drawing a stale dot as though it were now.
            where["age_s"] = max(0, int(time.time() - where.get("at", 0)))
        frame["iss"] = {
            "where": where,
            "track": track,
            "flown": round(flown, 2),
            # The count, not the names: the page has room for "9 aboard" and the frame is sent
            # again every second, so nine names would be a hundred and fifty bytes of nothing.
            "aboard": len(crew) if self.crew_wanted else None,
            # For anything wanting a number rather than a map.
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
            # The sub-solar point, which is the terminator: the badge builds the curve from
            # these two and nothing here has to know what day it is.
            "solar_lat": round(float(payload["solar_lat"]), 2),
            "solar_lon": round(float(payload["solar_lon"]), 2),
            "at": int(payload.get("timestamp") or time.time()),
        }
        with self._lock:
            self._where = where
        self.store.set(LAST, where)
        self._next_where = time.monotonic() + POSITION_EVERY

    def _refresh_track(self):
        if time.monotonic() < self._next_track:
            return
        now = int(time.time())
        wanted = [now + step * TRACK_STEP_S
                  for step in range(-TRACK_BACK, TRACK_AHEAD + 1)]
        points = []
        try:
            # Ten timestamps a request, so the run is asked for in chunks of ten.
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


def _get(url):
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _distance(value, units):
    """The feed answers in kilometres; miles if that is what was asked for."""
    if value is None:
        return None
    value = float(value)
    return round(value if units == "kilometres" else value * 0.621371, 1)
