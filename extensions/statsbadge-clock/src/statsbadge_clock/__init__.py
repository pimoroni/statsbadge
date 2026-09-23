"""A clock and weather page."""

import os
import threading
import time

from statsbadge.sources import web
from statsbadge.sources.base import PollingSource

HERE = os.path.dirname(os.path.abspath(__file__))

# Open-Meteo allows 10,000 calls a day.
FETCH_INTERVAL = 900.0
RETRY_AFTER = 60.0

# Open-Meteo units to what the badge shows.
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


# The icon for each condition, a character in badge/icons.af.
ICONS = {
    "clear": "a", "fair": "c", "cloudy": "e", "overcast": "f", "fog": "g",
    "drizzle": "h", "rain": "i", "heavy rain": "j", "downpour": "j", "sleet": "k",
    "showers": "l", "snow": "m", "heavy snow": "n", "thunder": "o",
}
# Night variants, also in badge/icons.af.
NIGHT_ICONS = {"clear": "b", "fair": "d"}


def _page_target(page):
    """Return (key, place, latitude, longitude) for a page, or None if it names nowhere."""
    latitude, longitude = page.get("latitude"), page.get("longitude")
    if latitude is not None and longitude is not None:
        return (f"{float(latitude):.4f},{float(longitude):.4f}", None,
                float(latitude), float(longitude))
    place = (page.get("place") or "").strip()
    if place:
        return (place.lower(), place, None, None)
    return None


def _clock_at(utc_offset):
    """Return the clock fields for a place, from its offset east of UTC."""
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


class Clock(PollingSource):
    name = "clock"
    provides = ("clock", "weather")

    # The badge-side code, pushed by `statsbadge install` and imported by the app.
    badge_module = os.path.join(HERE, "badge", "clockface.py")

    # Weather symbols (icons.af), the DSEG7 font (lcd.af), and a digits-only Lexend
    # variant (digits.af).
    badge_assets = (os.path.join(HERE, "badge", "icons.af"),
                    os.path.join(HERE, "badge", "lcd.af"),
                    os.path.join(HERE, "badge", "digits.af"))

    settings = (
        {"key": "place", "label": "Default place", "type": "text",
         "hint": "A town or city, and a country if the name is a common one: Sheffield, "
                 "or Sheffield, US. Leave empty to use the badge's location"},
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

    # Clockface ignores fields: it is not customisable beyond look and location.
    badge_page = {
        "kind": "clockface",
        "title": "Clock",
        "fields": [],
        "slots": {},
    }

    # Quick Add, where a face is picked without opening the page.
    badge_recipes = (
        {"name": "digital", "title": "Digital Clock",
         "summary": "The time as big digits in the badge theme, with local weather.",
         "pages": [{"kind": "clockface", "title": "Clock", "fields": [],
                    "face": "digital"}]},
        {"name": "station", "title": "Station Clock",
         "summary": "The railway clock face, with local weather.",
         "pages": [{"kind": "clockface", "title": "Clock", "fields": [],
                    "face": "railway"}]},
    )

    # Per page settings, so two clock pages can show two cities. Open-Meteo returns a
    # location's UTC offset with its forecast, so no timezone is set.
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
         "options": ["railway", "dots", "amsterdam", "squircle", "digital", "lcd"],
         "default": "railway",
         "hint": "railway is the station clock, dots is a dotted minute track, amsterdam "
                 "is the platform clock with a stop-to-go second hand, squircle "
                 "and digital adopt badge's theme, lcd is seven-segment digits over "
                 "their unlit segments"},
        {"key": "themed", "label": "Theme colours", "type": "bool", "default": False,
         "hint": "Draw a face that carries its own livery in the page theme instead. "
                 "Squircle and the digital faces are already themed either way"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._weather = {}
        self._next_weather = 0.0
        self._targets = {}
        self._page_order = []
        self._lock = threading.Lock()
        self._read_settings()

    def pages(self, instances):
        """Record the location each configured page is set to."""
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
        self.wake()

    def _read_settings(self):
        self.units = self.config["units"]
        self.wind_units = self.config["wind_units"]

    def configure(self, settings):
        """Update a location while running."""
        super().configure(settings)
        self._read_settings()
        self._next_weather = 0.0
        self.wake()

    def sample(self, frame, dt):
        """Return the host clock, and whatever forecast the fetcher last stored."""
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
        """Return one entry per page, its weather and that place's clock, by page id."""
        with self._lock:
            order, targets = list(self._page_order), dict(self._targets)
        out = {}
        for page_id, key in order:
            spec = targets.get(key)
            if spec and spec["data"]:
                out[page_id] = dict(spec["data"],
                                    **_clock_at(spec["data"].get("utc_offset")))
        return out

    def poll(self):
        """Fetch the default location and any page location whose timer has elapsed."""
        if time.monotonic() >= self._next_weather:
            self._next_weather = time.monotonic() + RETRY_AFTER
            try:
                # The clock's own default where it has one, else the badge's.
                where = self.location(self.config)
                self._weather = self._fetch(where) if where else {}
                if where:
                    self._next_weather = time.monotonic() + FETCH_INTERVAL
                self.note_ok("default")
            except Exception as exc:  # noqa: BLE001
                self.note_fault(exc, key="default")
        # The pages' places. Fetched outside the lock, against a snapshot: a spec dropped
        # meanwhile is written to and discarded.
        with self._lock:
            specs = list(self._targets.items())
        for key, spec in specs:
            if time.monotonic() < spec["next"]:
                continue
            spec["next"] = time.monotonic() + RETRY_AFTER
            try:
                if spec["lat"] is None or spec["lon"] is None:
                    found = self.geocode.lookup(spec["place"])
                    if not found:
                        continue
                    spec["lat"], spec["lon"], spec["label"] = found
                spec["data"] = self._fetch(
                    (spec["lat"], spec["lon"], spec["label"] or spec["place"]),
                    local_time=True)
                spec["next"] = time.monotonic() + FETCH_INTERVAL
                self.note_ok(key)
            except Exception as exc:  # noqa: BLE001
                self.note_fault(exc, key=key)

    @staticmethod
    def _today(series):
        """Return the first day of a daily series, or None where the forecast carried none."""
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
        payload = web.fetch_json(url, timeout=8)
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
