"""A clock and weather page, as a worked example of an extension.

Two things an extension can do, both shown here:

1. Put data in the frame under its own group name. The badge's built-in page kinds can
   draw it with no badge-side code at all - `clock.time` in a `text` page just works.
2. Ship badge-side Python for a page the built-in kinds cannot draw. `badge/clockface.py`
   registers a `clockface` kind, and `statsbadge install --with-extensions` pushes it
   into the app's `ext/` directory.

Weather comes from Open-Meteo, which needs no API key and no account. Location is
whatever the config gives, or a guess from the host's timezone.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from statsbadge.sources.base import Source

HERE = os.path.dirname(os.path.abspath(__file__))

# Open-Meteo's numeric weather codes, collapsed to what fits on a badge.
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


class Clock(Source):
    name = "clock"
    provides = ("clock", "weather")

    # Pushed to the badge by `statsbadge install --with-extensions`, and imported by
    # the app so its page kind is available.
    badge_module = os.path.join(HERE, "badge", "clockface.py")

    # Weather symbols, built from icons.txt by tools/make_icon_font.py. Pushed to the
    # badge beside the module, which loads it with font.load().
    badge_assets = (os.path.join(HERE, "badge", "icons.af"),)

    # Offered in the config UI, which stores them and hands them back through
    # configure(). Weather is off until a location is set, so the place comes first and
    # carries the explanation; coordinates are for pinning it exactly.
    settings = (
        {"key": "place", "label": "Place", "type": "text",
         "hint": "A town or city, and a country if the name is a common one: "
                 "Sheffield, or Sheffield, US. Weather stays off until one is set"},
        {"key": "latitude", "label": "Latitude", "type": "number",
         "hint": "Used instead of the place, for a spot no name lands on"},
        {"key": "longitude", "label": "Longitude", "type": "number"},
        {"key": "units", "label": "Temperature", "type": "choice",
         "options": ["celsius", "fahrenheit"], "default": "celsius"},
    )

    # Offered in the config UI's page list.
    badge_page = {
        "kind": "clockface",
        "title": "Clock",
        "fields": ["clock.time", "clock.date", "weather.temp", "weather.condition"],
    }

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._weather = {}
        self._next_weather = 0.0
        # Open-Meteo asks for no more than a request every few minutes per location.
        self._interval = float(config.get("weather_interval", 900))
        self._read_settings()

    def _read_settings(self):
        was = getattr(self, "place", None)
        self.place = (self.config.get("place") or "").strip()
        self.latitude = self.config.get("latitude")
        self.longitude = self.config.get("longitude")
        self.units = self.config.get("units", "celsius")
        # What the place name resolved to, kept so a name costs one lookup rather than one
        # per forecast. configure() runs on every save, so an unchanged name keeps it.
        if was != self.place or not hasattr(self, "_located"):
            self._located = None
            self._located_for = None

    def configure(self, settings):
        """Take a location while running.

        The values are copied out in __init__, so they have to be copied again here. The
        next sample refetches rather than waiting out the rest of the interval, because a
        location typed in the browser should show up on the badge and not in a quarter of
        an hour.
        """
        super().configure(settings)
        self._read_settings()
        self._next_weather = 0.0

    def sample(self, frame, dt):
        now = time.localtime()
        frame["clock"] = {
            "time": time.strftime("%H:%M", now),
            "date": time.strftime("%a %d %b", now),
            "seconds": now.tm_sec,
            "hour": now.tm_hour,
            "minute": now.tm_min,
        }
        where = self._where()
        if where is None:
            frame["weather"] = {}
            return
        if time.monotonic() >= self._next_weather:
            self._next_weather = time.monotonic() + self._interval
            try:
                self._weather = self._fetch(where)
            except Exception as exc:
                self.note_fault(exc)
        frame["weather"] = dict(self._weather)

    def _where(self):
        """Coordinates to ask about, and the name to show for them.

        Coordinates win where they are given, being the more specific answer. Otherwise
        the place name is looked up once and kept, because it cannot change until the
        setting does.
        """
        if self.latitude is not None and self.longitude is not None:
            return (self.latitude, self.longitude, None)
        if not self.place:
            return None
        if self._located_for != self.place:
            self._located_for = self.place
            try:
                self._located = self._geocode(self.place)
            except Exception as exc:
                self._located = None
                self.note_fault(exc)
        return self._located

    def _geocode(self, place):
        """A place name to coordinates, through Open-Meteo's own geocoder.

        No key and no account, like the forecast. A name after a comma is matched against
        the country, so "Sheffield, US" gets Alabama and "Sheffield" gets the one most
        people mean: results arrive ordered by how well known they are.
        """
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
            raise LookupError(f"nowhere called {place!r}")
        match = found[0]
        if country:
            for candidate in found:
                if country in (candidate.get("country_code", "").lower(),
                               candidate.get("country", "").lower()):
                    match = candidate
                    break
        label = ", ".join(part for part in (match.get("name"),
                                            match.get("country_code")) if part)
        return (match["latitude"], match["longitude"], label)

    def _fetch(self, where):
        latitude, longitude, label = where
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}"
            "&current=temperature_2m,relative_humidity_2m,"
            "apparent_temperature,weather_code,wind_speed_10m"
            f"&temperature_unit="
            f"{'fahrenheit' if self.units == 'fahrenheit' else 'celsius'}"
        )
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        current = payload.get("current", {})
        code = current.get("weather_code")
        return {
            "temp": current.get("temperature_2m"),
            "feels": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "condition": CONDITIONS.get(code, "?") if code is not None else None,
            "code": code,
            "place": label,
        }
