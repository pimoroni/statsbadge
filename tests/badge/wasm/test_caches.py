"""What is held between frames, and what a theme change drops.

Run under the WASM port by `node tools/wasm/run.mjs`. Every cache here holds something
painted in a theme's colours, so one left behind after a switch draws a widget in the
palette before last - which reads as a rendering fault and is a missing line.
"""

import unittest

import app
import draw
import look
import worldmap


class Registered(unittest.TestCase):
    def test_every_container_in_draw_is_registered(self):
        """`_cached` returns what it registers, so a cache is declared by being built
        through it. One built by hand is what this finds."""
        # Named as holding no colour: a face and its measurements, and the list itself.
        exempt = {"_fonts", "_weights", "_CLEARS"}
        loose = []
        for name in dir(draw):
            if not name.startswith("_") or name in exempt or name.startswith("__"):
                continue
            held = getattr(draw, name)
            if isinstance(held, (dict, set)) and held.clear not in draw._CLEARS:
                loose.append(name)
        self.assertEqual(loose, [], f"caches in draw.py outside _CLEARS: {loose}")

    def test_clearing_empties_all_of_them(self):
        draw.prepare()
        theme = look.get(look.DEFAULT)
        # Fill what a page fills: pens for the map, and whatever a render leaves behind.
        worldmap.pens(theme)
        self.assertTrue(worldmap._pens, "the map built no pens to drop")

        draw.clear_cache()

        self.assertEqual(worldmap._pens, {}, "the map kept the old theme's pens")
        for name in dir(draw):
            if not name.startswith("_"):
                continue
            held = getattr(draw, name)
            if isinstance(held, (dict, set)) and held.clear in draw._CLEARS:
                self.assertEqual(len(held), 0, f"draw.{name} survived a theme change")

    def test_state_that_is_not_a_container_is_registered_too(self):
        """The waterfall's scroll buffer is a second of columns painted in the ramp they
        were drawn with, and the map's pens are keyed by theme."""
        self.assertTrue(draw.waterfall_reset in draw._CLEARS)
        self.assertTrue(worldmap.forget in draw._CLEARS)


class ATintIsANewTheme(unittest.TestCase):
    """A derived theme keeps its name when it is built from another accent, so anything
    baked under the name would go on being drawn in the colours it was baked in."""

    def setUp(self):
        draw.prepare()
        self.one = look.from_palette("tinted", tinted(0))
        self.other = look.from_palette("tinted", tinted(200))

    def test_two_tints_of_one_theme_are_not_one_theme_to_a_cache(self):
        self.assertEqual(self.one.name, self.other.name)
        self.assertNotEqual(self.one.key, self.other.key)

    def test_a_layout_that_re_tints_drops_what_was_baked_in_the_old_colours(self):
        """The path the badge takes: a layout lands, and apply_layout is what notices."""
        one = app.App()
        one.layout = {"pages": [], "theme": "tinted", "palette": tinted(0)}
        one.apply_layout()
        worldmap.pens(one.theme)
        self.assertTrue(worldmap._pens, "nothing was baked to drop")

        one.layout = {"pages": [], "theme": "tinted", "palette": tinted(200)}
        one.apply_layout()
        self.assertEqual(worldmap._pens, {},
                         "a re-tint left the caches full of the old colours")

    def test_the_same_palette_twice_keeps_them(self):
        """Or every poll throws away everything the last frame built."""
        one = app.App()
        one.layout = {"pages": [], "theme": "tinted", "palette": tinted(0)}
        one.apply_layout()
        worldmap.pens(one.theme)
        one.apply_layout()
        self.assertTrue(worldmap._pens, "an unchanged theme cleared the caches")


def tinted(shade):
    """A palette like the ones the host sends, with the accent moved."""
    return {"bg": (16, 16, 20), "panel": (28, 28, 34), "ink": (235, 235, 240),
            "dim": (130, 130, 140), "grid": (60, 60, 70),
            "accent": (shade, 200, 120),
            "ramp": [(0.0, (60, 160, 220)), (0.5, (220, 190, 60)),
                     (1.0, (220, 70, 60))]}


if __name__ == "__main__":
    unittest.main()
