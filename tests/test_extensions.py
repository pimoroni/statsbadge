"""Discovery, settings, declared groups and the pages they add."""

import json
import pathlib
import sys

from statsbadge import extensions, install, layout


def test_extensions_describe_finds_the_clock(h, ui):

    found = {record["name"]: record for record in extensions.describe()}
    clock = found.get("clock")
    if clock is None:
        return              # the extension is not pip installed in this environment
    assert clock["loaded"], clock
    assert clock["badge_module"] == "clockface.py", clock
    assert "clock" in clock["provides"], clock

    # The UI gets all of them, not only the ones with settings: an extension that asks to be
    # told nothing had no box on the page, and a failed import went unreported.
    _status, caps = h.raw("GET", "/api/capabilities")
    described = {record["name"] for record in caps["extensions"]}
    assert described == set(found), (described, set(found))
    assert "extensions" in ui.ids, "the page has nowhere to list them"
    assert "caps.extensions" in ui.script, "the UI still lists only what has settings"
    assert "extensionBox" in ui.script, "an extension is not a box of its own"


def test_extension_settings_are_declared_stored_and_applied(h):
    """The UI can only offer what an extension declares, and a save has to reach it."""
    status, caps = h.raw("GET", "/api/capabilities")
    assert status == 200, status
    schema = caps.get("extension_settings") or {}
    if "clock" not in schema:
        return              # the clock extension is not pip installed here
    keys = {entry["key"] for entry in schema["clock"]}
    assert {"latitude", "longitude"} <= keys, keys

    _status, config = h.raw("GET", "/api/config")
    config["settings"] = {"clock": {"latitude": "52.4", "longitude": "-1.9",
                                    "units": "fahrenheit"}}
    status, _body = h.raw("PUT", "/api/config", json.dumps(config).encode(),
                          {"Content-Type": "application/json"})
    assert status == 200, status

    # Coerced to the declared type, not stored as the strings a form sends
    _status, stored = h.raw("GET", "/api/config")
    assert stored["settings"]["clock"]["latitude"] == 52.4, stored["settings"]
    assert stored["settings"]["clock"]["units"] == "fahrenheit", stored["settings"]

    # and handed to the running source, not left for the next restart
    clock = next(s for s in h.service.collector.extensions if s.name == "clock")
    assert clock.latitude == 52.4, clock.latitude
    assert clock.units == "fahrenheit", clock.units

    # Host-side only: a location is no business of the badge's
    _status, sent = h.raw("GET", "/api/preview")
    assert "settings" not in sent, sorted(sent)


def test_undeclared_settings_are_dropped_but_absent_extensions_are_kept():
    """A key nobody asked for goes; a whole block for an extension still to load
    stays, or disabling one would be what deletes its configuration."""
    schema = {"clock": [{"key": "latitude", "type": "number"}]}
    incoming = {**layout.DEFAULT_CONFIG, "settings": {
        "clock": {"latitude": "1.5", "sneaky": "no"},
        "notloaded": {"token": "keep me"},
    }}
    stored = layout.validate(incoming, (), schema)["settings"]
    assert stored["clock"] == {"latitude": 1.5}, stored
    assert stored["notloaded"] == {"token": "keep me"}, stored

    # An empty field clears a setting, and comes through as unset
    incoming["settings"]["clock"] = {"latitude": ""}
    cleared = layout.validate(incoming, (), schema)["settings"]
    assert cleared["clock"]["latitude"] is None, cleared


def test_an_extension_page_survives_without_fields():
    """A map page draws from its extension's group and declares no fields, so there is
    nothing in the host's field list to confirm it by.

    Pruned on that list alone it stopped at the host, and the UI said the host reported
    no data for it - while the same page added from the browser, which carries
    `from_extension`, was sent. A page is sent once its extension is installed.
    """
    capabilities = {"available": {"cpu": ["pct"]},
                    "extension_pages": [{"kind": "quakemap", "from_extension": "quakes"}]}
    pages = [{"id": "cpu", "kind": "dial", "field": "cpu.pct"},
             {"id": "quakes", "kind": "quakemap", "fields": []},
             {"id": "uninstalled", "kind": "othermap", "fields": []}]
    kept = [page["id"] for page in layout.prune(pages, capabilities)]
    assert kept == ["cpu", "quakes"], kept


def test_a_declared_group_is_offered_kept_and_recorded(h):
    """What an extension declares has to reach the pickers, the rings and the peaks.

    A group that arrives with a pip install is in none of the model's tables, so without
    this an extension's readings cannot be chosen in the UI and a page drawing one is
    pruned before it reaches the badge.
    """
    from statsbadge.sources.base import Source

    class Site(Source):
        name = "site"
        provides = ("site",)
        groups = {"site": {"label": "Example.com", "fields": {
            "hits": {"label": "Hits a minute", "unit": "/min", "graphed": True,
                     "peak": True, "peak_floor": 10.0},
            "cached_pct": {"label": "Cached %", "unit": "%", "percent": True},
        }}}

        def sample(self, frame, _dt):
            frame["site"] = {"hits": 40.0, "cached_pct": 62.0}

    collector = h.service.collector
    collector.extensions.append(Site({}))
    collector.sample_once()
    caps = collector.capabilities()

    assert caps["available"]["site"] == ["cached_pct", "hits"], caps["available"]
    assert caps["group_labels"]["site"] == "Example.com"
    assert caps["field_labels"]["site"]["hits"] == "Hits a minute"
    assert "cached_pct" in caps["percent_fields"], caps["percent_fields"]
    assert "site.hits" in caps["graphed"], caps["graphed"]

    # A ring, so a graph of it plots something, and a peak, so a gauge has a top end
    assert collector.history(["site.hits"])["site.hits"][-1] == 40.0
    assert collector.latest()["peaks"]["site.hits"] == 40, collector.latest()["peaks"]

    # and the page survives pruning, which reads the same list the pickers do
    page = {"id": "s", "kind": "dial", "field": "site.hits", "readouts": []}
    assert layout.prune([page], caps) == [page]


def test_a_slow_group_travels_only_when_it_changes(h):
    """A reading fetched once a minute should not be sent sixty times.

    Six domains took a frame from 832 bytes to 4.7KB, all of it standing still between the
    host's fetches. The badge says which revision it holds and the host leaves those
    groups out; asking at all marks it as able to read them, so an app too old
    to ask still gets every group inline.
    """
    from statsbadge.sources.base import Source

    class Feed(Source):
        name = "feed"
        groups = {"feed": {"label": "A feed", "slow": True, "fields": {
            "hits": {"label": "Hits", "peak": True, "peak_floor": 1.0}}}}

        def __init__(self, config):
            super().__init__(config)
            self.hits = 10.0

        def sample(self, frame, _dt):
            frame["feed"] = {"hits": self.hits}

    feed = Feed({})
    collector = h.service.collector
    collector.extensions.append(feed)
    collector.sample_once()
    assert "feed" in collector.slow_groups(), collector.slow_groups()

    def stats(query=""):
        status, body = h.signed("GET", f"/v1/stats{query}")
        assert status == 200, (status, body)
        return body

    # An app that does not ask gets it inline, exactly as before any of this
    assert "feed" in stats(), "an app that cannot merge was sent a split frame"

    # Asking, and behind: the group arrives under one key, so the badge keeps what it is
    # handed without having to know which of the frame's groups are the slow ones
    first = stats("?have=-1")
    rev = first["slow_rev"]
    assert "feed" not in first, sorted(first)
    assert first["slow"]["feed"] == {"hits": 10.0}, first["slow"]
    # The peak scales the reading, so it travels with it, on the slow half
    assert first["slow"]["peaks"] == {"feed.hits": 10}, first["slow"]

    # Asking, and up to date, so the group and its peak both stay
    collector.sample_once()
    lean = stats(f"?have={rev}")
    assert "slow" not in lean and "feed" not in lean, sorted(lean)
    assert "feed.hits" not in (lean.get("peaks") or {}), lean.get("peaks")
    assert lean["slow_rev"] == rev, "a reading that did not move revised itself"

    # When the reading moves, the revision does, and the next poll carries it
    feed.hits = 40.0
    collector.sample_once()
    moved = stats(f"?have={rev}")
    assert moved["slow_rev"] == rev + 1, (moved["slow_rev"], rev)
    assert moved["slow"]["feed"] == {"hits": 40.0}, moved["slow"]

    # The badge's side: what it holds goes back into every frame after the one that
    # carried it, and `peaks` merges into the fast ones, keeping both.
    sys.path.insert(0, install.app_source_dir())
    import pages

    held = moved.pop("slow")
    later = stats(f"?have={moved['slow_rev']}")
    fast_peaks = dict(later.get("peaks") or {})
    pages.merge_slow(later, held)
    assert later["feed"] == {"hits": 40.0}, later.get("feed")
    assert later["peaks"]["feed.hits"] == 40, later["peaks"]
    for ref, value in fast_peaks.items():
        assert later["peaks"][ref] == value, f"{ref} was lost to the merge"

    collector.extensions.remove(feed)


def test_a_declared_group_is_named_on_the_badge_too():
    """A badge names a reading after its field, and after its group where one page draws the
    same field from several. That comes out CF_GADGETOID_COM for a group named after a domain:
    the badge has only the key, and the dots cannot be put back. So the host's name for it
    travels with the layout, where a name somebody chose belongs."""
    sys.path.insert(0, install.app_source_dir())
    import pages

    caps = {"available": {"cf_a_com": ["requests"], "cf_b_com": ["requests"],
                          "cpu": ["pct"]},
            "group_source": {"cf_a_com": "Cloudflare", "cf_b_com": "Cloudflare"},
            "group_labels": {"cf_a_com": "a.com", "cf_b_com": "b.com",
                             "cpu": "Processor"}}
    page = {"id": "s", "kind": "spark",
            "fields": ["cf_a_com.requests", "cf_b_com.requests"]}
    labels = layout.group_labels([page], caps)
    assert labels == {"cf_a_com": "a.com", "cf_b_com": "b.com"}, labels

    # The model's groups are left out: "Processor" is read at a desk, the badge says CPU.
    assert layout.group_labels([{"kind": "dial", "field": "cpu.pct"}], caps) == {}

    was = pages.LABELS
    try:
        pages.LABELS = labels
        # Two readings of the same field, told apart by the group as the reader named it
        assert pages.names_for(page["fields"]) == ["a.com", "b.com"]
    finally:
        pages.LABELS = was
    # Absent that, the key in the case the rest of the furniture is in
    assert pages.names_for(page["fields"]) == ["CF_A_COM", "CF_B_COM"]


def test_a_bar_can_be_named_by_whoever_sent_it(h):
    """Bars number their lanes, which suits a core and is no use for a domain.

    A source sends the names beside the values, the way `peaks` are sent beside the readings
    they scale. Nothing has to declare the companion: the picker offers the list field, and
    the names ride along in the frame where only the renderer looks for them.
    """
    from statsbadge.sources.base import Source

    class Domains(Source):
        name = "domains"
        provides = ("edge",)
        groups = {"edge": {"label": "Edge", "fields": {
            "cached": {"label": "Cached % per domain", "percent": True, "list": True}}}}

        def sample(self, frame, _dt):
            frame["edge"] = {"cached": [87.0, 74.5],
                             "cached_names": ["a.com", "b.com"]}

    source = Domains({})
    collector = h.service.collector
    collector.extensions.append(source)
    try:
        collector.sample_once()
        caps = collector.capabilities()
        assert "cached" in caps["list_fields"], caps["list_fields"]
        assert "cached_names" not in caps["list_fields"], caps["list_fields"]
        assert "edge.cached_names" not in caps["available"], "the names were offered as a field"

        sys.path.insert(0, install.app_source_dir())
        import pages

        frame = collector.latest()
        assert pages.value_of(frame, "edge.cached") == [87.0, 74.5]
        assert pages.value_of(frame, "edge.cached" + pages.LANE_NAMES) == ["a.com", "b.com"]
    finally:
        collector.extensions.remove(source)

    source_text = pathlib.Path(install.app_source_dir(), "pages.py").read_text(encoding="utf-8")
    body = source_text[source_text.index("def _bars"):source_text.index("def behind_at")]
    assert "LANE_NAMES" in body, "_bars numbers its lanes whatever the source sent"


def test_stored_settings_beat_the_command_line():
    merged = layout.merge_settings({"clock": {"latitude": 1.0, "units": "celsius"}},
                                   {"clock": {"latitude": 52.4}})
    assert merged["clock"] == {"latitude": 52.4, "units": "celsius"}, merged


def test_an_extension_page_can_be_added_and_reaches_the_badge(h):
    """The UI's kind picker is built from this, and the config it PUTs has to validate.

    Without it the page an extension offers is unreachable. The server carries it, the
    badge can draw it, and nothing offers it to a reader.
    """
    status, caps = h.raw("GET", "/api/capabilities")
    assert status == 200, status
    offered = caps.get("extension_pages") or []
    if not offered:
        return              # no extension is pip installed in this environment
    page = offered[0]
    assert page.get("kind") and page.get("title"), page
    assert page["kind"] not in layout.KINDS, page

    _status, config = h.raw("GET", "/api/config")
    config["pages"].append({**page, "id": page["kind"] + "test"})
    status, saved = h.raw("PUT", "/api/config", json.dumps(config).encode(),
                          {"Content-Type": "application/json"})
    assert status == 200, (status, saved)

    _status, sent = h.raw("GET", "/api/preview")
    kinds = [p["kind"] for p in sent["pages"]]
    assert page["kind"] in kinds, kinds


def test_an_extension_sees_only_its_own_pages():
    """So a source can fetch per page without knowing about the rest of the layout."""

    seen = []

    class Fake:
        name = "fake"
        page_settings = ({"key": "place", "type": "text"},)
        badge_page = {"kind": "faceplate"}

        def pages(self, instances):
            seen.append([page.get("place") for page in instances])

    source = Fake()
    assert extensions.page_settings_schema([source]) == {
        "faceplate": [{"key": "place", "type": "text"}]}
    extensions.configure_pages([source], [
        {"kind": "faceplate", "place": "Tokyo"},
        {"kind": "dial", "field": "cpu.pct"},
        {"kind": "faceplate", "place": "Oslo"},
    ])
    assert seen == [["Tokyo", "Oslo"]], seen

    # A source that raises leaves the others still being told.
    class Angry(Fake):
        name = "angry"

        def pages(self, _instances):
            raise RuntimeError("no")

    extensions.configure_pages([Angry(), source], [{"kind": "faceplate", "place": "Rome"}])
    assert seen[-1] == ["Rome"]


def test_an_extension_installed_since_start_is_taken_up_without_a_restart():
    """entry_points() walks sys.path on every call, so a reload picks one up in place.

    One already running is kept as it stands: building it again would throw away what it
    has fetched and start its clock over. One that has gone is stopped.
    """
    from statsbadge import extensions as ext
    from statsbadge.collect import Collector

    class Fake:
        provides = ("fake",)

        def __init__(self, _config):
            self.started = self.stopped = 0

        @classmethod
        def available(cls):
            return True

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

    class Entry:
        name = "fake"

        def load(self):
            return Fake

    offered = []
    was = ext._entries
    ext._entries = lambda: list(offered)
    try:
        collector = Collector(interval=1.0)      # not started: no sampling thread wanted
        assert collector.extensions == []

        offered.append(Entry())
        assert collector.reload_extensions() == ["fake"]
        arrived = collector.extensions[0]
        assert arrived.started == 1, "a new extension was not started"

        assert collector.reload_extensions() == ["fake"]
        assert collector.extensions[0] is arrived, "a reload rebuilt one already running"
        assert arrived.started == 1, "a reload started one that was already going"

        offered.clear()
        assert collector.reload_extensions() == []
        assert arrived.stopped == 1, "one that has gone was left running"
    finally:
        ext._entries = was


def test_the_app_keeps_what_extensions_reach_into_it_for():
    """A badge module reaches into draw, look, worldmap and pages by attribute.

    Those resolve on the badge alone, so a helper whose callers are all extensions reads
    as unused here, and taking it out is a crash dialog after launch. `draw.readable` is
    one such helper.
    """
    import ast

    app_dir = pathlib.Path(install.app_source_dir())
    defined = {}
    for module in ("draw", "look", "worldmap", "pages"):
        tree = ast.parse((app_dir / f"{module}.py").read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        defined[module] = names

    reached = set()
    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        path = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in defined):
                reached.add((node.value.id, node.attr))
                assert node.attr in defined[node.value.id], (
                    f"{path}: {node.value.id}.{node.attr} is not in the app")
    assert ("draw", "readable") in reached, "the case this check was written for"
