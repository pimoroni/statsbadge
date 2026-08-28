"""Quick Add: the ready-made pages, and what a host is offered."""

import json

from statsbadge import collect, extensions, layout, model, recipes
from statsbadge.collect import Collector

# Every field the model defines, as a host that reports all of it would.
EVERYTHING = {"available": {group: list(fields) for group, fields in model.GROUPS.items()}}

# Which pool each slot of each kind draws from, mirroring SHAPE in web/app.js. A recipe
# naming a field the picker would not have offered draws a page nobody can read: a gauge
# with no full scale sits empty, a graph with no history ring is a flat line.
POOLS = {
    "dial": ("gauge", "any"),
    "dials": (None, "gauge"),
    "rings": (None, "gauge"),
    "radar": (None, "gauge"),
    "graph": (None, "series"),
    "spark": (None, "series"),
    "trend": ("series", None),
    "bars": ("list", None),
    "waterfall": ("list", None),
    "grid": (None, "any"),
    "text": (None, "any"),
    "notify": (None, "notify"),
    "badge": (None, None),
}

GRAPHED = {f"{group}.{field}" for group, field in collect._GRAPHED}


def _in_pool(ref, pool):
    field = ref.split(".")[1]
    if pool == "gauge":
        return field in model.PERCENT_FIELDS or field in model.FULL_SCALE
    if pool == "series":
        return ref in GRAPHED
    if pool == "list":
        return field in model.LIST_FIELDS
    return field not in model.LIST_FIELDS


def test_every_written_recipe_is_a_layout_the_server_takes():
    """A recipe is added as pages, so a bad kind or a missing field is a 400 on save."""
    for recipe in recipes.offered(EVERYTHING):
        assert recipe["title"], recipe
        assert recipe["pages"], recipe
        kept = layout.validate({**layout.DEFAULT_CONFIG, "pages": recipe["pages"]})
        assert len(kept["pages"]) == len(recipe["pages"]), recipe


def test_every_field_a_recipe_names_is_one_the_host_reports():
    """Not the model's tables and a typo would be silently dropped as unavailable."""
    for recipe in recipes.offered(EVERYTHING):
        for page in recipe["pages"]:
            refs = [page["field"]] if page.get("field") else []
            refs += page.get("readouts") or []
            refs += page.get("fields") or []
            for ref in refs:
                group, field = ref.split(".")
                assert field in model.GROUPS.get(group, ()), (recipe["name"], ref)


def test_every_slot_a_recipe_fills_takes_the_field_it_is_given():
    for recipe in recipes.offered(EVERYTHING):
        for page in recipe["pages"]:
            one, many = POOLS[page["kind"]]
            if page.get("field"):
                assert _in_pool(page["field"], one), (recipe["name"], page["field"], one)
            for ref in page.get("fields") or []:
                assert _in_pool(ref, many), (recipe["name"], ref, many)


def test_a_recipe_this_host_cannot_fill_is_not_offered():
    """A laptop with no discrete GPU should not be offered a GPU dial to add."""
    caps = {"available": {"cpu": ["pct", "temp", "freq", "procs", "cores"],
                          "mem": ["pct"]}}
    offered = {recipe["name"]: recipe for recipe in recipes.offered(caps)}
    assert "gpu" not in offered, sorted(offered)
    assert "cpu" in offered, sorted(offered)

    # The readouts beside a gauge are not the page: what this host has is kept, the rest
    # dropped, and the dial is still worth adding.
    cpu = offered["cpu"]["pages"][0]
    assert cpu["readouts"] == ["cpu.temp", "cpu.freq", "cpu.procs"], cpu

    # A graph of two temperatures where only one is measured is a graph of that one.
    assert offered["thermals"]["pages"][0]["fields"] == ["cpu.temp"], offered["thermals"]


def test_the_default_pages_are_offered_as_one_recipe():
    """So a badge stripped back to one page can be put back as it was."""
    offered = {recipe["name"]: recipe for recipe in recipes.offered(EVERYTHING)}
    assert offered[recipes.DEFAULTS]["pages"] == layout.DEFAULT_PAGES


def test_an_extension_kind_is_only_a_recipe_while_it_is_installed():
    """The pages are the extension's, so nothing can draw them once it is gone."""
    contributed = [{"name": "digital", "title": "Digital Clock",
                    "pages": [{"kind": "clockface", "face": "digital"}]}]

    class Fake:
        name = "clock"
        badge_recipes = tuple(contributed)

    listed = extensions.recipes([Fake()])
    # Namespaced by the source, so two extensions can both ship a "digital".
    assert listed[0]["name"] == "clock.digital", listed
    assert listed[0]["pages"][0]["from_extension"] == "clock", listed

    without = recipes.offered(EVERYTHING, listed)
    assert not [r for r in without if r["name"] == "clock.digital"], without

    installed = {**EVERYTHING, "extension_pages": [{"kind": "clockface"}]}
    offered = [r for r in recipes.offered(installed, listed) if r["name"] == "clock.digital"]
    assert offered, "the recipe is not offered with its extension installed"
    assert offered[0]["pages"][0]["face"] == "digital", offered


def test_a_recipe_the_host_offers_saves_and_reaches_the_badge(h):
    """What Quick Add does: the pages come back from /api/capabilities and are PUT as they
    arrived, bar their ids."""
    # A collector of this test's own, standing in for the harness's, so what capabilities
    # offers and what the preview prunes are read off the same frame. A thread samples the
    # harness's every 0.2s, and a source that drops a reading for one sample would offer a
    # recipe here and prune its page below. Twice, since a rate needs two.
    collector = Collector(interval=1.0)
    collector.sample_once()
    collector.sample_once()
    was, h.service.collector = h.service.collector, collector
    _status, before = h.raw("GET", "/api/config")
    try:
        status, caps = h.raw("GET", "/api/capabilities")
        assert status == 200, status
        offered = caps.get("recipes") or []
        assert offered, "no recipe is offered for a host reporting anything at all"

        added = [{**page, "id": f"{recipe['name']}{index}"}
                 for recipe in offered
                 for index, page in enumerate(recipe["pages"])]
        status, saved = h.raw("PUT", "/api/config",
                              json.dumps({**before, "pages": added[:24]}).encode(),
                              {"Content-Type": "application/json"})
        assert status == 200, (status, saved)

        _status, sent = h.raw("GET", "/api/preview")
        shown = {page["id"] for page in sent["pages"]}
        # Offered because this host reports the fields, so nothing is pruned on the way to
        # the badge.
        for page in added[:24]:
            assert page["id"] in shown, (page, sorted(shown))
    finally:
        h.raw("PUT", "/api/config", json.dumps(before).encode(),
              {"Content-Type": "application/json"})
        h.service.collector = was
