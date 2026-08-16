"""The map pages, where they cannot be drawn: the parse, and the terminator maths.

The pages themselves are driven in tests/badge/wasm/test_maps.py.
"""

import pathlib
import sys


from statsbadge import install


def test_the_world_map_is_parsed_on_demand_and_shared():
    """One parse serves both map pages, and a page nobody turns to pays for nothing.

    `Pens` in tests/badge/wasm/test_maps.py covers the other half of the cost: the pens
    a theme change invalidates.
    """
    # 215KB of JSON is 1256ms and 184KB on the badge.
    sys.path.insert(0, install.app_source_dir())
    import worldmap

    assert worldmap._shapes is None, "the map is parsed at import, not on first use"
    # The first call arms the parse and returns wait, so the frame that pays for it is not the
    # frame that draws the map.
    assert worldmap.ready() is False
    assert worldmap._shapes is None, "the parse happened in the frame that asked"



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
