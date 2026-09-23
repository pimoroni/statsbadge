"""Pages, fields, kinds and what a layout may carry."""

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

from conftest import headers as _headers
from statsbadge import install, layout, model, themes
from statsbadge.collect import Collector


def test_pruning_drops_absent_groups():
    caps = {"available": {"cpu": ["pct"], "sys": ["host"]}}
    pages = layout.prune(layout.DEFAULT_PAGES, caps)
    ids = [p["id"] for p in pages]
    assert "cpu" in ids
    assert "gpu" not in ids, ids
    cpu = next(p for p in pages if p["id"] == "cpu")
    assert cpu["readouts"] == [], cpu


def test_an_unsaved_layout_is_pruned_as_it_would_be_sent(h):
    """The UI asks which of the pages being edited would reach the badge, before a save."""
    _status, config = h.raw("GET", "/api/config")
    config["pages"] = config["pages"] + [
        {"id": "unsavedcpu", "kind": "dial", "title": "CPU", "field": "cpu.pct"},
        {"id": "unsavedgone", "kind": "dial", "title": "Gone", "field": "nosuch.pct"},
    ]
    status, shown = h.raw("POST", "/api/preview", json.dumps(config).encode())
    assert status == 200, (status, shown)
    ids = [page["id"] for page in shown["pages"]]
    assert "unsavedcpu" in ids and "unsavedgone" not in ids, ids

    _status, stored = h.raw("GET", "/api/config")
    assert "unsavedcpu" not in [page["id"] for page in stored["pages"]], "the preview saved"
    status, _bad = h.raw("POST", "/api/preview", b"[]")
    assert status == 400, status


def test_every_field_has_a_name_for_the_ui():
    """The pickers show these, so a field with none shows a column name instead."""
    from statsbadge import model

    described = model.describe()
    for group, fields in model.GROUPS.items():
        assert group in described["group_labels"], group
        for field in fields:
            assert described["field_labels"].get(group, {}).get(field), (group, field)


def test_every_page_of_many_fields_is_held_to_its_cap():
    refs = [f"cpu.f{index}" for index in range(10)]
    for kind, shape in layout.KIND_SHAPE.items():
        if not shape.get("many"):
            continue
        page = {"id": "p", "kind": kind, "field": "cpu.pct", shape["many"]: refs}
        kept = layout.validate({**layout.DEFAULT_CONFIG, "pages": [page]})["pages"][0]
        assert len(kept[shape["many"]]) == shape["max"], (kind, kept)


def test_a_dials_page_takes_up_to_four_fields():
    base = dict(layout.DEFAULT_CONFIG)
    refs = ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct", "cpu.temp"]

    def kept(count):
        page = {"id": "g", "kind": "dials", "title": "Load", "fields": refs[:count]}
        return layout.validate({**base, "pages": [page]})["pages"][0]["fields"]

    for count in (1, 2, 3, 4):
        assert len(kept(count)) == count, count
    assert len(kept(5)) == 4, "a fifth gauge has nowhere to go"

    try:
        layout.validate({**base, "pages": [{"id": "g", "kind": "dials", "fields": []}]})
        raise AssertionError("a page with no fields should be refused")
    except ValueError:
        pass

    # Pruned like any multi-field page: what the host cannot report goes, the page stays
    caps = {"available": {"cpu": ["pct"], "mem": ["pct"]}}
    page = {"id": "g", "kind": "dials",
            "fields": ["cpu.pct", "gpu.pct", "mem.pct"]}
    assert layout.prune([page], caps)[0]["fields"] == ["cpu.pct", "mem.pct"]


def test_every_kind_has_a_badge_renderer():
    sys.path.insert(0, install.app_source_dir())
    import pages

    assert set(layout.KINDS) <= set(pages._KINDS), set(layout.KINDS) - set(pages._KINDS)



def test_a_full_scale_is_offered_where_it_is_read():
    """A kind is marked scaled exactly where its renderer reads a full scale."""
    app = pathlib.Path(install.app_source_dir())
    pages_source = (app / "pages.py").read_text(encoding="utf-8")

    # fraction_of reads the page's max for its caller, so those kinds count as reading it.
    reads = set()
    for kind in layout.KINDS:
        start = pages_source.find(f"def _{kind}(")
        if start < 0:
            continue
        end = pages_source.find("\ndef ", start + 1)
        body = pages_source[start:end if end > 0 else len(pages_source)]
        if 'page.get("max")' in body or 'page["max"]' in body:
            reads.add(kind)
        elif "fraction_of(ref, value, page, frame)" in body:
            reads.add(kind)

    offered = {kind for kind, shape in layout.KIND_SHAPE.items() if shape.get("scaled")}
    assert offered == reads, f"marked {sorted(offered)}, renderers read {sorted(reads)}"

    def scaled(value):
        stored = layout.validate({**layout.DEFAULT_CONFIG,
                                  "pages": [{"id": "b", "kind": "bars",
                                             "field": "cpu.cores", "max": value}]})
        return stored["pages"][0].get("max")

    assert scaled(1200) == 1200.0
    assert scaled("250") == 250.0
    assert scaled(0) is None and scaled(-5) is None, "a full scale of nothing was stored"
    assert scaled("nonsense") is None and scaled(None) is None


def test_caselights_take_a_field_or_a_flag(ui):
    """Three settings in one value: off, the backlight's level, or a reading to follow."""
    base = dict(layout.DEFAULT_CONFIG)

    def stored(value):
        return layout.validate({**base, "caselights": value})["caselights"]

    # The UI offers it as following the backlight; the stored value is still a flag.
    page = ui.script
    assert "Follow the Backlight" in page and "Follow the Theme" not in page

    assert stored("cpu.pct") == "cpu.pct"
    assert stored(True) is True
    assert stored(False) is False
    # Anything other than a "group.field" falls back to a flag, so the badge never sees a
    # reference it cannot look up.
    assert stored("bogus") is True
    assert stored("too.many.dots") is True
    assert stored(None) is False


def test_a_reading_prints_as_one_string_with_its_unit():
    """A grid or a sparkline row has one slot, so the unit has to be in the text."""

    sys.path.insert(0, install.app_source_dir())
    import draw

    refs = ["cpu.pct", "cpu.temp", "sys.host", "disk.read_bps", "mem.used_mb",
            "mem.total_mb", "cpu.load", "cpu.cores"]
    draw.use_units(layout.field_facts([{"fields": refs}], {})["units"])
    try:
        assert draw.reading(9.2, "cpu.pct") == "9.2%"
        assert draw.reading(85.7, "cpu.pct") == "85.7%"
        assert draw.reading(71.0, "cpu.temp") == "71.0\u00b0C"
        assert draw.reading(None, "cpu.pct") == "--"
        assert draw.reading("workshop-pc", "sys.host") == "workshop-pc"

        # A byte figure carries its prefix on the number and its base in the unit.
        assert draw.reading(800, "disk.read_bps") == "800B/s"
        assert draw.reading(819200, "disk.read_bps") == "800KB/s"
        assert draw.reading(52428800, "disk.read_bps") == "50.0MB/s"
        assert draw.reading(3 * 1024 ** 3, "disk.read_bps") == "3.0GB/s"
        assert draw.reading(512, "mem.used_mb") == "512MB"
        assert draw.reading(12600, "mem.used_mb") == "12.3GB"
        assert draw.reading(3 * 1024 ** 2, "mem.total_mb") == "3.0TB"

        # A list has no hash and cannot reach a table keyed on value.
        assert draw.reading([1.52, 1.18, 0.94], "cpu.load") == "1.5 1.2 0.9"
        # Sixteen per-core loads overflow a slot, and three of the sixteen misreport it.
        assert draw.reading([31.0] * 16, "cpu.cores") == "16 values"
        assert draw.reading([], "cpu.load") == "--"
    finally:
        draw.use_units({})

    import pages

    assert pages.fraction_of("cpu.load", [1.5, 1.2, 0.9]) is None, (
        "a list cannot sit on a gauge, and asking must not raise")


def test_a_page_carries_only_what_its_kind_declared():
    """A page keeps the settings its kind declared, at the declared type, and drops the rest."""
    schema = {"clockface": [{"key": "place", "label": "Place", "type": "text"},
                            {"key": "big", "label": "Big", "type": "bool"}]}
    config = {"pages": [{"id": "a", "kind": "clockface", "title": "Tokyo",
                         "fields": [], "place": "Tokyo", "big": "yes",
                         "smuggled": "nope"}]}
    page = layout.validate(config, extra_kinds=("clockface",),
                           page_settings_schema=schema)["pages"][0]
    assert page["place"] == "Tokyo"
    assert page["big"] is True, "declared type not applied"
    assert "smuggled" not in page, "an undeclared key reached the badge"

    # Without a schema an extension page keeps its fields alone.
    plain = layout.validate(config, extra_kinds=("clockface",))["pages"][0]
    assert "place" not in plain


def test_every_kind_picks_from_a_pool_that_suits_it(ui):
    """Every slot names a pool the UI has, and a kind with no slot takes no fields."""
    pools = ui.script[ui.script.index("const POOLS = {"):]
    named = set(re.findall(r"^  (\w+):", pools[:pools.index("\n}")], re.M))
    for kind, shape in layout.KIND_SHAPE.items():
        if not shape.get("one") and not shape.get("many"):
            assert "max" not in shape, kind
            continue
        for slot, pool in (("one", "pool"), ("many", "many_pool")):
            if shape.get(slot):
                assert shape.get(pool) in named, (kind, slot, shape.get(pool), named)



def test_the_ui_is_told_what_a_gauge_can_scale():
    """The described model marks which fields have a top end, to keep uptime off a gauge."""
    described = model.describe()
    assert "full_scale" in described and described["full_scale"], described.keys()
    assert "temp" in described["full_scale"]
    assert "uptime_s" not in described["full_scale"]
    assert "uptime_s" not in described["percent_fields"]
    # Which fields are a list, so only the kinds that draw lanes are offered them.
    assert set(described["list_fields"]) >= {"cores", "load"}


def test_a_layout_is_stored_per_badge(h, ui):
    """A save for one badge is not a save for another, and one without draws the default."""
    other = "badgetwo00000002"
    other_secret = h.service.badges.provision(other, "second badge")
    try:
        _status, default = h.raw("GET", "/api/config")
        assert "badges" not in default, "the UI is handed every badge's layout at once"

        # The second badge, and only it, is given a layout.
        theirs = dict(default, theme="mono", interval_ms=2000)
        status, saved = h.raw("PUT", f"/api/config?badge={other}",
                              json.dumps(theirs).encode(),
                              {"Content-Type": "application/json"})
        assert status == 200, (status, saved)
        assert saved["badge"] == other and saved["rev"] > default["rev"]

        status, sent = h.raw("GET", "/v1/layout", None,
                             _headers(other, 1, other_secret, path="/v1/layout"))
        assert status == 200, (status, sent)
        assert sent["theme"] == "mono" and sent["interval_ms"] == 2000
        # The table stays behind: it names every other badge paired with this host.
        assert "badges" not in sent, "a badge is told about every other badge here"

        # The first is still on the default, and its revision has not moved.
        _status, mine = h.signed("GET", "/v1/layout")
        assert mine["theme"] == default["theme"], mine["theme"]
        assert mine["rev"] == default["rev"], "a save for one badge moved another's revision"

        # Each watches its layout's revision for a change.
        _status, frame = h.signed("GET", "/v1/stats")
        assert frame["layout_rev"] == default["rev"]
        _status, their_frame = h.raw("GET", "/v1/stats", None,
                                     _headers(other, 2, other_secret))
        assert their_frame["layout_rev"] == saved["rev"]

        # The UI edits one badge at a time, and is told which of them have a layout.
        _status, listing = h.raw("GET", "/api/badges")
        assert listing[other]["configured"] is True
        assert listing[h.badge_id]["configured"] is False

        # The list carries what each is drawing, read off the merged layout.
        assert listing[other]["theme"] == "mono", listing[other]
        assert listing[other]["interval_ms"] == 2000, listing[other]
        assert listing[h.badge_id]["theme"] == default["theme"], listing[h.badge_id]
        assert listing[h.badge_id]["interval_ms"] == default["interval_ms"]
        assert listing[h.badge_id]["pages"] == len(default["pages"])
        # No secret rides along with any of it.
        assert "secret" not in listing[other], listing[other]
        _status, edited = h.raw("GET", f"/api/config?badge={other}")
        assert edited["theme"] == "mono"

        # A layout cannot be stored against a badge that is not paired here.
        status, refused = h.raw("PUT", "/api/config?badge=nobody",
                                json.dumps(theirs).encode(),
                                {"Content-Type": "application/json"})
        assert status == 404, (status, refused)

        # An extension doing per-page work fetches for every badge at once.
        everywhere = {page["id"] for page in h.service.config.all_pages()}
        assert {page["id"] for page in default["pages"]} <= everywhere

        # Forgetting a badge takes its layout with it, or the next to hold that id gets it.
        assert h.service.config.configured() == [other]
        h.raw("DELETE", f"/api/badges/{other}")
        assert h.service.config.configured() == []
    finally:
        h.service.badges.forget(other)
        h.service.config.forget(other)

    # A file with no badge blocks is taken as the default for all of them.
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-layout-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 7, "theme": "mono", "pages": layout.DEFAULT_PAGES}, handle)
    old = layout.Config(path)
    assert old.configured() == []
    assert old.layout_for()["theme"] == "mono"
    assert old.layout_for("anybadge")["theme"] == "mono", "an old file lost its layout"
    assert old.rev_for("anybadge") == 7
    # A revision is always fresh, whichever layout it was last spent on.
    assert old.replace({"pages": layout.DEFAULT_PAGES}, badge_id="anybadge") == 8
    assert old.replace({"pages": layout.DEFAULT_PAGES}) == 9
    assert old.rev_for("anybadge") == 8, "the default's save moved a badge's revision"
    assert old.layout_for("anybadge")["pages"], "a badge's layout was lost"
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # The picker is in the header, above everything it applies to.
    page, script = ui.markup, ui.script
    header = page[page.index("<header>"):page.index("</header>")]
    for control in ("<label>Badge", 'id="pair"', 'id="save"'):
        assert control in header, control
    # Naming and forgetting sit with the badge itself, not beside the picker.
    assert '"Forget"' in script and "function rename(" in script, "no way to forget or name one"
    assert "?badge=" in script, "the UI saves without saying whose layout it is"
    assert "ownIds" in script, "a badge's pages can collide with another's"


def test_a_badge_block_sits_over_the_default():
    """A badge block overrides what it names and inherits everything it does not."""
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-blocks-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 4, "theme": "sakura", "brightness": 0.8,
                   "badges": {"partial": {"brightness": 0.5},
                              "whole": {"theme": "mono", "tint": layout.DEFAULT_CONFIG["tint"]}}},
                  handle)
    config = layout.Config(path)

    # A block that named no theme keeps none.
    assert "theme" not in config.data["badges"]["partial"]

    partial = config.for_badge(None, "partial")
    assert partial["theme"] == "sakura", "a partial block lost the theme it inherits"
    assert partial["brightness"] == 0.5, "a partial block lost what it does say"
    assert partial["palette"]["bg"] == themes.written()["sakura"]["bg"]

    whole = config.for_badge(None, "whole")
    assert whole["theme"] == "mono"
    assert whole["brightness"] == 0.8, "a block should inherit what it does not name"


def test_a_unit_the_badge_cannot_guess_travels_with_the_layout():
    """A field with no suffix to read a unit off is sent its unit with the layout."""
    from statsbadge.sources.base import Source

    class Meter(Source):
        name = "meter"
        provides = ("energy",)
        groups = {"energy": {"label": "Energy", "fields": {
            "kwh": {"label": "Newest half hour", "unit": "kWh", "full_scale": 2.5},
            "spend_p": {"label": "Cost", "unit": "p"},
            "temp": {"label": "Tank", "unit": "K", "full_scale": 400},
            "share": {"label": "Share", "percent": True}}}}

        def sample(self, frame, _dt):
            frame["energy"] = {"kwh": 0.25, "spend_p": 316.8, "temp": 330.0, "share": 40}

    source = Meter({})
    # A collector of this test's own, so the frame it samples is the frame it reads.
    collector = Collector(interval=1.0)
    collector.extensions.append(source)
    collector.sample_once()
    caps = collector.capabilities()
    pages = [{"id": "e", "kind": "grid", "title": "Energy",
              "fields": ["energy.kwh", "energy.temp", "energy.share"]},
             {"id": "m", "kind": "grid", "title": "Mem",
              "fields": ["mem.used_mb", "sys.uptime_s", "fans.rpm", "cpu.temp", "cpu.pct"]}]
    facts = layout.field_facts(pages, caps)
    units, scales = facts["units"], facts["scales"]
    assert units["energy.kwh"] == "kWh" and scales["energy.kwh"] == 2.5, facts
    # The model's fields travel the same way.
    assert units["fans.rpm"] == "rpm", units
    # A field no page draws is not sent.
    assert "energy.spend_p" not in units, units
    # An extension's `temp` is its own, and leaves the processor's alone.
    assert (units["energy.temp"], scales["energy.temp"]) == ("K", 400.0), facts
    assert (units["cpu.temp"], scales["cpu.temp"]) == ("°C", 100.0), facts
    assert set(facts["percent"]) == {"energy.share", "cpu.pct"}, facts["percent"]

    # The case lights can follow a reading no page draws.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({**layout.DEFAULT_CONFIG, "caselights": "mem.pct",
                    "pages": [{"id": "t", "kind": "text", "fields": ["sys.host"]}]})
    sent = config.for_badge({"available": {"sys": ["host"], "mem": ["pct"]}})
    assert sent["percent"] == ["mem.pct"], sent["percent"]


def test_a_row_of_a_name_and_a_figure_takes_the_unit_in_the_figure():
    """A grid row has one slot, so `reading` returns the two together."""
    sys.path.insert(0, install.app_source_dir())
    import draw
    import pages

    rows = []
    was = draw.lines
    draw.lines = lambda _theme, entries: rows.extend(entries)
    try:
        pages._text({"fields": ["power.battery_pct", "sys.uptime_s", "sys.host"]},  # noqa: SLF001
                    {"power": {"battery_pct": 86.0},
                     "sys": {"uptime_s": 273600, "host": "workshop-pc"}}, None, None)
    finally:
        draw.lines = was
    # A duration prints its units in the figure and takes none; a string is not a reading.
    assert rows == [("BATTERY", "86.0%"), ("UPTIME", "3d4h"), ("HOST", "workshop-pc")], rows


def test_a_number_setting_is_held_to_its_bounds(ui):
    """A number setting is clamped to the bounds its extension declared, on this side too."""
    schema = {"thing": [{"key": "every", "type": "number", "min": 60, "max": 3600,
                         "unit": "seconds"},
                        {"key": "loose", "type": "number"}]}

    def stored(settings):
        return layout.validate({**layout.DEFAULT_CONFIG, "settings": {"thing": settings}},
                               (), schema)["settings"]["thing"]

    assert stored({"every": 5, "loose": 5}) == {"every": 60.0, "loose": 5.0}
    assert stored({"every": 9999, "loose": 9999}) == {"every": 3600.0, "loose": 9999.0}
    assert stored({"every": 120, "loose": None}) == {"every": 120.0, "loose": None}

    # The UI draws one as a number, with the bounds on the field.
    ui = ui.script
    assert 'setting.type === "number"' in ui, "a number setting is still a text box"
    assert "setting.unit" in ui, "nowhere to put what it is counted in"


def test_every_display_setting_lands_on_a_known_value():
    """A flag, a choice and a bounded number each come back usable however they arrive."""
    def stored(**sent):
        return layout.validate({**layout.DEFAULT_CONFIG, **sent})

    absent = stored()
    assert {key: absent[key] for key in
            ("smooth", "animate", "plot_animation", "auto_brightness")} == {
        "smooth": True, "animate": False,
        "plot_animation": False, "auto_brightness": False}
    assert {key: absent[key] for key in ("slide", "rows", "gauge_fill", "accent_b")} == {
        "slide": "off", "rows": "zebra", "gauge_fill": "solid", "accent_b": "same"}
    assert {key: absent[key] for key in
            ("interval_ms", "graph_points", "idle_advance_s", "advance_every_s")} == {
        "interval_ms": 1000, "graph_points": 48,
        "idle_advance_s": 0, "advance_every_s": 10}

    # A flag takes anything, since the UI is not the only caller.
    assert stored(smooth=0)["smooth"] is False
    assert stored(animate="x")["animate"] is True

    # A choice the badge has no renderer for falls back rather than reaching it.
    for key, fallback in (("slide", "off"), ("rows", "zebra"),
                          ("gauge_fill", "solid"), ("accent_b", "same")):
        assert stored(**{key: "nonsense"})[key] == fallback, key

    # `slide` was a bool before it was a choice, and a config saved then still loads.
    assert stored(slide=True)["slide"] == "over"
    assert stored(slide=False)["slide"] == "off"

    # Caselights take a field reference to follow, or a plain on and off.
    assert stored(caselights="cpu.pct")["caselights"] == "cpu.pct"
    assert stored(caselights=1)["caselights"] is True

    # Numbers are clamped and never refused, so a hand-edited file still loads.
    for key, low, high in (("interval_ms", 250, 60000),
                           ("brightness", 0.05, 1.0),
                           ("graph_points", 8, 160),
                           ("idle_advance_s", 0, 3600),
                           ("advance_every_s", 1, 600)):
        assert stored(**{key: -10**6})[key] == low, key
        assert stored(**{key: 10**6})[key] == high, key


def test_a_setting_that_is_not_a_number_is_refused():
    """Clamping is for a number out of range; one that is not a number is a bad request."""
    for bad in ("abc", None, [1]):
        try:
            layout.validate({**layout.DEFAULT_CONFIG, "interval_ms": bad})
        except (TypeError, ValueError):
            continue
        raise AssertionError(f"accepted interval_ms={bad!r}")


def test_every_control_is_bound_to_a_setting_the_server_takes(ui):
    """Every binding in the script names a control in the page and a setting `validate` keeps."""
    # Three files that have to agree and none imports another.
    assert ui.bindings, "no bindings were read out of app.js"
    for control, setting in ui.bindings.items():
        assert control in ui.ids, f"{control} is bound but not in the page"
        assert ui.ids[control] in ("input", "select"), (control, ui.ids[control])
        assert setting in layout.DEFAULT_CONFIG, f"{control} is bound to {setting}, not a setting"

    # A default round-trips through validate unchanged.
    kept = layout.validate({**layout.DEFAULT_CONFIG, "pages": layout.DEFAULT_PAGES})
    for control, setting in ui.bindings.items():
        assert setting in kept, f"validate drops {setting}, which {control} sets"


def test_every_slider_stays_inside_what_the_server_takes(ui):
    import html.parser

    class Ranges(html.parser.HTMLParser):
        found = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("type") == "range":
                self.found[attrs["id"]] = (float(attrs["min"]), float(attrs["max"]))

    parser = Ranges()
    parser.feed(ui.markup)
    bounds = {"interval_ms": (layout.INTERVAL_MS, 1), "graph_points": (layout.GRAPH_POINTS, 1),
              "idle_advance_s": (layout.IDLE_ADVANCE_S, 1),
              "advance_every_s": (layout.ADVANCE_EVERY_S, 1),
              "brightness": (layout.BRIGHTNESS, 100)}
    assert set(parser.found) == {c for c, s in ui.bindings.items() if s in bounds}
    for control, (low, high) in parser.found.items():
        (floor, ceiling), scale = bounds[ui.bindings[control]]
        assert floor * scale <= low and high <= ceiling * scale, (control, low, high)
