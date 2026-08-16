"""Turning a location into coordinates, once per install.

A clock page is drawn for a location, and so is anything weather-shaped after it. One
lookup here serves all of them, against one cache: asking twice means geocoding twice and
holding two answers that can disagree.

Open-Meteo's geocoder, which needs neither key nor account.
"""

import json
import time
import urllib.parse
import urllib.request

from . import state

SEARCH = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT = 8.0
# Seconds before a name that failed is looked up again. The geocoder is the part of this
# most likely to rate limit, and a page redrawing does not need to find that out again.
RETRY_AFTER = 60.0

# What the badge-wide location is stored under, and what a page overrides it with.
KEYS = ("place", "latitude", "longitude")


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
