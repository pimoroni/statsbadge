"""What follows a number on the badge, and where the badge gets it.

Run under the WASM port by `node tools/wasm/run.mjs`. `fmt` rescales some families as it
prints them, so the suffix has to pair with what it printed rather than with what the
host sent: `_mb` prints as 11.1G, which takes a B.
"""

import unittest

import app
import draw
import look
import pages

SENT = {"used_mb": "MB", "uptime_s": "s", "rpm": "rpm", "kwh": "kWh"}


class Units(unittest.TestCase):
    def setUp(self):
        self.was = draw.UNITS
        draw.use_units(SENT)

    def tearDown(self):
        draw.use_units(self.was)

    def test_a_rescaled_family_takes_the_unit_the_figure_landed_in(self):
        shown = {field: draw.fmt(value, field) + draw.short_unit(field)
                 for field, value in (("used_mb", 11400.0), ("uptime_s", 273600),
                                      ("rpm", 2200.0), ("kwh", 0.25))}
        # 0.25 shows as 0.3: MicroPython rounds a half away from zero where CPython
        # rounds it to even. The badge is what these strings have to match.
        self.assertEqual(shown, {"used_mb": "11.1GB", "uptime_s": "3d4h",
                                 "rpm": "2200rpm", "kwh": "0.3kWh"})

    def test_a_field_the_host_said_nothing_about_takes_nothing(self):
        self.assertEqual(draw.short_unit("nonesuch"), "")

    def test_a_new_table_drops_the_readings_baked_under_the_old_one(self):
        draw.reading(0.25, "kwh")
        self.assertTrue(draw._readings, "nothing was baked to drop")
        draw.use_units({"kwh": "kW"})
        self.assertEqual(draw._readings, {}, "a reading kept the unit it was baked with")

    def test_a_layout_hands_them_over(self):
        """The app takes them where it takes the group names."""
        draw.use_units({})
        one = app.App()
        one.layout = {"pages": [], "units": SENT}
        one.apply_layout()
        self.assertEqual(draw.short_unit("kwh"), "kWh", draw.UNITS)


class EveryFigureCarriesAUnit(unittest.TestCase):
    """A kind that prints a figure asks for its unit, wherever it puts it: a slot with
    room takes the two separately, a row that is a name and a figure takes them together.
    """

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)
        self.calls = []
        self.was = (draw.fmt, draw.short_unit, draw.reading)
        real_fmt, real_short, real_reading = self.was
        calls = self.calls

        def fmt(value, field, *rest):
            calls.append("fmt")
            return real_fmt(value, field, *rest)

        def short_unit(field):
            calls.append("short_unit")
            return real_short(field)

        def reading(value, field, *rest):
            # The two together, for a slot with nowhere to put a unit of its own.
            calls.append("reading")
            return real_reading(value, field, *rest)

        draw.fmt, draw.short_unit, draw.reading = fmt, short_unit, reading

    def tearDown(self):
        draw.fmt, draw.short_unit, draw.reading = self.was

    def test_no_kind_prints_a_bare_figure(self):
        from test_pages import FRAME, PAGES

        for index, page in enumerate(PAGES):
            self.calls.clear()
            pages.render(page, FRAME, {}, self.theme, index, len(PAGES))
            if "fmt" not in self.calls:
                continue
            self.assertTrue("short_unit" in self.calls or "reading" in self.calls,
                            f"{page['kind']} printed a figure and asked for no unit")


if __name__ == "__main__":
    unittest.main()
