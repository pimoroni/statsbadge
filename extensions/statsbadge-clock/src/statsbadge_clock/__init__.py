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
        self.latitude = config.get("latitude")
        self.longitude = config.get("longitude")
        self.units = config.get("units", "celsius")
        self._weather = {}
        self._next_weather = 0.0
        # Open-Meteo asks for no more than a request every few minutes per location.
        self._interval = float(config.get("weather_interval", 900))

    def sample(self, frame, dt):
        now = time.localtime()
        frame["clock"] = {
            "time": time.strftime("%H:%M", now),
            "date": time.strftime("%a %d %b", now),
            "seconds": now.tm_sec,
            "hour": now.tm_hour,
            "minute": now.tm_min,
        }
        if self.latitude is None or self.longitude is None:
            frame["weather"] = {}
            return
        if time.monotonic() >= self._next_weather:
            self._next_weather = time.monotonic() + self._interval
            try:
                self._weather = self._fetch()
            except Exception as exc:
                self.note_fault(exc)
        frame["weather"] = dict(self._weather)

    def _fetch(self):
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude={}&longitude={}&current=temperature_2m,relative_humidity_2m,"
            "apparent_temperature,weather_code,wind_speed_10m"
            "&temperature_unit={}"
        ).format(self.latitude, self.longitude,
             "fahrenheit" if self.units == "fahrenheit" else "celsius")
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
        }
