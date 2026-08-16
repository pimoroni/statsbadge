"""The shared geocoder, and the badge-wide location a source falls back to.

Nothing here reaches Open-Meteo: `urlopen` is replaced, so a test counts the requests a
real run would have made.
"""

import json
import tempfile

import pytest

from statsbadge import geocode, state
from statsbadge.sources.base import Source

SHEFFIELD = {"name": "Sheffield", "country": "United Kingdom", "country_code": "GB",
             "latitude": 53.38, "longitude": -1.47}
ALABAMA = {"name": "Sheffield", "country": "United States", "country_code": "US",
           "latitude": 34.76, "longitude": -87.7}


class Reply:
    """What `urlopen` hands back: a context manager over one JSON body."""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def answering(monkeypatch, results, store=None):
    """A geocoder returning `results`, and the list of URLs it was asked for."""
    asked = []

    def urlopen(url, **_named):
        asked.append(url)
        if isinstance(results, Exception):
            raise results
        return Reply({"results": results})

    monkeypatch.setattr(geocode.urllib.request, "urlopen", urlopen)
    return geocode.Geocoder(store), asked


def test_a_name_is_looked_up_once_and_kept(monkeypatch):
    """Two extensions asking for a town cost one request, which is the point of sharing."""
    finder, asked = answering(monkeypatch, [SHEFFIELD])

    assert finder.lookup("Sheffield") == (53.38, -1.47, "Sheffield, GB")
    assert finder.lookup("sheffield ") == (53.38, -1.47, "Sheffield, GB")
    assert len(asked) == 1, asked


def test_a_resolved_town_outlives_the_process(monkeypatch):
    """A store means the badge comes up knowing where it is with the geocoder unreachable."""
    with tempfile.TemporaryDirectory() as directory:
        store = state.Store(f"{directory}/geocode.json")
        finder, asked = answering(monkeypatch, [SHEFFIELD], store)
        finder.lookup("Sheffield")

        again, asked = answering(monkeypatch, RuntimeError("the geocoder is down"),
                                 state.Store(f"{directory}/geocode.json"))
        assert again.lookup("Sheffield") == (53.38, -1.47, "Sheffield, GB")
        assert asked == [], "asked for a place it had already resolved"


def test_a_country_after_the_comma_picks_that_one(monkeypatch):
    """Results arrive best-known first, and a country is the only way to reach the other."""
    finder, _asked = answering(monkeypatch, [SHEFFIELD, ALABAMA])
    assert finder.lookup("Sheffield")[:2] == (53.38, -1.47)

    # A country matches on its code and on its name, since somebody typing a country has no
    # reason to know which of the two Open-Meteo answers with.
    for named in ("Sheffield, US", "Sheffield, United States"):
        finder, _asked = answering(monkeypatch, [SHEFFIELD, ALABAMA])
        assert finder.lookup(named)[:2] == (34.76, -87.7), named


def test_a_name_that_finds_nothing_is_reported(monkeypatch):
    """LookupError and not None: a misspelled location reaches the config UI as a fault."""
    finder, _asked = answering(monkeypatch, [])
    with pytest.raises(LookupError):
        finder.lookup("Sheffieldshire-on-Trent")


def test_a_failed_lookup_is_backed_off(monkeypatch):
    """A page redrawing must not hammer a geocoder that is rate limiting it."""
    finder, asked = answering(monkeypatch, RuntimeError("429"))
    with pytest.raises(RuntimeError):
        finder.lookup("Sheffield")

    assert finder.lookup("Sheffield") is None, "raised again instead of backing off"
    assert len(asked) == 1, asked

    # Another name is unaffected: the backoff is per name, and one typo leaves the rest
    # of the badge resolving.
    with pytest.raises(RuntimeError):
        finder.lookup("Tokyo")

    finder._retry_at.clear()
    with pytest.raises(RuntimeError):
        finder.lookup("Sheffield")
    assert len(asked) == 3, "never tried again"


def test_nothing_typed_is_looked_up(monkeypatch):
    """An empty setting is nowhere, not a request for everywhere."""
    finder, asked = answering(monkeypatch, [SHEFFIELD])
    assert finder.lookup("") is None
    assert finder.lookup(None) is None
    assert finder.lookup("   ") is None
    assert asked == [], asked


class Located(Source):
    name = "located"

    def sample(self, frame, dt):
        pass


def test_a_source_falls_back_to_where_the_badge_is(monkeypatch):
    """A page naming nowhere gets the badge's location, and needs no settings of its own."""
    source = Located({})
    source.geocode, asked = answering(monkeypatch, [SHEFFIELD])
    source.home = {"place": "Sheffield"}

    assert source.location() == (53.38, -1.47, "Sheffield, GB")
    assert source.location({}) == (53.38, -1.47, "Sheffield, GB")
    assert source.location({"place": ""}) == (53.38, -1.47, "Sheffield, GB")
    assert len(asked) == 1, asked


def test_a_page_naming_a_place_overrides_the_badge(monkeypatch):
    """One badge holds a clock for Sheffield and a clock for Tokyo."""
    source = Located({})
    source.geocode, _asked = answering(monkeypatch, [ALABAMA])
    source.home = {"place": "Sheffield"}

    assert source.location({"place": "Sheffield, US"})[:2] == (34.76, -87.7)
    # Coordinates are the more specific answer and win over any name beside them.
    assert source.location({"place": "Sheffield, US", "latitude": 35.7,
                            "longitude": 139.7}) == (35.7, 139.7, None)


def test_a_page_that_cannot_be_located_does_not_borrow_the_badge(monkeypatch):
    """A page asking for a location shows nothing until it resolves, not somewhere else."""
    source = Located({})
    source.geocode, _asked = answering(monkeypatch, RuntimeError("429"))
    source.home = {"place": "Sheffield"}

    with pytest.raises(RuntimeError):
        source.location({"place": "Tokyo"})
    assert source.location({"place": "Tokyo"}) is None


def test_a_source_with_nowhere_set_says_so():
    """None, since a latitude of 0 is the equator and not an unanswered field."""
    source = Located({})
    assert source.location() is None
    assert source.location({"latitude": 0, "longitude": 0}) == (0.0, 0.0, None)


def test_a_home_is_read_off_the_host_config():
    """Only the three location keys, and only where they were answered."""
    assert geocode.home_from({"place": "Sheffield", "lhm_url": "http://x"}) == {
        "place": "Sheffield"}
    assert geocode.home_from({"place": "", "latitude": 0, "longitude": None}) == {
        "latitude": 0}
    assert geocode.home_from(None) == {}
