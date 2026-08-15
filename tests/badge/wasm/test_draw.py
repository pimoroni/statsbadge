"""Measuring and drawing, against the real font."""

import unittest

import draw
import look
from pixels import body_pixels, differing

EXT_DIR = "/system/apps/stats/ext"

CLOCK_FRAME = {
    "v": 1, "seq": 7,
    "clock": {"time": "09:41", "date": "Fri 15 Aug", "epoch": 1786000000},
    "weather": {"temp": 17.5, "code": 3, "wind": 11.0, "units": "celsius"},
    "places": {},
}


class ColumnWidth(unittest.TestCase):
    """A fixed column leaves a gap after short names and clips long ones."""

    def setUp(self):
        draw.prepare()

    def test_nothing_takes_no_room(self):
        self.assertEqual(draw.column_width([], look.SIZE_SMALL), 0)

    def test_a_column_is_as_wide_as_its_widest_line(self):
        widest = draw.text_width("wwwwwwww", look.SIZE_SMALL)
        column = draw.column_width(["i", "wwwwwwww", "il"], look.SIZE_SMALL)
        self.assertEqual(column, widest)

    def test_longer_names_take_a_wider_column(self):
        short = draw.column_width(["cpu", "mem"], look.SIZE_SMALL)
        long = draw.column_width(["cpu", "a much longer field name"], look.SIZE_SMALL)
        self.assertTrue(short > 0, "a column of names measured zero")
        self.assertTrue(long > short, (short, long))

    def test_a_bigger_size_takes_a_wider_column(self):
        small = draw.column_width(["cpu.pct"], look.SIZE_SMALL)
        big = draw.column_width(["cpu.pct"], look.SIZE_BIG)
        self.assertTrue(big > small, (small, big))


class SplitPages(unittest.TestCase):
    """Anything round takes its centre and radius from look.py, so paging between a
    gauge, a ring stack and a clock face moves nothing under the reader."""

    def test_four_rings_fit_the_radius_a_single_gauge_draws_in(self):
        innermost = look.DIAL_OUTER - 4 * draw.RING_BAND - 3 * draw.RING_GAP
        self.assertTrue(innermost >= 8, f"the fourth ring is {innermost} across")

    def test_the_clock_face_takes_the_geometry_it_is_given(self):
        try:
            import clockface
        except ImportError:
            self.skipTest("the clock extension was not staged")
        self.assertEqual(clockface.CENTRE, look.DIAL_C)
        self.assertEqual(clockface.RADIUS, look.DIAL_OUTER)


class Fitting(unittest.TestCase):
    """`fit` shortens one line to a width, for a name off a feed that is whatever
    length it is."""

    def setUp(self):
        draw.prepare()

    def test_a_string_that_fits_is_left_alone(self):
        self.assertEqual(draw.fit("cpu", look.SIZE_SMALL, 300), "cpu")

    def test_a_string_that_does_not_is_cut_to_the_room(self):
        long = "a place name far longer than the column it has to sit in"
        short = draw.fit(long, look.SIZE_SMALL, 60)
        self.assertTrue(len(short) < len(long), short)
        self.assertTrue(draw.text_width(short, look.SIZE_SMALL) <= 60, short)

    def test_a_cut_string_ends_in_an_ellipsis(self):
        short = draw.fit("a place name far longer than its column", look.SIZE_SMALL, 60)
        self.assertTrue(short.endswith("..."), short)

    def test_a_string_that_cannot_be_cut_to_fit_comes_back_whole(self):
        """Nothing of the name is worse than too much of it, and an ellipsis alone is
        not a name."""
        self.assertEqual(draw.fit("cpu", look.SIZE_SMALL, 1), "cpu")


class Flowing(unittest.TestCase):
    """A post is whatever length it is, and the block has room for two or three lines."""

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)

    def band(self, text):
        draw.background(self.theme, "Feed", 0, 1, None)
        draw.flow(text, look.SIZE_SMALL, self.theme.ink,
                  rect(look.PAD, look.BODY_TOP, look.W - 2 * look.PAD, 40))  # noqa: F821
        return body_pixels()

    def test_a_message_too_long_for_its_block_stays_inside_it(self):
        """Whether the firmware truncates or merely clips does not show in the pixels:
        both cut the same prefix at the same place. What is checked is the block."""
        blank = self.band("")
        long = self.band(" ".join(["a post that runs on and on"] * 40))
        self.assertTrue(differing(blank, long) > 0.001, "the long message drew nothing")
        # Sampled every other row, so the 40px block is the first 20 rows of the band.
        below = 20 * (look.W // 4) * 3
        self.assertEqual(long[below:], blank[below:],
                         "the message ran past the bottom of its block")


class Gauges(unittest.TestCase):
    """The sweep is the reading; the colour is where that reading sits on the ramp."""

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)

    def drawn(self, fraction, hot=None):
        draw.background(self.theme, "CPU", 0, 1, None)
        chrome = body_pixels()
        draw.gauge(self.theme, look.DIAL_C, look.DIAL_OUTER, look.DIAL_INNER,
                   fraction, "", hot=hot)
        return differing(chrome, body_pixels())

    def test_a_fuller_reading_sweeps_further(self):
        quiet, busy = self.drawn(0.1), self.drawn(0.9)
        self.assertTrue(quiet > 0.001, f"a gauge at 10% drew {quiet * 100:.2f}%")
        self.assertTrue(busy > quiet, (quiet, busy))

    def test_the_colour_comes_from_severity_and_the_sweep_from_the_reading(self):
        """A battery at 100% is a machine doing well, and draws a full calm ring."""
        asked = []
        real = self.theme.at
        self.theme.at = lambda fraction: asked.append(fraction) or real(fraction)
        try:
            full = self.drawn(1.0, hot=0.0)
        finally:
            self.theme.at = real
        self.assertTrue(0.0 in asked, f"the ramp was read at {asked}, not at the severity")
        self.assertTrue(1.0 not in asked, f"the reading coloured the sweep: {asked}")
        self.assertTrue(full > self.drawn(0.1, hot=0.0), "the severity shortened the sweep")


class ClockFaces(unittest.TestCase):
    """A face in the settings but not in FACES or DIGITAL draws the default, silently."""

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)
        import sys
        if EXT_DIR not in sys.path:
            sys.path.insert(0, EXT_DIR)

    def clockface(self):
        try:
            import clockface
        except ImportError:
            self.skipTest("the clock extension was not staged")
        return clockface

    def test_every_face_draws_a_different_face(self):
        clockface = self.clockface()
        faces = list(clockface.FACES) + list(clockface.DIGITAL)
        self.assertTrue(faces, "no faces are declared")

        drawn = {}
        for face in faces:
            page = {"kind": "clockface", "id": "clock1", "title": face, "face": face}
            draw.background(self.theme, face, 0, 1, None)
            chrome = body_pixels()
            clockface.render(page, CLOCK_FRAME, {}, self.theme)
            band = body_pixels()
            moved = differing(chrome, band)
            self.assertTrue(moved > 0.001,
                            f"the {face} face drew {moved * 100:.2f}% of its band")
            drawn[face] = band

        # And each is a different drawing: two names in the table pointing at one
        # rendering is the same silent default the tables exist to avoid.
        names = list(drawn)
        for first in range(len(names)):
            for second in range(first + 1, len(names)):
                self.assertTrue(
                    drawn[names[first]] != drawn[names[second]],
                    f"{names[first]} and {names[second]} draw the same face")


if __name__ == "__main__":
    unittest.main()
