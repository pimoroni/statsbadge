"""What follows a number on the badge, and where the badge gets it."""

import unittest

import app
import draw
import look
import pages

SENT = {"mem.used_mb": "MB", "sys.uptime_s": "s", "fans.rpm": "rpm", "energy.kwh": "kWh",
        "cpu.temp": "°C"}


class Units(unittest.TestCase):
    def setUp(self):
        self.was = draw.UNITS
        draw.use_units(SENT)

    def tearDown(self):
        draw.use_units(self.was)

    def test_a_rescaled_family_takes_the_unit_the_figure_landed_in(self):
        shown = {ref: draw.reading(value, ref)
                 for ref, value in (("mem.used_mb", 11400.0), ("sys.uptime_s", 273600),
                                    ("fans.rpm", 2200.0), ("energy.kwh", 0.25))}
        # 0.25 shows as 0.3: MicroPython rounds a half away from zero where CPython
        # rounds it to even. The badge is what these strings have to match.
        self.assertEqual(shown, {"mem.used_mb": "11.1GB", "sys.uptime_s": "3d4h",
                                 "fans.rpm": "2200rpm", "energy.kwh": "0.3kWh"})

    def test_a_field_named_like_another_takes_its_own_unit(self):
        self.assertEqual(draw.short_unit("cpu.temp"), "°C")
        self.assertEqual(draw.short_unit("energy.temp"), "")

    def test_a_field_the_host_said_nothing_about_takes_nothing(self):
        self.assertEqual(draw.short_unit("energy.nonesuch"), "")

    def test_a_new_table_drops_the_readings_baked_under_the_old_one(self):
        self.assertTrue(draw.reading(0.25, "energy.kwh").endswith("kWh"))
        draw.use_units({"energy.kwh": "kW"})
        self.assertTrue(draw.reading(0.25, "energy.kwh").endswith("kW"),
                        "a reading kept the unit it was baked with")

    def test_a_layout_hands_them_over(self):
        """The app takes them where it takes the group names."""
        draw.use_units({})
        one = app.App()
        one.layout = {"pages": [], "units": SENT, "scales": {"energy.kwh": 2.5},
                      "percent": ["energy.share"]}
        one.apply_layout()
        self.assertEqual(draw.short_unit("energy.kwh"), "kWh", draw.UNITS)
        self.assertEqual(pages.fraction_of("energy.kwh", 1.25), 0.5)
        self.assertEqual(pages.fraction_of("energy.share", 40.0), 0.4)
        pages.use_facts({}, ())


class EveryFigureCarriesAUnit(unittest.TestCase):
    """A kind that prints a figure asks for its unit, wherever it puts it."""

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
