"""Turning a location into coordinates, once per install, and back again.

A clock page is drawn for a location, and so is anything weather-shaped after it. One
lookup here serves all of them, against one cache: asking twice means geocoding twice and
holding two answers that can disagree.

`lookup` is Open-Meteo's geocoder, which needs neither key nor account. `nearest` runs the
other way, naming a coordinate that arrived without one - a burnt area, a quake, whatever
the space station is over. That direction is answered from `cities.tsv.gz`. Open-Meteo has
no reverse endpoint, and the services that do are shared ones whose usage policies rule out
software installed on many machines querying them systematically.
"""

import gzip
import json
import math
import time
import urllib.parse
import urllib.request
from importlib import resources

from . import state

SEARCH = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT = 8.0
# Seconds before a name that failed is looked up again. The geocoder is the part of this
# most likely to rate limit, and a page redrawing does not need to find that out again.
RETRY_AFTER = 60.0

# What the badge-wide location is stored under, and what a page overrides it with.
KEYS = ("place", "latitude", "longitude")

# GeoNames cities15000, packed by tools/make_cities.py. CC BY 4.0.
CITIES = "cities.tsv.gz"
# How much further than the closest settlement a bigger one may be and still be the name
# given. Los Angeles is ringed by towns of their own, and "8 km NE of Rosemead" names a
# fire nobody can place.
NEAR_ENOUGH_KM = 25.0
EARTH_KM = 6371.0
# Bearings are named to sixteen points, matching the USGS strings the quakes page already
# draws: "77 km N of Ruteng", "67 km WSW of Puerto Madero".
POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


class Geocoder:
    """Locations to (latitude, longitude, label), cached and backed off per name.

    `Geocoder()` caches in memory. The server hands it a store, so a location costs one
    request for the life of an install and a known location still resolves with Open-Meteo
    unreachable.
    """

    def __init__(self, store=None):
        self.store = store or state.Store()
        self._retry_at = {}

    def lookup(self, place):
        """Coordinates for a location, or None while a failed lookup is backed off.

        Raises what the request raised, and LookupError where the name finds nothing, so a
        misspelled location is reported instead of drawing nothing and saying nothing.
        """
        key = (place or "").strip().lower()
        if not key:
            return None
        cached = self.store.get(key)
        if cached and len(cached) == 3:
            return (cached[0], cached[1], cached[2])
        if time.monotonic() < self._retry_at.get(key, 0.0):
            return None
        try:
            found = self._search(key)
        except Exception:
            self._retry_at[key] = time.monotonic() + RETRY_AFTER
            raise
        self.store.set(key, list(found))
        return found

    def nearest(self, latitude, longitude):
        """`nearest`, reachable from the geocoder a source is already handed."""
        return nearest(latitude, longitude)

    def _search(self, place):
        name, _, country = place.partition(",")
        name, country = name.strip(), country.strip().lower()
        if not name:
            raise LookupError(f"could not find {place!r}")
        url = (f"{SEARCH}?name={urllib.parse.quote(name)}"
               "&count=10&language=en&format=json")
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            found = json.loads(response.read().decode("utf-8")).get("results") or []
        if not found:
            raise LookupError(f"could not find {place!r}")
        match = _in_country(found, country) if country else found[0]
        label = ", ".join(part for part in
                          (match.get("name"), match.get("country_code")) if part)
        return (match["latitude"], match["longitude"], label)


def _in_country(found, country):
    """The first result in a named country, or the best known of them all.

    Results arrive ordered by how well known they are, so "Sheffield" is Yorkshire's and
    "Sheffield, US" is Alabama's. A country matches on its code and on its name, since
    somebody typing a country has no reason to know which of the two Open-Meteo answers with.
    """
    for candidate in found:
        if country in (candidate.get("country_code", "").lower(),
                       candidate.get("country", "").lower()):
            return candidate
    return found[0]


def home_from(config):
    """The badge-wide location out of a host config, as a source is handed it."""
    return {key: (config or {}).get(key) for key in KEYS
            if (config or {}).get(key) not in (None, "")}


# The settlement table, read on the first call that needs it. A host drawing nothing that
# arrives as bare coordinates never pays for the read.
_cities = None


def cities():
    """Every packed settlement, as (name, country, latitude, longitude, thousands)."""
    global _cities
    if _cities is None:
        packed = resources.files(__package__).joinpath(CITIES).read_bytes()
        found = []
        for line in gzip.decompress(packed).decode("utf-8").splitlines():
            if line.startswith("#"):
                continue
            name, country, latitude, longitude, thousands = line.split("\t")
            found.append((name, country, float(latitude), float(longitude),
                          int(thousands)))
        _cities = found
    return _cities


def nearest(latitude, longitude):
    """What to call a coordinate that arrived without a name, or None with no table.

    `{"name", "country", "km", "bearing", "text"}`, where `text` reads "62 km NE of
    Castelo Branco, PT" - the way USGS names a quake, so a page can draw it beside one.

    The closest settlement is not always the one to name. A fire in the hills above a city
    is closest to some suburb of it, so the largest settlement within NEAR_ENOUGH_KM of the
    closest is the one given.
    """
    table = cities()
    if not table:
        return None
    # Longitude degrees shrink toward the poles, so they are scaled to compare against
    # latitude degrees. Ranking on that flat approximation and measuring only the winner
    # properly costs one trig call rather than one per settlement.
    scale = math.cos(math.radians(latitude))
    tolerance = NEAR_ENOUGH_KM / (EARTH_KM * math.pi / 180.0)
    best = reach = None
    close = []
    for row in table:
        north = row[2] - latitude
        east = _wrap(row[3] - longitude) * scale
        away = north * north + east * east
        if best is None or away < best[0]:
            best = (away, row)
            reach = (math.sqrt(away) + tolerance) ** 2
        if away <= reach:
            close.append((away, row))
    # Filtered again: `reach` shrank as nearer settlements turned up, so what went in
    # early can be outside the reach that ended up applying.
    contenders = [row for away, row in close if away <= reach]
    name, country, city_lat, city_lon, _thousands = (
        max(contenders, key=lambda row: row[4]) if contenders else best[1])

    km = round(_km_between(city_lat, city_lon, latitude, longitude))
    bearing = _bearing(city_lat, city_lon, latitude, longitude)
    # A coordinate on the town itself is named after it, since "0 km SW of Sheffield" is
    # a direction nobody has to travel.
    text = f"{km} km {bearing} of {name}, {country}" if km else f"{name}, {country}"
    return {"name": name, "country": country, "km": km, "bearing": bearing, "text": text}


def _wrap(degrees):
    """A longitude difference the short way round, so either side of the date line is near."""
    return (degrees + 180.0) % 360.0 - 180.0


def _km_between(from_lat, from_lon, to_lat, to_lon):
    first, second = math.radians(from_lat), math.radians(to_lat)
    along = math.radians(_wrap(to_lon - from_lon))
    corner = (math.sin((second - first) / 2) ** 2
              + math.cos(first) * math.cos(second) * math.sin(along / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(corner)))


def _bearing(from_lat, from_lon, to_lat, to_lon):
    """Which way the coordinate lies from the settlement, to one of sixteen points."""
    first, second = math.radians(from_lat), math.radians(to_lat)
    along = math.radians(_wrap(to_lon - from_lon))
    east = math.sin(along) * math.cos(second)
    north = (math.cos(first) * math.sin(second)
             - math.sin(first) * math.cos(second) * math.cos(along))
    return POINTS[round(math.degrees(math.atan2(east, north)) % 360.0 / 22.5) % 16]
