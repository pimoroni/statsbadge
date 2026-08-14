"""The map pages: projection, night side, and the two extensions."""

import pathlib
import sys

import pytest

from statsbadge import install


def test_the_world_map_is_parsed_once_for_every_page_that_wants_it():
    """Two extensions draw on the firmware's coastlines. 215KB of JSON is 1256ms and 184KB on
    the badge, so a copy per page would be both twice, and a page that has not been turned to
    should cost neither."""
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    assert worldmap._shapes is None, "the map is parsed at import, not on first use"
    # First ask arms it and says wait, so the frame paying for the parse comes before the one
    # was meant to draw the notice.
    assert worldmap.ready() is False
    assert worldmap._shapes is None, "the parse happened in the frame that asked"

    # The pens are the part a theme change invalidates, and the expensive half of a
    # second page: 288 ramp lookups and 288 composites.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("def pens("):]
    body = body[:body.index("\ndef ", 1)]
    # By `theme.key` and not `theme.name`: a derived theme keeps its name when it is built
    # again from another accent, so a cache under the name outlives the colours in it.
    assert "theme.key" in body and "alpha" in body, "the pens are not keyed by theme"
    assert "_pens.clear()" in body, "the table of pens grows without bound"

    # Every page's band comes out of one View, leaving the projection stated once.
    for extension, module in (("statsbadge-quakes", "quakemap"), ("statsbadge-iss", "issmap")):
        page = (pathlib.Path("extensions") / extension / "src"
                / extension.replace("-", "_") / "badge" / f"{module}.py").read_text(encoding="utf-8")
        assert "worldmap.View(" in page, module
        assert "world.geo.json" not in page, f"{module} reads the map itself"


def test_the_night_side_is_the_one_the_sun_is_not_on():
    """The terminator is a curve and the wash is the polygon closed off at a pole, so the half
    that gets filled depends on which pole is lit. Filling the same side all year - which the
    firmware's iss_tracker does - is right for one solstice and inside out for the other."""
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    # Northern summer: the sun is over the tropic of Cancer, so the north pole is lit all day
    # and the terminator at the sun's longitude is as far south as it goes.
    below = worldmap.terminator_at(23.0, 23.0, 23.0)
    opposite = worldmap.terminator_at(23.0 + 180.0, 23.0, 23.0)
    assert below < 0 and opposite > 0, (below, opposite)
    # Southern summer flips both.
    assert worldmap.terminator_at(0.0, 0.0, -23.0) > 0
    # An equinox has no terminator latitude to give: it saturates at a pole, which is the
    # meridian the curve becomes, and the divisor is held off zero getting there.
    assert abs(worldmap.terminator_at(0.0, 0.0, 0.0)) > 89.0

    # The wash is that curve closed off at a pole, and the pole it closes at is the one in
    # darkness: the other is the one the sun is over. The path is in map degrees, where y is
    # -latitude, so a northern sun closes at y +90.
    assert worldmap.night_path(0.0, 23.0)[0].y == 90.0
    assert worldmap.night_path(0.0, 23.0)[-1].y == 90.0
    assert worldmap.night_path(0.0, -23.0)[0].y == -90.0
    # The curve between them spans the world, so the fill has an edge everywhere.
    path = worldmap.night_path(0.0, 23.0)
    assert path[1].x == -180.0 and path[-2].x == 180.0, (path[1], path[-2])

    # Three copies across, or a view wide enough to see a date line loses the wash at one edge.
    source = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    body = source[source.index("    def night("):]
    body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
    assert "nearest - 360.0, nearest, nearest + 360.0" in body, body[-400:]


def test_a_map_page_stays_inside_its_own_band():
    """A map is 288 polygons placed by a transform, with a track and a terminator drawn in
    degrees beside them.

    None of that stops at the edge of the page's band, so the header, the footer and the
    reading band are one clip away from being drawn over."""
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
        # The band the map draws in plus the band that names it come to the page's band
        # exactly.
        assert scope["MAP_H"] + scope["BAND_H"] == look.BODY_H, (module, scope)
        assert scope["MAP_TOP"] == look.BODY_TOP, module
        assert scope["BAND_TOP"] == look.BODY_TOP + scope["MAP_H"], module
        # Everything the page draws on the map clips to it and puts back what it found, or
        # the next page would inherit the clip.
        for name in drawers:
            body = source[source.index(f"def {name}("):]
            body = body[:body.index("\ndef ", 1)]
            assert "screen.clip = view.box" in body, (module, name)
            assert body.index("was = screen.clip") < body.index("screen.clip = view.box"), name
            assert "screen.clip = was" in body, (module, name)

    # The map itself and the night wash are the app's, and clip themselves for the same reason.
    shared = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    for name in ("    def land(", "    def night("):
        body = shared[shared.index(name):]
        body = body[:body.index("\n    def ", 1) if "\n    def " in body[1:] else len(body)]
        assert "screen.clip = self.box" in body, name
        assert "screen.clip = was" in body, name


def test_the_station_rides_the_track_it_is_already_drawing():
    """The position feed was asked for every five seconds, at 720 requests an hour.

    The station covers 0.065 degrees a second, which is a pixel of a whole-world map every
    seventeen seconds, so four in five of those replies moved the marker nowhere. The run of
    predictions already carries the position, and `flown` says where now sits in it.
    """
    import ast
    import math

    ext = pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss")
    source = (ext / "__init__.py").read_text(encoding="utf-8")
    assert "POSITION_EVERY = 300.0" in source, "the feed is still asked for every few seconds"

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
    # An unwrapped longitude comes back in range, the spline having been given turns.
    assert -180.0 <= env["flown_at"]([(190.0, 0.0, 1)], 0.0)[0] <= 180.0
    assert env["flown_at"]([], 0.0) is None

    # A new prediction a little off the last is eased across, not hopped.
    held = (0.0, 40.0, 1)
    moved = env["eased"](held, (2.0, 40.0, 1))
    assert 0.0 < moved[0] < 2.0, moved
    # Past the ceiling it is a different place, so it goes straight there.
    far = env["eased"](held, (40.0, 40.0, 1))
    assert far == (40.0, 40.0, 1), far

    # The marker takes the eased position and the band prints it, so neither shows a
    # position the other does not.
    assert "_marker(theme, view, at[0], at[1], at[2])" in page
    assert '_band(theme, where, iss.get("aboard"), at)' in page


def test_the_iss_page_agrees_with_its_source():
    """The station is host side and the drawing is badge side. The terminator is the one that
    would fail quietly: the sub-solar point arrives with the position, and a page reading a
    name that moved would draw a map with no night on it and say nothing."""
    source = (pathlib.Path("extensions/statsbadge-iss/src/statsbadge_iss/badge")
              / "issmap.py").read_text(encoding="utf-8")
    ISS = pytest.importorskip("statsbadge_iss").ISS

    kind = ISS.badge_page["kind"]
    assert f'pages.EXTRA["{kind}"] = render' in source, kind
    # Held unanimated, unlike the quake map: with the world in view a frame
    # is 78ms, and the station covers 0.06 pixels of it a second. It holds still between
    # readings, so asking for frames it has no use for is 30% of the CPU for a pulse.
    assert f'pages.ANIMATED.add("{kind}")' not in source, kind
    assert "jump_to" in source, "the camera eases on a page that is only drawn once a reading"
    for setting in ISS.page_settings:
        assert f'get("{setting["key"]}")' in source, setting["key"]
    # Every option the UI offers for the camera is one the page tests for. "whole world" is
    # the default the page falls through to, so it is the one with nothing to match on.
    for option in next(s for s in ISS.page_settings if s["key"] == "follow")["options"]:
        if option == "whole world":
            continue
        assert option in source, option
    # The keys a position carries, all of which the page reads.
    for field in ("lat", "lon", "altitude", "speed", "sunlit", "solar_lat", "solar_lon"):
        assert f'"{field}"' in source, field
    assert '"flown"' in source or 'get("flown")' in source


def test_a_quake_marker_is_a_marker_and_not_a_footprint():
    """The reticle reached twenty degrees, which is a 2200km radius: twice Australia.

    It did that for a magnitude 3 as readily as a 7, and being in degrees it covered the same
    ground however far in the camera was. No ring here can be a footprint - a magnitude 5 is
    strongly felt for about 60km, half a pixel at this scale - so it is a marker, sized in
    pixels and ordered by magnitude.
    """
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

    # Small: a ring of eight pixels at the top of the scale, against twenty-one before.
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
    assert page.count("_dot_px(") >= 3, "the active marker is sized on its own again"


def test_the_quake_page_agrees_with_its_source():
    """The events are host side and the drawing is badge side, so a name that moved on one
    is a page that draws blank and leaves the reader guessing."""
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

    # Every key an event carries is one of them, both ways round: the group name, the
    # list inside it, and the fields of an event.
    event = _event({"properties": {"mag": 4.5, "place": "somewhere", "time": 1700000000000},
                    "geometry": {"coordinates": [1.0, 2.0, 10.0]}})
    assert 'frame.get("quakes") or {}).get("events")' in source
    for field in event:
        if field == "at":
            continue        # worked out into age_s before it is sent
        assert f'"{field}"' in source, field


def test_a_map_page_only_uses_names_the_badge_has():
    """An extension's badge module is compiled on the badge at launch and cannot be imported
    here, so a name that is neither defined, imported nor a badge builtin is a crash dialog
    after the app has started. Same check the app's modules get."""
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
