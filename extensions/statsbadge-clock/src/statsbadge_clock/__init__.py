"""A clock and weather page.

Demos three things an extension can do:

1. Put data in the frame under a group name. The badge's built-in pages can
   draw the time with no badge-side code at all - `clock.time` in a `text` page just works.
2. Ship badge-side Python to add a new page to the app. `badge/clockface.py`
   registers a `clockface`, and `statsbadge install` pushes it into the app's `ext/` directory.
3. Save settings. `self.store` is a namespaced dict the host persists, the
   resolved location coordinates are saved/cached there.

Weather comes from Open-Meteo, which requires neither API nor account.

Where the host calls in:

    available()            extensions.load, before constructing
    __init__(config)       extensions.load, with config["extensions"]["clock"]
    start() / stop()       Collector.start / stop; self.store is persistent by now
    sample(frame, dt)      Collector, every tick, on its thread
    configure(settings)    server.replace_config, on every UI save
    pages(instances)       server.announce_pages at startup, replace_config after
    settings               the UI form, and layout validation drops undeclared keys
    page_settings          per-kind page validation
    badge_page             the UI page list, and the kinds the validator accepts
    badge_module/_assets   pushed to the badge by statsbadge install
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from statsbadge.sources.base import Source

HERE = os.path.dirname(os.path.abspath(__file__))

# How often the fetch thread wakes to check timers
FETCH_POLL = 1.0
# Seconds before a location is fetched again. Open-Meteo allows 10,000 calls a day.
FETCH_INTERVAL = 900.0
# Seconds before a failed fetch is retried. Avoids a looong wait on a transient failure.
RETRY_AFTER = 60.0
# Store key for resolved coordinates
GEOCODED = "geocoded"

# LUT to map Open-Meteo units to what's displayed on the badge.
TEMPERATURE_UNITS = {"celsius": "C", "fahrenheit": "F"}
WIND_UNITS = {"kmh": "km/h", "mph": "mph", "ms": "m/s", "kn": "kn"}

# Open-Meteo's weather codes, collapsed to what fits on a badge.
CONDITIONS = {
    0: "clear", 1: "fair", 2: "cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "sleet", 57: "sleet",
    61: "rain", 63: "rain", 65: "heavy rain",
    66: "sleet", 67: "sleet",
    71: "snow", 73: "snow", 75: "heavy snow", 77: "snow",
    80: "showers", 81: "showers", 82: "downpour",
    85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}


# The icon for each condition (a character in badge/icons.af)
ICONS = {
    "clear": "a", "fair": "c", "cloudy": "e", "overcast": "f", "fog": "g",
    "drizzle": "h", "rain": "i", "heavy rain": "j", "downpour": "j", "sleet": "k",
    "showers": "l", "snow": "m", "heavy snow": "n", "thunder": "o",
}
# Separate icon variants for nighttime (also in badge/icons.af)
NIGHT_ICONS = {"clear": "b", "fair": "d"}


def _page_target(page):
    """(key, place, latitude, longitude) for a page, or None if it names nowhere.

    Coordinates win over a named location.
    """
    latitude, longitude = page.get("latitude"), page.get("longitude")
    if latitude is not None and longitude is not None:
        return (f"{float(latitude):.4f},{float(longitude):.4f}", None,
                float(latitude), float(longitude))
    place = (page.get("place") or "").strip()
    if place:
        return (place.lower(), place, None, None)
    return None


def _clock_at(utc_offset):
    """The clock fields for a place, from its offset east of UTC."""
    if utc_offset is None:
        return {}
    there = time.gmtime(time.time() + float(utc_offset))
    return {
        "time": time.strftime("%H:%M", there),
        "date": time.strftime("%a %d %b", there),
        "hour": there.tm_hour,
        "minute": there.tm_min,
        "seconds": there.tm_sec,
    }


class Clock(Source):
    name = "clock"
    provides = ("clock", "weather")

    # The badge-side code; pushed by `statsbadge install`, and imported by the app
    badge_module = os.path.join(HERE, "badge", "clockface.py")

    # Weather symbols (icons.af), built from icons.txt by tools/make_icon_font.py
    # The DSEG7 font (lcd.af)
    # A high-resolution digits-only Lexend variant (digits.af)
    badge_assets = (os.path.join(HERE, "badge", "icons.af"),
                    os.path.join(HERE, "badge", "lcd.af"),
                    os.path.join(HERE, "badge", "digits.af"))

    # Settings offered in the web config UI
    settings = (
        {"key": "place", "label": "Default place", "type": "text",
         "hint": "A town or city, and a country if the name is a common one: Sheffield, "
                 "or Sheffield, US. Weather is not displayed until a location is set"},
        {"key": "latitude", "label": "Default latitude", "type": "number",
         "min": -90, "max": 90, "step": 0.001, "unit": "degrees",
         "hint": "Instead of the name, for a spot no name lands on"},
        {"key": "longitude", "label": "Default longitude", "type": "number",
         "min": -180, "max": 180, "step": 0.001, "unit": "degrees"},
        {"key": "units", "label": "Temperature", "type": "choice",
         "options": ["celsius", "fahrenheit"], "default": "celsius"},
        {"key": "wind_units", "label": "Wind speed", "type": "choice",
         "options": sorted(WIND_UNITS), "default": "kmh"},
    )

    # Pages this app supplies, offered in the config UI's page list
    # Clockface ignores fields, since it's not customisable beyond look/location.
    badge_page = {
        "kind": "clockface",
        "title": "Clock",
        "fields": [],
        "slots": {},
    }

    # Per page settings, so two clock pages can show two cities.
    # Open-Meteo returns a location's UTC offset with its forecast, no need to set timezone
    page_settings = (
        {"key": "place", "label": "Place", "type": "text",
         "hint": "The clock's location, setting its weather and local time. "
                 "Leave empty to fall back to the global default location."},
        {"key": "latitude", "label": "Latitude", "type": "number",
         "min": -90, "max": 90, "step": 0.001, "unit": "degrees",
         "hint": "Instead of the name, for a spot no name lands on"},
        {"key": "longitude", "label": "Longitude", "type": "number",
         "min": -180, "max": 180, "step": 0.001, "unit": "degrees"},
        {"key": "face", "label": "Face", "type": "choice",
         "options": ["railway", "dots", "squircle", "digital", "lcd"],
         "default": "railway",
         "hint": "railway is the station clock, dots is a dotted minute track, squircle "
                 "and digital adopt badge's theme, lcd is seven-segment digits over "
                 "their unlit segments"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._weather = {}
        self._next_weather = 0.0
        self._retry_at = 0.0
        self._targets = {}
        self._page_order = []
        self._lock = threading.Lock()
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    def start(self):
        """A thread for API fetches, so they don't block the main thread."""
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-clock")
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
                # The fetcher must not die: clock pages would continue drawing the stale data.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def pages(self, instances):
        """Record the location each configured page is set to.

        Pairs are kept in page order so page_samples can key output by page id; targets are
        keyed by location so two pages showing one city share the one request.
        """
        order, targets = [], {}
        for page in instances:
            page_id = page.get("id")
            target = _page_target(page)
            if not page_id or target is None:
                continue
            key, place, latitude, longitude = target
            order.append((page_id, key))
            targets.setdefault(key, {"place": place, "lat": latitude,
                                     "lon": longitude})
        with self._lock:
            for key, spec in targets.items():
                was = self._targets.get(key) or {}
                spec["data"] = was.get("data", {})
                spec["next"] = was.get("next", 0.0)
                spec["label"] = was.get("label")
                if spec["lat"] is None:
                    spec["lat"], spec["lon"] = was.get("lat"), was.get("lon")
            self._page_order = order
            self._targets = targets
        # Signal the fetch thread to wake up
        self._wake.set()

    def _read_settings(self):
        was = getattr(self, "place", None)
        self.place = (self.config.get("place") or "").strip()
        self.latitude = self.config.get("latitude")
        self.longitude = self.config.get("longitude")
        self.units = self.config.get("units", "celsius")
        self.wind_units = self.config.get("wind_units", "kmh")
        if self.wind_units not in WIND_UNITS:
            self.wind_units = "kmh"
        if was != self.place or not hasattr(self, "_located"):
            self._located = None
            self._located_for = None

    def configure(self, settings):
        """Update a location while running.

        Zeroing both timers makes a save refetch immediately and drops any backoff a failed
        lookup left. Avoids a minute wait when correcting a misspelled location.
        """
        super().configure(settings)
        self._read_settings()
        self._next_weather = 0.0
        self._retry_at = 0.0
        self._wake.set()

    def sample(self, frame, dt):
        """The clock from the host, and whatever forecast the fetcher last stored."""
        now = time.localtime()
        frame["clock"] = {
            "time": time.strftime("%H:%M", now),
            "date": time.strftime("%a %d %b", now),
            "seconds": now.tm_sec,
            "hour": now.tm_hour,
            "minute": now.tm_min,
        }
        frame["places"] = self._places()
        frame["weather"] = dict(self._weather)

    def _places(self):
        """One entry per page, its weather and that place's clock, keyed by page id.

        The clock fields come from the location's UTC offset, which the forecast returns,
        so a page showing another city shows its time without the badge knowing anything
        about timezones.
        """
        with self._lock:
            order, targets = list(self._page_order), dict(self._targets)
        out = {}
        for page_id, key in order:
            spec = targets.get(key)
            if spec and spec["data"]:
                out[page_id] = dict(spec["data"],
                                    **_clock_at(spec["data"].get("utc_offset")))
        return out

    def _refresh(self):
        """Fetch the default location and any page location whose timer has elapsed.

        Runs on the fetcher thread. A failed fetch or geocode sets that timer to
        RETRY_AFTER instead of the full interval.
        """
        where = self._where()
        if where is None:
            self._weather = {}
        elif time.monotonic() >= self._next_weather:
            try:
                self._weather = self._fetch(where)
                self._next_weather = time.monotonic() + FETCH_INTERVAL
                self.note_ok()
            except Exception as exc:
                self._next_weather = time.monotonic() + RETRY_AFTER
                self.note_fault(exc)
        # The pages' places, which are often the only ones set. Fetched outside the lock,
        # against a snapshot: a spec dropped meanwhile is written to and discarded.
        with self._lock:
            specs = list(self._targets.values())
        for spec in specs:
            if time.monotonic() < spec["next"]:
                continue
            try:
                if spec["lat"] is None or spec["lon"] is None:
                    found = self._geocode(spec["place"])
                    if not found:
                        spec["next"] = time.monotonic() + RETRY_AFTER
                        continue
                    spec["lat"], spec["lon"], spec["label"] = found
                spec["data"] = self._fetch(
                    (spec["lat"], spec["lon"], spec["label"] or spec["place"]),
                    local_time=True)
                spec["next"] = time.monotonic() + FETCH_INTERVAL
                self.note_ok()
            except Exception as exc:
                spec["next"] = time.monotonic() + RETRY_AFTER
                self.note_fault(exc)

    def _where(self):
        """The default location as (latitude, longitude, label), or None if unset.

        Coordinates from config are returned with label None. A place name is geocoded once
        and cached until the setting changes. A failed lookup retries after RETRY_AFTER.
        """
        if self.latitude is not None and self.longitude is not None:
            return (self.latitude, self.longitude, None)
        if not self.place:
            return None
        if self._located is None or self._located_for != self.place:
            if time.monotonic() < self._retry_at:
                return None
            self._located_for = self.place
            try:
                self._located = self._geocode(self.place)
            except Exception as exc:
                self._located = None
                self._retry_at = time.monotonic() + RETRY_AFTER
                self.note_fault(exc)
        return self._located

    def _geocode(self, place):
        """Resolve a place name to coordinates using Open-Meteo's geocoder.

        A name after a comma is matched against the country, so "Sheffield, US"
        gets Alabama and "Sheffield" gets Sheffield-on-Sea, in very definitely
        certainly real Yorkshire and not the fake one.

        Geocode results arrive ordered by how well known they are.

        Lookups are cached in the store and never expire, so a name needs one lookup
        and a known place still resolves with the geocoder unreachable.
        """
        key = place.strip().lower()
        cached = (self.store.get(GEOCODED) or {}).get(key)
        if cached and len(cached) == 3:
            # Latitude, Longitude and Label (eg: Sheffield, GB)
            return (cached[0], cached[1], cached[2])

        name, _, country = place.partition(",")
        name = name.strip()
        country = country.strip().lower()

        if not name:
            return None

        url = ("https://geocoding-api.open-meteo.com/v1/search"
               f"?name={urllib.parse.quote(name)}&count=10&language=en&format=json")
        with urllib.request.urlopen(url, timeout=8) as response:
            found = json.loads(response.read().decode("utf-8")).get("results") or []
        if not found:
            raise LookupError(f"could not find {place!r}")
        match = found[0]
        if country:
            for candidate in found:
                if country in (candidate.get("country_code", "").lower(),
                               candidate.get("country", "").lower()):
                    match = candidate
                    break
        label = ", ".join(part for part in (match.get("name"),
                                            match.get("country_code")) if part)
        located = (match["latitude"], match["longitude"], label)
        table = dict(self.store.get(GEOCODED) or {})
        table[key] = list(located)
        self.store.set(GEOCODED, table)
        return located

    @staticmethod
    def _today(series):
        """The first day out of a daily series, or None where the forecast carried none."""
        if isinstance(series, list) and series and isinstance(series[0], (int, float)):
            return series[0]
        return None

    def _fetch(self, where, local_time=False):
        latitude, longitude, label = where
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}"
            "&current=temperature_2m,relative_humidity_2m,"
            "apparent_temperature,weather_code,wind_speed_10m,is_day"
            f"&temperature_unit="
            f"{'fahrenheit' if self.units == 'fahrenheit' else 'celsius'}"
            f"&wind_speed_unit={self.wind_units}"
            "&daily=temperature_2m_max,temperature_2m_min"
            # Without this daily[0] is the UTC day and utc_offset_seconds is 0.
            "&timezone=auto"
        )
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        current = payload.get("current", {})
        code = current.get("weather_code")
        condition = CONDITIONS.get(code, "?") if code is not None else None
        night = current.get("is_day") == 0
        icon = NIGHT_ICONS.get(condition) if night else None
        daily = payload.get("daily") or {}
        return {
            "high": self._today(daily.get("temperature_2m_max")),
            "low": self._today(daily.get("temperature_2m_min")),
            "temp": current.get("temperature_2m"),
            "feels": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "condition": condition,
            "code": code,
            "place": label,
            "temp_unit": TEMPERATURE_UNITS.get(self.units, "C"),
            "wind_unit": WIND_UNITS[self.wind_units],
            "icon": icon or ICONS.get(condition),
            "utc_offset": payload.get("utc_offset_seconds") if local_time else None,
        }
