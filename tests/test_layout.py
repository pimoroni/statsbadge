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


def test_pruning_drops_absent_groups():
    caps = {"available": {"cpu": ["pct"], "sys": ["host"]}}
    pages = layout.prune(layout.DEFAULT_PAGES, caps)
    ids = [p["id"] for p in pages]
    assert "cpu" in ids
    assert "gpu" not in ids, ids
    cpu = next(p for p in pages if p["id"] == "cpu")
    assert cpu["readouts"] == [], cpu


def test_every_field_has_a_name_for_the_ui():
    """The pickers show these, so a field with none shows a column name instead."""
    from statsbadge import model

    described = model.describe()
    for group, fields in model.GROUPS.items():
        assert group in described["group_labels"], group
        for field in fields:
            assert described["field_labels"].get(group, {}).get(field), (group, field)


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

    # Pruned like any multi-field page: what the host cannot answer goes, and the page
    # stays for what is left
    caps = {"available": {"cpu": ["pct"], "mem": ["pct"]}}
    page = {"id": "g", "kind": "dials",
            "fields": ["cpu.pct", "gpu.pct", "mem.pct"]}
    assert layout.prune([page], caps)[0]["fields"] == ["cpu.pct", "mem.pct"]


def test_every_kind_has_a_badge_layout_and_a_ui_shape():
    """A kind the server accepts has to be drawable and configurable, or it is a page
    that validates, reaches the badge and shows a message saying it cannot be drawn."""
    app = pathlib.Path(install.app_source_dir())
    pages_source = (app / "pages.py").read_text(encoding="utf-8")
    ui_source = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
                 / "app.js").read_text(encoding="utf-8")
    markup = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
              / "index.html").read_text(encoding="utf-8")
    for kind in layout.KINDS:
        assert f'"{kind}": _' in pages_source, f"{kind} has no renderer"
        assert f"  {kind}: {{" in ui_source, f"{kind} has no shape in the UI"
        assert f'value="{kind}"' in markup, f"{kind} is not in the kind picker"


def test_a_full_scale_is_offered_where_it_is_read():
    """A page carries where full is, and the UI has to offer that for exactly the kinds whose
    renderer looks at it. Offered too widely it is a control that does nothing; too narrowly
    and a page of counts is stuck being scaled by the busiest reading the host has seen."""
    app = pathlib.Path(install.app_source_dir())
    pages_source = (app / "pages.py").read_text(encoding="utf-8")
    ui_source = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
                 / "app.js").read_text(encoding="utf-8")

    # fraction_of reads the page's max for its caller, so those kinds count too.
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

    offered = ui_source[ui_source.index("const SCALED"):]
    offered = set(re.findall(r'"([a-z]+)"', offered[:offered.index(")")]))
    assert offered == reads, f"UI offers {sorted(offered)}, renderers read {sorted(reads)}"

    def scaled(value):
        stored = layout.validate({**layout.DEFAULT_CONFIG,
                                  "pages": [{"id": "b", "kind": "bars",
                                             "field": "cpu.cores", "max": value}]})
        return stored["pages"][0].get("max")

    assert scaled(1200) == 1200.0
    assert scaled("250") == 250.0
    assert scaled(0) is None and scaled(-5) is None, "a full scale of nothing was stored"
    assert scaled("nonsense") is None and scaled(None) is None


def test_caselights_take_a_field_or_a_flag():
    """Three settings in one value: off, the backlight's level, or a reading to follow."""
    base = dict(layout.DEFAULT_CONFIG)

    def stored(value):
        return layout.validate({**base, "caselights": value})["caselights"]

    # Offered as following the backlight now, though the flag is still stored as it was.
    page = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    assert "Follow the Backlight" in page and "Follow the Theme" not in page

    assert stored("cpu.pct") == "cpu.pct"
    assert stored(True) is True
    assert stored(False) is False
    # Anything other than a "group.field" falls back to a flag, and stops before the
    # badge as a reference it cannot look up.
    assert stored("bogus") is True
    assert stored("too.many.dots") is True
    assert stored(None) is False


def test_a_reading_carries_its_unit():
    """A grid or a sparkline row has one slot, so the unit has to be in the text."""

    sys.path.insert(0, install.app_source_dir())
    import draw

    assert draw.reading(9.2, "pct") == "9.2%"
    assert draw.reading(85.7, "pct") == "85.7%"
    assert draw.reading(71.0, "temp") == "71.0\u00b0C"
    assert draw.reading(None, "pct") == "--"
    assert draw.reading("workshop-pc", "host") == "workshop-pc"

    # A byte figure carries its prefix on the number and its base in the unit, so one
    # unit serves every size the reading grows to.
    assert draw.reading(800, "read_bps") == "800B/s"
    assert draw.reading(819200, "read_bps") == "800KB/s"
    assert draw.reading(52428800, "read_bps") == "50.0MB/s"
    assert draw.reading(3 * 1024 ** 3, "read_bps") == "3.0GB/s"
    assert draw.reading(512, "used_mb") == "512MB"
    assert draw.reading(12600, "used_mb") == "12.3GB"
    assert draw.reading(3 * 1024 ** 2, "total_mb") == "3.0TB"

    # A field can arrive as a list - a load average, per-core loads - and a list has no
    # hash, so it stops before the table keyed on a value. A load average prints as its
    # three figures, bare.
    assert draw.reading([1.52, 1.18, 0.94], "load") == "1.5 1.2 0.9"
    # Sixteen per-core loads overflow a slot, and three of the sixteen misreport it.
    assert draw.reading([31.0] * 16, "cores") == "16 values"
    assert draw.reading([], "load") == "--"

    import pages

    assert pages.fraction_of("cpu.load", [1.5, 1.2, 0.9]) is None, (
        "a list cannot sit on a gauge, and asking must not raise")


def test_a_page_carries_only_what_its_kind_declared():
    """Page-scoped settings, so two clock pages can show two cities.

    An extension page could not carry anything but fields before this: validate dropped
    every other key, so there was nowhere for a per-page place to live.
    """
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

    # Without a schema an extension page keeps its fields alone, as before.
    plain = layout.validate(config, extra_kinds=("clockface",))["pages"][0]
    assert "place" not in plain


def test_the_field_picker_offers_each_reading_once():
    """numericRefs is a subset of availableRefs, so concatenating them listed every
    number twice - once qualified by its group and once again below it."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    # Joining the two lists is fine, so long as the result is deduplicated where it is
    # joined. Checked per line so this cannot pass by matching the fix itself.
    for line in ui.splitlines():
        if "concat(availableRefs())" in line:
            assert "new Set(" in line, f"undeduplicated: {line.strip()}"
    assert "function preferredRefs()" in ui
    # RefSelect deduplicates whatever it is handed, so no caller can bring it back.
    assert "new Set(refs)" in ui


def test_every_kind_picks_from_a_pool_that_suits_it():
    """A gauge offered uptime drew an empty ring, and a grid offered cpu.cores printed a
    list. Each slot now draws from a pool, and every kind has to name one."""
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    shape = ui[ui.index("const SHAPE = {"):ui.index("async function api(")]
    pools = ui[ui.index("const POOLS = {"):]
    pools = pools[:pools.index("}")]
    named = {name for name in ("gauge", "series", "list", "notify", "any")
             if name in pools}

    for kind in layout.KINDS:
        # An entry may be wrapped over two lines, so take it up to its closing brace
        # and the whole of it.
        start = shape.find(f"  {kind}: {{")
        assert start != -1, f"{kind} has no shape"
        entry = shape[start:shape.index("},", start)]
        if 'one: "' not in entry and 'many: "' not in entry:
            # A kind with no slots has an empty pool: the badge page reads the
            # badge, so there is no field to offer.
            assert "max: 0" in entry, f"{kind} has no slots but a field maximum"
            continue
        for slot, key in (("one", "pool"), ("many", "manyPool")):
            if f'{slot}: "' in entry:
                assert f"{key}:" in entry, f"{kind} has a {slot} slot with no {key}"
        for pool in named:
            if f'"{pool}"' in entry:
                break
        else:
            raise AssertionError(f"{kind} names no pool from {sorted(named)}: {entry}")


def test_the_ui_is_told_what_a_gauge_can_scale():
    """It cannot filter uptime out of a gauge without knowing what has a top end."""
    described = model.describe()
    assert "full_scale" in described and described["full_scale"], described.keys()
    assert "temp" in described["full_scale"]
    assert "uptime_s" not in described["full_scale"]
    assert "uptime_s" not in described["percent_fields"]
    # Which fields are a list, so only the kinds that draw lanes are offered them.
    assert set(described["list_fields"]) >= {"cores", "load"}


def test_each_badge_has_its_own_layout(h):
    """Everything on the page is configured per badge: two badges on one host draw different
    pages, and a save for one is not a save for the other. A badge that has not been given a
    layout draws the default, which is also what there is to edit before anything is paired."""
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
        # The table stays behind. It names every other badge paired with this host,
        # which is nothing to do with the one asking.
        assert "badges" not in sent, "a badge is told about every other badge here"

        # The first is still on the default, and its revision has not moved - or every badge
        # would refetch a layout that had not changed.
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

        # The list also carries what each is drawing, so a reader need not open it. Read off
        # the merged layout: a badge on the default reports the default's settings.
        assert listing[other]["theme"] == "mono", listing[other]
        assert listing[other]["interval_ms"] == 2000, listing[other]
        assert listing[h.badge_id]["theme"] == default["theme"], listing[h.badge_id]
        assert listing[h.badge_id]["interval_ms"] == default["interval_ms"]
        assert listing[h.badge_id]["pages"] == len(default["pages"])
        # No secret rides along with any of it.
        assert "secret" not in listing[other], listing[other]
        _status, edited = h.raw("GET", f"/api/config?badge={other}")
        assert edited["theme"] == "mono"

        # A layout cannot be stored against a badge that is not paired here, or a typo in a
        # query string would configure a phantom.
        status, refused = h.raw("PUT", "/api/config?badge=nobody",
                                json.dumps(theirs).encode(),
                                {"Content-Type": "application/json"})
        assert status == 404, (status, refused)

        # An extension doing per-page work is told about every badge's pages: it fetches for
        # all of them at once and keys what it fetched by page id.
        everywhere = {page["id"] for page in h.service.config.all_pages()}
        assert {page["id"] for page in default["pages"]} <= everywhere

        # Forgetting a badge takes its layout with it, or the layout would sit in the file
        # naming an unreachable badge and be handed to whatever next held that id.
        assert h.service.config.configured() == [other]
        h.raw("DELETE", f"/api/badges/{other}")
        assert h.service.config.configured() == []
    finally:
        h.service.badges.forget(other)
        h.service.config.forget(other)

    # A single-badge file is taken as the default, so every badge carries on showing
    # what it showed.
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

    # The picker is in the header, where it says what everything below belongs to.
    web = pathlib.Path("src/statsbadge/web")
    page, script = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    header = page[page.index("<header>"):page.index("</header>")]
    for control in ("<label>Badge", 'id="pair"', 'id="save"'):
        assert control in header, control
    # Naming one and forgetting one belong with the badge itself, not beside the picker.
    assert '"Forget"' in script and "function rename(" in script, "no way to forget or name one"
    assert "?badge=" in script, "the UI saves without saying whose layout it is"
    assert "ownIds" in script, "a badge's pages can collide with another's"


def test_a_badge_s_own_layout_falls_back_to_the_default():
    """A badge's block sits over the default, not instead of it.

    A block saved before a setting existed carries none, and returning it whole handed the
    badge a layout with holes: no tint to build a palette from, and a theme of None that
    drew the boot colours whatever was chosen.
    """
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


def test_a_unit_the_badge_cannot_guess_travels_with_the_layout(h):
    """A field named kwh has no suffix to read a unit off, so a graph of it had none.

    The badge answers for the families `fmt` rescales, the suffix pairing with what it
    printed: `_mb` shows as 11.1G, which takes a B and not the MB it arrived in.
    Everything else takes what the host sent.
    """
    import ast

    from statsbadge.sources.base import Source

    class Meter(Source):
        name = "meter"
        provides = ("energy",)
        groups = {"energy": {"label": "Energy", "fields": {
            "kwh": {"label": "Newest half hour", "unit": "kWh"},
            "spend_p": {"label": "Cost", "unit": "p"}}}}

        def sample(self, frame, _dt):
            frame["energy"] = {"kwh": 0.25, "spend_p": 316.8}

    source = Meter({})
    collector = h.service.collector
    collector.extensions.append(source)
    try:
        collector.sample_once()
        caps = collector.capabilities()
        pages = [{"id": "e", "kind": "graph", "title": "Energy",
                  "fields": ["energy.kwh"]},
                 {"id": "m", "kind": "grid", "title": "Mem",
                  "fields": ["mem.used_mb", "sys.uptime_s", "fans.rpm"]}]
        units = layout.field_units(pages, caps)
        assert units.get("kwh") == "kWh", units
        # The model's travel too: a fan's rpm was a bare number on the page.
        assert units.get("rpm") == "rpm", units
        # A field no page draws is not sent.
        assert "spend_p" not in units, units
    finally:
        collector.extensions.remove(source)

    # The badge keeps the rescaled families to itself, or 11.1G would print as 11.1GMB.
    src = pathlib.Path(install.app_source_dir(), "draw.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"fmt", "_several", "_rate", "_size", "_duration", "short_unit", "use_units"}
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in wanted]
    env = {"SEVERAL": 3, "UNITS": {}, "_readings": {}}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "draw", "exec"), env)  # noqa: S102
    env["use_units"]({"used_mb": "MB", "uptime_s": "s", "rpm": "rpm", "kwh": "kWh"})
    shown = {field: env["fmt"](value, field) + env["short_unit"](field)
             for field, value in (("used_mb", 11400.0), ("uptime_s", 273600),
                                  ("rpm", 2200.0), ("kwh", 0.25))}
    assert shown == {"used_mb": "11.1GB", "uptime_s": "3d4h",
                     "rpm": "2200rpm", "kwh": "0.2kWh"}, shown

    # The app takes them where it takes the group names.
    app = pathlib.Path(install.app_source_dir(), "__init__.py").read_text(encoding="utf-8")
    assert 'draw.use_units(self.setting("units"))' in app


def test_a_figure_carries_its_unit_or_is_handed_one():
    """A battery read 86 on the host page, where every other page says 86%.

    `fmt` prints the number and `short_unit` what follows it. A slot with somewhere to put
    the unit passes them separately - a dial has the line under the needle, a trend the
    space beside the big reading. A row that is only a name and a figure has neither, and
    has to ask for the two together."""
    import ast

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
    # A duration prints its own units and takes none, and a string is not a reading at all.
    assert rows == [("BATTERY", "86.0%"), ("UPTIME", "3d4h"), ("HOST", "workshop-pc")], rows

    # Nowhere else either: a page kind printing a figure has to place the unit somewhere.
    source = pathlib.Path(install.app_source_dir(), "pages.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = {ast.unparse(call.func) for call in ast.walk(node)
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)}
        if "draw.fmt" in calls:
            assert "draw.short_unit" in calls, f"{node.name} prints a figure with no unit"


def test_an_api_key_is_masked_until_it_is_asked_for():
    """The config page sits open on a desk all day, and a token is readable across a room.

    Masked and not hidden: "not set" and "set to the wrong one" have to be told apart,
    and the first few characters are enough for somebody checking to recognise.
    """
    ui = (pathlib.Path(__file__).parent.parent / "src" / "statsbadge" / "web"
          / "app.js").read_text(encoding="utf-8")
    assert "function masked(" in ui and "Edit secrets" in ui
    # A secret does not go in the ordinary run of rows, or it would be on screen anyway
    assert "if (setting.secret) continue" in ui, "a secret is still drawn with the rest"
    # Reopened by name, so a redraw does not close the box under someone's typing
    assert "editingSecrets" in ui

    # Whatever declares one is stored and coerced like any other setting: masking is the
    # UI's business, and the host has to hand the value back or it could not be edited.
    schema = {"thing": [{"key": "api_token", "type": "text", "secret": True}]}
    stored = layout.validate({**layout.DEFAULT_CONFIG,
                              "settings": {"thing": {"api_token": "sekrit"}}},
                             (), schema)["settings"]
    assert stored["thing"] == {"api_token": "sekrit"}, stored


def test_a_number_setting_is_held_to_its_bounds():
    """A reading's units, and how far it can go, are the extension's to declare.

    The browser stops the spinner and marks a field out of range, but a value typed straight
    into one still arrives, so the floor is held to on this side as well. It said "Seconds"
    and why in a note under the field before, which is neither enforceable nor brief.
    """
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
    ui = (pathlib.Path("src/statsbadge/web") / "app.js").read_text(encoding="utf-8")
    assert 'setting.type === "number"' in ui, "a number setting is still a text box"
    assert "setting.unit" in ui, "nowhere to put what it is counted in"
