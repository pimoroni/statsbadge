"""The two map pages, imported and driven.

Run under the WASM port by `node tools/wasm/run.mjs`. The extension's badge modules are
staged into the app's ext/ the way an install puts them there, so these are the modules
the badge would compile.

The map itself is not staged - `worldmap: no map in /system/assets/world.geo.json` on
stderr - so the pages draw without coastlines. What they place on top is the same either
way.
"""

import unittest

import app
import draw
import look
import pages
import worldmap
from pixels import body_pixels, chrome_pixels, differing

APP_DIR = "/system/apps/stats"

ISS_FRAME = {
    "iss": {"lat": 12.0, "lon": -30.0, "altitude": 421.0, "speed": 27600.0,
            "sunlit": 1, "solar_lat": 18.0, "solar_lon": 45.0, "flown": 0.25,
            "aboard": 7,
            "track": [(-40.0, 8.0, 1), (-30.0, 12.0, 1), (-20.0, 16.0, 0)]},
}
QUAKE_FRAME = {
    "quakes": {"events": [
        {"mag": 6.1, "place": "off the coast", "lat": -12.0, "lon": 166.0,
         "depth": 35.0, "age_s": 900},
        {"mag": 3.2, "place": "inland", "lat": 37.0, "lon": -122.0,
         "depth": 8.0, "age_s": 4200}]},
}


def staged(name):
    """One of the extensions' badge modules, or a skip if it was not installed."""
    app.load_extensions(APP_DIR)
    try:
        return __import__(name)
    except ImportError:
        return None


class MapBands(unittest.TestCase):
    """A map is polygons placed by a transform, with a track and a terminator in degrees
    beside them, none of which stops at the edge of the page band."""

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)

    def each_page(self):
        for name, frame in (("quakemap", QUAKE_FRAME), ("issmap", ISS_FRAME)):
            module = staged(name)
            if module is None:
                self.skipTest(f"{name} was not staged")
            yield name, module, frame

    def test_the_map_and_its_band_come_to_the_page_band_exactly(self):
        for name, module, _frame in self.each_page():
            self.assertEqual(module.MAP_H + module.BAND_H, look.BODY_H, name)
            self.assertEqual(module.MAP_TOP, look.BODY_TOP, name)
            self.assertEqual(module.BAND_TOP, look.BODY_TOP + module.MAP_H, name)

    def test_nothing_reaches_outside_the_page_band(self):
        """The header, the footer and the title are one missing clip away."""
        for name, _module, frame in self.each_page():
            draw.background(self.theme, name, 0, 1, None)
            outside = chrome_pixels()
            pages.render({"kind": self.kind_of(name), "id": name, "title": name},
                         frame, {}, self.theme, 0, 1)
            self.assertEqual(chrome_pixels(), outside, f"{name} drew outside its band")

    def test_the_clip_is_put_back(self):
        """The pages draw one after another into the same screen."""
        for name, _module, frame in self.each_page():
            screen.clip = rect(0, 0, screen.width, screen.height)  # noqa: F821
            pages.render({"kind": self.kind_of(name), "id": name, "title": name},
                         frame, {}, self.theme, 0, 1)
            clip = screen.clip  # noqa: F821
            self.assertEqual((clip.x, clip.y, clip.w, clip.h),
                             (0.0, 0.0, float(screen.width), float(screen.height)),  # noqa: F821
                             f"{name} left a clip behind")

    def kind_of(self, module_name):
        for kind in pages.EXTRA:
            if kind.startswith(module_name[:4]):
                return kind
        return module_name


class Pens(unittest.TestCase):
    """One pen per band of the ramp, which is 288 ramp lookups and 288 composites."""

    def test_a_theme_builds_its_pens_once(self):
        theme = look.get(look.DEFAULT)
        self.assertTrue(worldmap.pens(theme) is worldmap.pens(theme), "rebuilt per frame")

    def test_a_re_tinted_theme_gets_pens_of_its_own(self):
        """A derived theme keeps its name when it is re-tinted, so the key is theme.key."""
        one = look.from_palette("tinted", tinted(0))
        other = look.from_palette("tinted", tinted(60))
        self.assertNotEqual(one.key, other.key, "the two themes look the same to a cache")
        self.assertTrue(worldmap.pens(one) is not worldmap.pens(other))

    def test_the_table_does_not_grow_without_bound(self):
        """A badge cycling themes would keep a set of pens for each."""
        for shade in range(0, 240, 30):
            worldmap.pens(look.from_palette("tinted", tinted(shade)))
        self.assertTrue(len(worldmap._pens) <= 4, len(worldmap._pens))


def tinted(shade):
    """A palette like the ones the host sends, with the accent moved."""
    return {"bg": (16, 16, 20), "panel": (28, 28, 34), "ink": (235, 235, 240),
            "dim": (130, 130, 140), "grid": (60, 60, 70),
            "accent": (shade, 200, 120),
            "ramp": [(0.0, (60, 160, 220)), (0.5, (220, 190, 60)),
                     (1.0, (220, 70, 60))]}


class NightWash(unittest.TestCase):
    """The terminator is a curve, and the wash is that curve closed off at a pole."""

    def test_three_copies_reach_across_a_view_that_spans_the_date_line(self):
        """One copy leaves the wash missing at an edge of a wide view."""
        # Looking at the date line, at the scale that puts the whole world across.
        view = worldmap.View(look.BODY_TOP, 100, lon=180.0, scale=look.W / 360.0)
        draw.prepare()
        theme = look.get(look.DEFAULT)
        draw.background(theme, "ISS", 0, 1, None)
        before = body_pixels()
        view.night(theme, 0.0, 23.0)
        after = body_pixels()
        self.assertTrue(differing(before, after) > 0.01, "no wash was drawn at all")

        # The left and right thirds both darken: one copy would leave one of them alone.
        rows = (look.W // 4) * 3
        third = rows // 3
        for edge, start, end in (("left", 0, third), ("right", 2 * third, rows)):
            moved = 0
            for index in range(20):
                base = index * rows
                if before[base + start:base + end] != after[base + start:base + end]:
                    moved += 1
            self.assertTrue(moved > 0, f"the {edge} edge kept the day side")


class Station(unittest.TestCase):
    """The run of predictions carries the position, and `flown` says where now is in it."""

    def setUp(self):
        self.issmap = staged("issmap")
        if self.issmap is None:
            self.skipTest("issmap was not staged")

    def test_halfway_between_two_points_is_halfway_along_the_line(self):
        dense = [(10.0, 40.0, 1), (20.0, 44.0, 1), (30.0, 46.0, 0)]
        lon, lat, lit = self.issmap.flown_at(dense, 0.5 / self.issmap.TRACK_STEPS)
        self.assertTrue(abs(lon - 15.0) < 1e-6 and abs(lat - 42.0) < 1e-6, (lon, lat))
        self.assertEqual(lit, 1)

    def test_a_longitude_off_the_end_of_the_world_comes_back_in_range(self):
        """The spline is given turns, so it can run past 180 and be brought back."""
        self.assertTrue(-180.0 <= self.issmap.flown_at([(190.0, 0.0, 1)], 0.0)[0] <= 180.0)

    def test_no_run_at_all_places_nothing(self):
        self.assertIsNone(self.issmap.flown_at([], 0.0))

    def test_a_prediction_a_little_off_the_last_is_eased_across(self):
        moved = self.issmap.eased((0.0, 40.0, 1), (2.0, 40.0, 1))
        self.assertTrue(0.0 < moved[0] < 2.0, moved)

    def test_a_prediction_far_off_the_last_is_a_different_place(self):
        far = self.issmap.eased((0.0, 40.0, 1), (40.0, 40.0, 1))
        self.assertEqual(far, (40.0, 40.0, 1))


class QuakeMarkers(unittest.TestCase):
    """A ring here cannot be a footprint: a magnitude 5 is strongly felt for about 60km,
    which is half a pixel with the world in view. So it is a marker, sized in pixels."""

    def setUp(self):
        self.quakemap = staged("quakemap")
        if self.quakemap is None:
            self.skipTest("quakemap was not staged")

    def test_a_marker_is_small(self):
        self.assertTrue(self.quakemap.RING_PX_HIGH <= 10.0, self.quakemap.RING_PX_HIGH)
        self.assertTrue(self.quakemap.DOT_PX_HIGH <= 3.0, self.quakemap.DOT_PX_HIGH)

    def test_a_bigger_quake_takes_a_bigger_marker(self):
        low = self.quakemap._dot_px(self.quakemap.MAG_LOW)
        high = self.quakemap._dot_px(self.quakemap.MAG_HIGH)
        self.assertTrue(low < high, (low, high))
        self.assertTrue(self.quakemap.RING_PX_LOW < self.quakemap.RING_PX_HIGH)

    def test_a_ring_clears_the_dot_it_sits_around(self):
        """Or it is a disc."""
        high = self.quakemap._dot_px(self.quakemap.MAG_HIGH)
        self.assertTrue(self.quakemap.RING_PX_LOW > high)
        self.assertTrue(self.quakemap.RING_MIN_PX < self.quakemap.RING_PX_LOW)


if __name__ == "__main__":
    unittest.main()
