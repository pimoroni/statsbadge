"""The map pages: projection, night side, and the two extensions."""

import pathlib
import sys

import pytest

from statsbadge import install


def test_the_world_map_is_parsed_on_demand_and_shared():
    """One parse serves both map pages, and a page nobody turns to pays for nothing."""
    # 215KB of JSON is 1256ms and 184KB on the badge.
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    assert worldmap._shapes is None, "the map is parsed at import, not on first use"
    # The first call arms the parse and says wait, so the frame that pays for it is not the
    # frame that draws the map.
    assert worldmap.ready() is False
    assert worldmap._shapes is None, "the parse happened in the frame that asked"

    # The pens are what a theme change invalidates: 288 ramp lookups and 288 composites.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("def pens("):]
    body = body[:body.index("\ndef ", 1)]
    # By `theme.key`: a derived theme keeps its name when it is re-tinted.
    assert "theme.key" in body and "alpha" in body, "the pens are not keyed by theme"
    assert "_pens.clear()" in body, "the table of pens grows without bound"

    # Every page's band comes out of one View, leaving the projection stated once.
    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        page = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py").read_text(encoding="utf-8")
        assert "worldmap.View(" in page, module
        assert "world.geo.json" not in page, f"{module} reads the map itself"


def test_the_night_side_is_the_one_the_sun_is_not_on():
    """The night wash closes at whichever pole is dark, so it swaps sides between
    solstices."""
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    # Northern summer: the sun is over the tropic of Cancer, so the terminator at the sun's
    # longitude is as far south as it goes.
    below = worldmap.terminator_at(23.0, 23.0, 23.0)
    opposite = worldmap.terminator_at(23.0 + 180.0, 23.0, 23.0)
    assert below < 0 and opposite > 0, (below, opposite)
    # Southern summer flips both.
    assert worldmap.terminator_at(0.0, 0.0, -23.0) > 0
    # At an equinox the curve is a meridian, so the latitude saturates at a pole and the
    # divisor is held off zero getting there.
    assert abs(worldmap.terminator_at(0.0, 0.0, 0.0)) > 89.0

    # The path is in map degrees, where y is -latitude, so a northern sun closes at y +90.
    assert worldmap.night_path(0.0, 23.0)[0].y == 90.0
    assert worldmap.night_path(0.0, 23.0)[-1].y == 90.0
    assert worldmap.night_path(0.0, -23.0)[0].y == -90.0
    # The curve between them spans the world, so the fill has an edge everywhere.
    path = worldmap.night_path(0.0, 23.0)
    assert path[1].x == -180.0 and path[-2].x == 180.0, (path[1], path[-2])

    # Three copies across, or a view wide enough to see a date line loses the wash at one
    # edge.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("    def night("):]
    body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
    assert "nearest - 360.0, nearest, nearest + 360.0" in body, body[-400:]


def test_a_map_page_stays_inside_its_band():
    """Everything a map page draws is clipped to the band and puts the clip back."""
    # 288 polygons placed by a transform, with a track and a terminator in degrees beside
    # them, none of which stops at the edge of the band.
    sys.path.insert(0, install.app_source_dir())
    import look

    pages_and_bands = (
        ("statsbadge-quakes", "quakemap", ("_others", "_reticle")),
        ("statsbadge-iss", "issmap", ("_marker", "_track")),
    )
    for extension, module, drawers in pages_and_bands:
        source = (pathlib.Path("extensions") / extension / "src"
                  / extension.replace("-", "_") / "badge" / f"{module}.py").read_text(encoding="utf-8")
        scope = {"look": look}
        for line in source.splitlines():
            if line.startswith(("BAND_H", "MAP_TOP", "MAP_H", "BAND_TOP")):
                exec(line, scope)  # noqa: S102  a repo module, four constants off the top
        # The map and the band naming it come to the page's band exactly.
        assert scope["MAP_H"] + scope["BAND_H"] == look.BODY_H, (module, scope)
        assert scope["MAP_TOP"] == look.BODY_TOP, module
        assert scope["BAND_TOP"] == look.BODY_TOP + scope["MAP_H"], module
        # Put back what it found, or the next page inherits the clip.
        for name in drawers:
            body = source[source.index(f"def {name}("):]
            body = body[:body.index("\ndef ", 1)]
            assert "screen.clip = view.box" in body, (module, name)
            assert body.index("was = screen.clip") < body.index("screen.clip = view.box"), name
            assert "screen.clip = was" in body, (module, name)

    # The map and the night wash live in the app, and clip themselves the same way.
    shared = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    for name in ("    def land(", "    def night("):
        body = shared[shared.index(name):]
        body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
        assert "screen.clip = self.box" in body, name
        assert "screen.clip = was" in body, name


def test_the_station_rides_the_track_it_is_already_drawing():
    """The marker is placed along the run of predictions, so the position feed is fetched
    every five minutes."""
    # The station covers 0.065 degrees a second, a pixel of a whole-world map every
    # seventeen seconds, and `flown` says where now sits in the run.
    import ast
    import math

    ext = pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss")
    source = (ext / "__init__.py").read_text(encoding="utf-8")
    assert "POSITION_EVERY = 300.0" in source, "the feed is fetched more often than that"

    # The two that place the marker, run without the firmware behind them.
    page = (ext / "badge" / "issmap.py").read_text(encoding="utf-8")
    tree = ast.parse(page)
    wanted = {"eased", "flown_at"}
    picked = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in wanted]
    consts = [node for node in tree.body
              if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in
              ("CATCH_UP", "CATCH_UP_MAX", "TRACK_STEPS")]
    assert len(picked) == len(wanted), [node.name for node in picked]

    class Wrapping:
        shortest = staticmethod(lambda d: d - 360.0 * math.floor(d / 360.0 + 0.5))

    env = {"worldmap": Wrapping}
    exec(compile(ast.Module(body=consts + picked, type_ignores=[]),  # noqa: S102
                 "issmap", "exec"), env)
    steps = env["TRACK_STEPS"]

    # Halfway between two dense points is halfway along the line between them.
    dense = [(10.0, 40.0, 1), (20.0, 44.0, 1), (30.0, 46.0, 0)]
    lon, lat, lit = env["flown_at"](dense, 0.5 / steps)
    assert abs(lon - 15.0) < 1e-6 and abs(lat - 42.0) < 1e-6, (lon, lat)
    assert lit == 1
    # The spline is given turns, so an unwrapped longitude comes back in range.
    assert -180.0 <= env["flown_at"]([(190.0, 0.0, 1)], 0.0)[0] <= 180.0
    assert env["flown_at"]([], 0.0) is None

    # A new prediction a little off the last is eased across, not hopped.
    held = (0.0, 40.0, 1)
    moved = env["eased"](held, (2.0, 40.0, 1))
    assert 0.0 < moved[0] < 2.0, moved
    # Past the ceiling it is a different place, so it goes straight there.
    far = env["eased"](held, (40.0, 40.0, 1))
    assert far == (40.0, 40.0, 1), far

    # The marker and the band are given the same eased position.
    assert "_marker(theme, view, at[0], at[1], at[2])" in page
    assert '_band(theme, where, iss.get("aboard"), at)' in page


def test_the_iss_page_agrees_with_its_source():
    """Every key and page setting the source sends is one the ISS page reads."""
    source = (pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss/badge")
              / "issmap.py").read_text(encoding="utf-8")
    ISS = pytest.importorskip("statsbadge_iss").ISS

    kind = ISS.badge_page["kind"]
    assert f'pages.EXTRA["{kind}"] = render' in source, kind
    # Unanimated: with the world in view a frame is 78ms, and the station covers 0.06
    # pixels of it a second, so frames between readings are 30% of the CPU for nothing.
    assert f'pages.ANIMATED.add("{kind}")' not in source, kind
    assert "jump_to" in source, "the camera eases on a page drawn once a reading"
    for setting in ISS.page_settings:
        assert f'get("{setting["key"]}")' in source, setting["key"]
    # "whole world" is the default the page falls through to, so it matches on nothing.
    for option in next(s for s in ISS.page_settings if s["key"] == "follow")["options"]:
        if option == "whole world":
            continue
        assert option in source, option
    # The keys a position carries, all of which the page reads.
    for field in ("lat", "lon", "altitude", "speed", "sunlit", "solar_lat", "solar_lon"):
        assert f'"{field}"' in source, field
    assert '"flown"' in source or 'get("flown")' in source


def test_a_quake_marker_is_a_marker_and_not_a_footprint():
    """A quake is marked in pixels, sized and ordered by magnitude, never in degrees."""
    # No ring at this scale can be a footprint: a magnitude 5 is strongly felt for about
    # 60km, which is half a pixel.
    import ast

    page = pathlib.Path("extensions/statsbadge-quakes/src/statsbadge_quakes/badge"
                        "/quakemap.py").read_text(encoding="utf-8")
    assert "RING_SPAN" not in page, "the reticle is still measured in degrees"

    tree = ast.parse(page)
    names = ("MAG_LOW", "MAG_HIGH", "RING_PX_LOW", "RING_PX_HIGH",
             "DOT_PX_LOW", "DOT_PX_HIGH", "RING_MIN_PX")
    consts = [node for node in tree.body if isinstance(node, ast.Assign)
              and getattr(node.targets[0], "id", "") in names]
    fns = [node for node in tree.body if isinstance(node, ast.FunctionDef)
           and node.name in {"_dot_px", "_mag_fraction"}]
    env = {}
    exec(compile(ast.Module(body=consts + fns, type_ignores=[]),  # noqa: S102
                 "quakemap", "exec"), env)
    assert len(consts) == len(names), [node.targets[0].id for node in consts]

    # Small: a ring of eight pixels at the top of the scale.
    assert env["RING_PX_HIGH"] <= 10.0, env["RING_PX_HIGH"]
    assert env["DOT_PX_HIGH"] <= 3.0, env["DOT_PX_HIGH"]
    # Ordered as well, so two events can be ranked by eye.
    assert env["RING_PX_LOW"] < env["RING_PX_HIGH"]
    low, high = env["_dot_px"](env["MAG_LOW"]), env["_dot_px"](env["MAG_HIGH"])
    assert low < high, (low, high)
    # A ring has to clear the dot it sits around, or it is a disc.
    assert env["RING_PX_LOW"] > high, (env["RING_PX_LOW"], high)
    # The smallest ring drawn still reads as one.
    assert env["RING_MIN_PX"] < env["RING_PX_LOW"], env["RING_MIN_PX"]

    # One size for an epicentre, so the selected event and the rest of the feed agree.
    assert page.count("_dot_px(") >= 3, "the active marker is sized separately"


def test_the_quake_page_agrees_with_its_source():
    """Every key and page setting the source sends is one the quake page reads."""
    source = (pathlib.Path("extensions/statsbadge-quakes/src/statsbadge_quakes/badge")
              / "quakemap.py").read_text(encoding="utf-8")
    quakes = pytest.importorskip("statsbadge_quakes")
    Quakes, _event = quakes.Quakes, quakes._event

    kind = Quakes.badge_page["kind"]
    assert f'pages.EXTRA["{kind}"] = render' in source, kind
    # The camera travels and the rings grow between readings, so the page has to ask for
    # frames it has not been polled for.
    assert f'pages.ANIMATED.add("{kind}")' in source, kind

    # Every per-page setting the UI offers is one the renderer reads.
    for setting in Quakes.page_settings:
        assert f'get("{setting["key"]}")' in source, setting["key"]

    # The group name, the list inside it, and every field of an event.
    event = _event({"properties": {"mag": 4.5, "place": "somewhere", "time": 1700000000000},
                    "geometry": {"coordinates": [1.0, 2.0, 10.0]}})
    assert 'frame.get("quakes") or {}).get("events")' in source
    for field in event:
        if field == "at":
            continue        # worked out into age_s before it is sent
        assert f'"{field}"' in source, field


def test_a_map_page_only_uses_names_the_badge_has():
    """Every name an extension's badge module uses is defined, imported, or a badge
    builtin."""
    # The module is compiled on the badge at launch and cannot be imported here, so a
    # missing name is a crash dialog after the app has started.
    import ast

    import check_app

    injected = check_app.badge_globals()
    assert not isinstance(injected, str), injected

    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        path = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fault = check_app.check_names(path, tree, injected)
        assert fault is None, fault
