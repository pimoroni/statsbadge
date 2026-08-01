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

# What Open-Meteo calls a unit, against what it should be labelled as on the badge.
TEMPERATURE_UNITS = {"celsius": "C", "fahrenheit": "F"}
WIND_UNITS = {"kmh": "km/h", "mph": "mph", "ms": "m/s", "kn": "kn"}

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


# The symbol for each condition, as a character in badge/icons.af. Kept here beside
# CONDITIONS rather than on the badge, so there is one mapping and icons.txt is the only
# other place the letters appear.
ICONS = {
    "clear": "a", "fair": "c", "cloudy": "e", "overcast": "f", "fog": "g",
    "drizzle": "h", "rain": "i", "heavy rain": "j", "downpour": "j", "sleet": "k",
    "showers": "l", "snow": "m", "heavy snow": "n", "thunder": "o",
}
# Night has its own symbol where there is one to have.
NIGHT_ICONS = {"clear": "b", "fair": "d"}


def _page_target(page):
    """(key, place, latitude, longitude) for a page, or None if it names nowhere.

    Coordinates win over a name, being the more specific answer, which is the rule the
    extension-wide default follows too. The key is what identifies the *location*, so
    pages pointed at the same one share a request.
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

    # Pushed to the badge by `statsbadge install --with-extensions`, and imported by
    # the app so its page kind is available.
    badge_module = os.path.join(HERE, "badge", "clockface.py")

    # Weather symbols, built from icons.txt by tools/make_icon_font.py, and the LCD face's
    # seven-segment digits. Pushed to the badge beside the module, which loads them with
    # font.load().
    badge_assets = (os.path.join(HERE, "badge", "icons.af"),
                    os.path.join(HERE, "badge", "lcd.af"))

    # Offered in the config UI, which stores them and hands them back through
    # configure(). Weather is off until a location is set, so the place comes first and
    # carries the explanation; coordinates are for pinning it exactly.
    settings = (
        {"key": "place", "label": "Default place", "type": "text",
         "hint": "Used by any clock page that does not name its own. A town or city, "
                 "and a country if the name is a common one: Sheffield, or "
                 "Sheffield, US. Weather stays off until somewhere is set"},
        {"key": "latitude", "label": "Default latitude", "type": "number",
         "hint": "Instead of the name, for a spot no name lands on"},
        {"key": "longitude", "label": "Default longitude", "type": "number"},
        {"key": "units", "label": "Temperature", "type": "choice",
         "options": ["celsius", "fahrenheit"], "default": "celsius"},
        {"key": "wind_units", "label": "Wind speed", "type": "choice",
         "options": sorted(WIND_UNITS), "default": "kmh"},
    )

    # Offered in the config UI's page list.
    # No field slots: the renderer draws from its own groups and never reads `fields`,
    # so offering pickers for them offered controls that did nothing.
    badge_page = {
        "kind": "clockface",
        "title": "Clock",
        "fields": [],
        "slots": {},
    }

    # Per page, so two clock pages can show two cities. Open-Meteo returns a location's
    # UTC offset with its forecast, so a place settles the time as well as the weather
    # and there is no separate timezone to set.
    page_settings = (
        {"key": "place", "label": "Place", "type": "text",
         "hint": "Where this page shows. Its weather and its local time both follow "
                 "from it. Empty falls back to the default place"},
        {"key": "latitude", "label": "Latitude", "type": "number",
         "hint": "Instead of the name, for a spot no name lands on"},
        {"key": "longitude", "label": "Longitude", "type": "number"},
        {"key": "face", "label": "Face", "type": "choice",
         "options": ["railway", "dots", "squircle", "digital", "lcd"],
         "default": "railway",
         "hint": "railway is the station clock, dots is a dotted minute track, squircle "
                 "and digital take the badge's theme, lcd is seven-segment digits over "
                 "their own unlit segments"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        self._weather = {}
        self._next_weather = 0.0
        # Where the pages look, keyed by location, and which page wants which.
        self._targets = {}
        self._page_order = []
        # Open-Meteo asks for no more than a request every few minutes per location.
        self._interval = float(config.get("weather_interval", 900))
        self._read_settings()

    def pages(self, instances):
        """Where each of this source's pages wants to look.

        Called whenever the config changes, so a place typed in the browser is fetched on
        the next sample rather than at the next restart.

        Two maps, because they answer different questions: pages are keyed by page id, so
        the badge can find its own entry without deriving a key, and locations are keyed
        by where they are, so two pages showing one city cost one request.
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
        # Carry over what has already been fetched for somewhere still wanted; anywhere
        # no page asks for now is dropped, along with its timer.
        for key, spec in targets.items():
            was = self._targets.get(key) or {}
            spec["data"] = was.get("data", {})
            spec["next"] = was.get("next", 0.0)
            spec["label"] = was.get("label")
            if spec["lat"] is None:
                spec["lat"], spec["lon"] = was.get("lat"), was.get("lon")
        self._page_order = order
        self._targets = targets

    def _read_settings(self):
        was = getattr(self, "place", None)
        self.place = (self.config.get("place") or "").strip()
        self.latitude = self.config.get("latitude")
        self.longitude = self.config.get("longitude")
        self.units = self.config.get("units", "celsius")
        self.wind_units = self.config.get("wind_units", "kmh")
        if self.wind_units not in WIND_UNITS:
            self.wind_units = "kmh"
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
        # Before the global place is considered: a page can carry its own, and often the
        # only places set are the pages'.
        frame["places"] = self._sample_places()
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

    def _sample_places(self):
        """One entry per page, its weather and that place's own clock, keyed by page id.

        The clock fields come from the location's UTC offset, which the forecast returns,
        so a page showing another city shows its time without the badge knowing anything
        about timezones.
        """
        for spec in self._targets.values():
            if time.monotonic() < spec["next"]:
                continue
            spec["next"] = time.monotonic() + self._interval
            try:
                if spec["lat"] is None or spec["lon"] is None:
                    found = self._geocode(spec["place"])
                    if not found:
                        continue
                    spec["lat"], spec["lon"], spec["label"] = found
                spec["data"] = self._fetch(
                    (spec["lat"], spec["lon"], spec["label"] or spec["place"]),
                    local_time=True)
            except Exception as exc:
                self.note_fault(exc)

        out = {}
        for page_id, key in self._page_order:
            spec = self._targets.get(key)
            if spec and spec["data"]:
                out[page_id] = dict(spec["data"],
                                    **_clock_at(spec["data"].get("utc_offset")))
        return out

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
        )
        if local_time:
            # Asks for the location's own offset, which is what a per-place clock needs.
            url += "&timezone=auto"
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        current = payload.get("current", {})
        code = current.get("weather_code")
        condition = CONDITIONS.get(code, "?") if code is not None else None
        # is_day is 1 or 0, and absent on a response that predates it.
        night = current.get("is_day") == 0
        icon = NIGHT_ICONS.get(condition) if night else None
        return {
            "temp": current.get("temperature_2m"),
            "feels": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "condition": condition,
            "code": code,
            "place": label,
            # Units travel with the numbers: the badge has no way to know which was asked
            # for, and a temperature with no scale on it is worse than none.
            "temp_unit": TEMPERATURE_UNITS.get(self.units, "C"),
            "wind_unit": WIND_UNITS[self.wind_units],
            "icon": icon or ICONS.get(condition),
            # Seconds east of UTC for this location, which is what makes a per-place
            # clock possible. Absent unless timezone=auto was asked for.
            "utc_offset": payload.get("utc_offset_seconds"),
        }
