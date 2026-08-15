"""The app itself, built and driven.

Run under the WASM port by `node tools/wasm/run.mjs`. app.py reaches the firmware at
import, and `socket`, `wifi` and `secrets` come from tools/wasm/shims: nothing here touches a
network, and anything that tried would raise.

A layout is assigned rather than fetched. That is what `apply_layout` does with the
host's reply, and `setting()` reads it, so an App with one is an App that has polled.
"""

import unittest

import app
import draw
import look
import pages
from pixels import body_pixels, differing

PAGES = [{"id": "one", "kind": "text", "title": "One", "fields": ["sys.host"]},
         {"id": "two", "kind": "text", "title": "Two", "fields": ["sys.host"]},
         {"id": "three", "kind": "text", "title": "Three", "fields": ["sys.host"]}]


def built(pages=3, **settings):
    """An App holding a layout of `pages` pages, and whatever settings are named."""
    one = app.App()
    layout = {"pages": PAGES[:pages]}
    layout.update(settings)
    one.layout = layout
    return one


def paired(**settings):
    """An App the poll loop will act on: credentials, but nothing at the other end."""
    one = built(**settings)
    one.config.badge_id = "e661badge0000001"
    one.config.hosts = {"host1": {"host": "10.0.0.5", "port": 8420, "secret": "s", "seq": 0}}
    one.config.active = "host1"
    return one


class Entry(unittest.TestCase):
    def test_importing_the_app_does_not_start_it(self):
        """__init__.py calls main(), so importing app.py starts nothing."""
        self.assertTrue(callable(app.main), "no main() to call")
        self.assertIsNone(app._app, "main() ran at import")


class Paging(unittest.TestCase):
    def test_a_badge_with_no_layout_has_no_page_to_turn_to(self):
        one = app.App()
        self.assertIsNone(one.current_page())
        one.turn(1)
        self.assertEqual(one.page_index, 0)

    def test_turning_wraps_at_both_ends(self):
        one = built()
        for expected in (1, 2, 0):
            one.turn(1)
            self.assertEqual(one.page_index, expected)
        one.turn(-1)
        self.assertEqual(one.page_index, 2, "turning back off the front does not wrap")

    def test_the_page_index_follows_the_page_drawn(self):
        one = built()
        one.turn(1)
        self.assertEqual(one.current_page()["id"], "two")

    def test_an_index_past_a_shorter_layout_comes_back_to_the_front(self):
        """A layout can arrive with fewer pages than the one it replaces."""
        one = built()
        one.turn(2)
        one.layout = {"pages": PAGES[:1]}
        self.assertEqual(one.current_page()["id"], "one")
        self.assertEqual(one.page_index, 0)


class IdleAdvance(unittest.TestCase):
    """`advance_if_idle` takes the time as an argument, so no clock is faked here."""

    def test_a_badge_left_alone_does_not_page_unless_asked_to(self):
        one = built()
        one.advance_if_idle(one._pressed_at + 600_000)
        self.assertEqual(one.page_index, 0)

    def test_one_page_does_not_turn_to_itself(self):
        one = built(pages=1, idle_advance_s=5)
        one.advance_if_idle(one._pressed_at + 6000)
        self.assertEqual(one._advanced_at, 0, "a single page was turned")

    def test_a_badge_left_alone_pages_on(self):
        one = built(idle_advance_s=5, advance_every_s=10)
        one.advance_if_idle(one._pressed_at + 4999)
        self.assertEqual(one.page_index, 0, "it turned before it was idle")
        one.advance_if_idle(one._pressed_at + 5000)
        self.assertEqual(one.page_index, 1)

    def test_the_turns_it_makes_leave_the_idle_timer_alone(self):
        """Or the first turn would put the badge back to sleep."""
        one = built(idle_advance_s=5, advance_every_s=10)
        was = one._pressed_at
        one.advance_if_idle(was + 5000)
        self.assertEqual(one._pressed_at, was, "the badge counts itself as touched")
        one.advance_if_idle(was + 9000)
        self.assertEqual(one.page_index, 1, "it turned again inside advance_every_s")
        one.advance_if_idle(was + 15000)
        self.assertEqual(one.page_index, 2)


class LocalActions(unittest.TestCase):
    """The three the host offers, which the badge answers itself."""

    def test_a_page_can_be_turned_with_no_host_at_all(self):
        one = built(buttons={"a": "badge.next", "b": "badge.prev"})
        one.press("a")
        self.assertEqual(one.page_index, 1)
        one.press("b")
        self.assertEqual(one.page_index, 0)

    def test_the_brightness_button_steps_and_comes_back_round(self):
        one = built(buttons={"c": "badge.brightness"}, brightness=0.8)
        levels = []
        for _ in range(len(app.BRIGHTNESS_STEPS)):
            one.press("c")
            levels.append(one.dimmed)
        self.assertIsNone(levels[-1], "the cycle does not come back to the top")
        self.assertTrue(all(level is not None for level in levels[:-1]), levels)
        self.assertTrue(levels[0] > levels[1], levels)

    def test_a_binding_that_is_not_local_is_held_for_the_host(self):
        one = built(buttons={"a": "media_next"})
        one.press("a")
        self.assertEqual([command for command, _at in one._commands], ["media_next"])
        self.assertEqual(one.page_index, 0, "a host command turned a page")

    def test_a_button_with_no_binding_does_nothing(self):
        one = built(buttons={"a": "badge.next"})
        one.press("b")
        self.assertEqual(one.page_index, 0)
        self.assertEqual(one._commands, [])


class Commands(unittest.TestCase):
    def test_a_press_waits_for_the_connection_rather_than_an_idle_one(self):
        one = built()
        one.send_command("media_next")
        self.assertEqual(len(one._commands), 1)
        self.assertIsNone(one._pending, "it went out while a poll was in flight")

    def test_the_queue_has_a_ceiling(self):
        one = built()
        for index in range(app.COMMAND_QUEUE + 3):
            one.send_command(f"cmd{index}")
        self.assertEqual(len(one._commands), app.COMMAND_QUEUE)
        self.assertEqual(one.toast_text, "busy", "nothing said the press was dropped")

    def test_a_press_goes_out_before_the_badge_polls(self):
        """Both are due; the press is what a reader is waiting on."""
        one = paired(interval_ms=1000)
        one.send_command("media_next")
        one._queued = ("history", "/v1/history")
        one.poll()
        self.assertEqual(one._pending, "command")
        self.assertEqual(one._queued, ("history", "/v1/history"), "the queue was spent")

    def test_a_press_nobody_is_waiting_for_any_more_is_dropped(self):
        one = paired()
        one.send_command("media_next")
        command, _at = one._commands[0]
        one._commands = [(command, app.time.ticks_add(app.time.ticks_ms(),
                                                     -app.COMMAND_WAIT_MS - 1000))]
        one.poll()
        self.assertIsNone(one._pending, "a stale press was sent")
        self.assertEqual(one.toast_text, "dropped")

    def test_nothing_is_queued_for_a_binding_that_is_empty(self):
        one = built()
        one.send_command("")
        self.assertEqual(one._commands, [])


class Setup(unittest.TestCase):
    def test_an_unpaired_badge_offers_setup(self):
        self.assertTrue(app.App().needs_setup())

    def test_one_failed_poll_offers_setup(self):
        """Every control on the notice screen needs it, so waiting for three is a screen
        of buttons that do nothing."""
        one = paired()
        one.layout = None
        one.client.failures = app.SETUP_AFTER
        self.assertTrue(one.needs_setup())

    def test_a_badge_the_host_refused_offers_setup(self):
        one = paired()
        one.rejected = True
        self.assertTrue(one.needs_setup(), "a badge with dead credentials cannot re-pair")

    def test_a_working_badge_does_not(self):
        self.assertFalse(paired().needs_setup())

    def test_retrying_drops_the_backoff_and_what_was_in_flight(self):
        one = paired()
        one.client.failures = 4
        one._pending = "stats"
        one._queued = ("history", "/v1/history")
        one.send_command("media_next")
        one.detail = "connection refused"
        one.retry()
        self.assertEqual(one.client.failures, 0)
        self.assertIsNone(one._pending)
        self.assertIsNone(one._queued)
        self.assertEqual(one._commands, [], "a press meant for a silent host was kept")
        self.assertIsNone(one.detail)


class ForgetHost(unittest.TestCase):
    """Readings, series and revisions are numbered by whoever sent them."""

    def test_leaving_a_host_drops_everything_that_was_numbered_by_it(self):
        one = paired()
        one.layout_rev = 7
        one.history = {"cpu.pct": [1.0, 2.0]}
        one.slow = {"feed": {"hits": 3}}
        one.slow_rev = 4
        one._queued = ("history", "/v1/history")
        one._series_age = 500
        one._series_at = 900
        one.rejected = True
        one.send_command("media_next")

        one.forget_host()

        self.assertIsNone(one.layout)
        self.assertEqual(one.layout_rev, app.NO_REV)
        self.assertEqual(one.history, {})
        self.assertEqual(one.slow, {})
        self.assertEqual(one.slow_rev, app.NO_REV)
        self.assertIsNone(one._queued)
        self.assertEqual(one._commands, [])
        self.assertEqual((one._series_age, one._series_at), (0, 0))
        self.assertFalse(one.rejected)


class Backlight(unittest.TestCase):
    """What reaches the panel, captured where the app hands it over."""

    def setUp(self):
        self.asked = []
        app.display = type("Panel", (), {"backlight": lambda _self, value:
                                         self.asked.append(value)})()

    def tearDown(self):
        del app.display

    def test_the_panel_is_only_ever_asked_for_a_fraction(self):
        """`display.backlight` casts to a byte, so 2.4 wraps to a dark panel."""
        for asked in (-1.0, 0.0, 0.05, 1.0, 2.463):
            app.backlight(asked)
        self.assertEqual(self.asked, [0.0, 0.0, 0.05, 1.0, 1.0])

    def test_a_dark_room_dims_the_badge_and_does_not_switch_it_off(self):
        one = built(brightness=0.05, auto_brightness=True)
        one.ambient = 0.0
        app.backlight(one.wanted_brightness())
        self.assertTrue(0.0 < self.asked[-1] <= 1.0, self.asked)

    def test_the_configured_level_is_the_ceiling(self):
        one = built(brightness=0.5, auto_brightness=True)
        one.ambient = 1.0
        self.assertAlmostEqual(one.wanted_brightness(), 0.5)
        one.ambient = 0.0
        self.assertTrue(one.wanted_brightness() < 0.5, "a dark room brightened the panel")

    def test_the_brightness_button_wins_over_the_setting(self):
        one = built(brightness=0.8)
        one.dimmed = 0.24
        self.assertAlmostEqual(one.wanted_brightness(), 0.24)


class Settings(unittest.TestCase):
    """A layout setting reaching the switch that draws it."""

    def setUp(self):
        self.was = (draw.SMOOTH, draw.ROWS, draw.GAUGE_FILL, pages.PLOT_ANIMATION,
                    pages.ANIMATE)

    def tearDown(self):
        (draw.SMOOTH, draw.ROWS, draw.GAUGE_FILL, pages.PLOT_ANIMATION,
         pages.ANIMATE) = self.was
        pages.sweep_reset()

    def test_a_layout_moves_the_drawing_switches(self):
        one = built(smooth=False, rows="none", gauge_fill="ramp", plot_animation=True)
        one.apply_layout()
        self.assertFalse(draw.SMOOTH, "graphs are still smoothed")
        self.assertEqual(draw.ROWS, "none")
        self.assertEqual(draw.GAUGE_FILL, "ramp")
        self.assertTrue(pages.PLOT_ANIMATION)

    def test_a_layout_that_names_nothing_leaves_the_defaults(self):
        built().apply_layout()
        self.assertTrue(draw.SMOOTH)
        self.assertEqual(draw.ROWS, "zebra")
        self.assertEqual(draw.GAUGE_FILL, "solid")
        self.assertFalse(pages.PLOT_ANIMATION)

    def test_a_page_turn_drops_the_positions_everything_was_drawn_at(self):
        """A turn is not a change in the machine, so the needles start where they land."""
        one = built(animate=True)
        one.apply_layout()
        pages.fraction_of("cpu.pct", 0.4)
        self.assertTrue(pages._sweeps, "nothing was sweeping to begin with")
        one.turn(1)
        self.assertFalse(pages._sweeps, "a needle kept its position across a page turn")


class Series(unittest.TestCase):
    """Which fields a poll asks for a ring of."""

    def plotting(self, *kinds):
        one = app.App()
        one.layout = {"pages": [
            {"id": f"p{index}", "kind": kind, "title": kind, "fields": [f"g{index}.value"]}
            for index, kind in enumerate(kinds)]}
        return one

    def test_a_series_is_requested_only_for_the_pages_that_plot_one(self):
        """A sparkline and a trend draw one too, not only the graph pages."""
        one = self.plotting("graph", "dial", "spark", "text")
        self.assertEqual(one._plot_refs(), ["g0.value", "g2.value"])

    def test_the_request_starts_at_the_page_on_screen(self):
        """In page order, the refs at the end are never fetched, and the page holding
        them draws its live reading twice."""
        one = self.plotting("graph", "graph", "graph")
        one.turn(1)
        self.assertEqual(one._graph_keys()[0], "g1.value")

    def test_the_request_is_capped(self):
        one = self.plotting(*(["graph"] * (app.GRAPH_KEYS + 4)))
        self.assertEqual(len(one._graph_keys()), app.GRAPH_KEYS)
        self.assertTrue(len(one._plot_refs()) > app.GRAPH_KEYS, "nothing was left out")


class DrawingElsewhere(unittest.TestCase):
    def setUp(self):
        draw.prepare()      # main() does this before anything is drawn

    def test_a_page_drawn_into_an_image_puts_the_screen_back(self):
        """`screen` is a builtin, so it is rebound: an extension's renderer draws through
        the same name and would otherwise draw to the screen while the app drew the image.
        """
        one = built()
        one.theme = look.get(look.DEFAULT)
        was = screen  # noqa: F821
        target = image(look.W, look.H)  # noqa: F821
        one.draw_page_into(target, one.current_page())
        self.assertTrue(screen is was, "the page left itself bound to the image")  # noqa: F821

    def test_the_image_is_given_the_font_the_screen_has(self):
        """An image starts with none, and a page that labels anything wants one."""
        one = built()
        target = image(look.W, look.H)  # noqa: F821
        one.draw_page_into(target, one.current_page())
        self.assertTrue(target.font is not None)


class Rendering(unittest.TestCase):
    """A press answers on the frame it lands on, and the body catches up after it."""

    def setUp(self):
        draw.prepare()
        self.was_connected = app.wifi.is_connected
        # There is no radio here, and render() draws "No WiFi" before it draws a page.
        app.wifi.is_connected = lambda: True
        self.app = paired(slide="over")
        self.app.layout["pages"] = [
            {"id": "one", "kind": "dial", "title": "One", "field": "cpu.pct"},
            {"id": "two", "kind": "text", "title": "Two", "fields": ["sys.host"]}]
        self.app.theme = look.get(look.DEFAULT)

    def tearDown(self):
        app.wifi.is_connected = self.was_connected

    def test_a_turn_that_is_waiting_leaves_the_body_standing(self):
        """The title and the pip move on the press; redrawing the body as well is what
        the wait is holding off."""
        self.app.render()
        standing = body_pixels()
        self.app.turn(1)
        self.app.render()
        self.assertEqual(differing(standing, body_pixels()), 0.0,
                         "the body was redrawn while the turn was still waiting")

    def test_the_body_catches_up_once_the_wait_is_over(self):
        self.app.render()
        standing = body_pixels()
        self.app.turn(1)
        self.app._slide_at = 0          # the wait ran out and no slide was started
        self.app.render()
        self.assertTrue(differing(standing, body_pixels()) > 0.001,
                        "the second page never reached the screen")


class CaseLights(unittest.TestCase):
    """Four lights beside the screen, which are a brightness and not a colour."""

    def setUp(self):
        self.asked = []
        real, asked = badge, self.asked  # noqa: F821  the firmware's, shadowed below

        class Watched:
            """The badge, with what the lights are asked for written down."""

            def __getattr__(self, name):
                return getattr(real, name)

            def caselights(self, level):
                asked.append(level)

        app.badge = Watched()

    def tearDown(self):
        del app.badge       # back to the builtin

    def test_the_setting_off_is_dark(self):
        built(caselights=False).apply_caselights()
        self.assertEqual(self.asked, [0.0])

    def test_they_follow_the_backlight(self):
        """A dark room dims the screen; lights left burning are the wrong way round."""
        one = built(caselights=True, brightness=0.6)
        one.apply_caselights()
        self.assertAlmostEqual(self.asked[-1], one.wanted_brightness())

    def test_a_reading_moves_them_between_the_floor_and_that(self):
        one = built(caselights="cpu.pct", brightness=1.0)
        one.frame = {"cpu": {"pct": 100.0}}
        one.apply_caselights()
        self.assertAlmostEqual(self.asked[-1], 1.0)
        one.frame = {"cpu": {"pct": 0.0}}
        one.apply_caselights()
        self.assertAlmostEqual(self.asked[-1], app.CASELIGHT_FLOOR)

    def test_a_field_the_host_stopped_sending_sits_at_the_floor(self):
        one = built(caselights="cpu.pct", brightness=1.0)
        one.frame = {}
        one.apply_caselights()
        self.assertAlmostEqual(self.asked[-1], app.CASELIGHT_FLOOR)

    def test_the_brightness_button_takes_them_with_it(self):
        """Reapplied wherever the panel moves, or a press dims the screen alone."""
        one = built(caselights=True, brightness=1.0)
        one.apply_backlight()
        first = self.asked[-1]
        one.dimmed = 0.3
        one.apply_backlight()
        self.assertTrue(self.asked[-1] < first, self.asked)


class Slides(unittest.TestCase):
    def test_a_page_turn_with_the_setting_off_starts_no_slide(self):
        one = built(slide="off")
        one.turn(1)
        self.assertEqual(one._slide_at, 0)
        one.slide_due(app.time.ticks_ms())
        self.assertIsNone(one.sliding)

    def test_each_press_pushes_the_wait_out(self):
        """A burst is one slide, from the page the reader started on to the one they
        landed on."""
        one = built(slide="over")
        one.turn(1)
        # As if the first press had been a wait ago: two turns land in the same
        # millisecond, and it is the deadline moving that is under test.
        stale = app.time.ticks_add(one._slide_at, -app.SLIDE_WAIT_MS)
        one._slide_at = stale
        one.turn(1)
        self.assertTrue(app.time.ticks_diff(one._slide_at, stale) > 0, "the wait held")
        one.slide_due(stale)
        self.assertIsNone(one.sliding, "it moved on the first press's deadline")

    def test_a_press_during_a_slide_abandons_it(self):
        """Queueing behind the movement gives a slide per press, each one late."""
        one = built(slide="over")
        one.turn(1)
        one.sliding = "a movement in flight"
        one.turn(1)
        self.assertIsNone(one.sliding, "the press queued behind the slide it landed in")
        self.assertTrue(one._slide_at, "the press armed no wait of its own")

    def test_a_wait_that_has_not_run_out_starts_nothing(self):
        one = built(slide="over")
        one.turn(1)
        one.slide_due(one._slide_at - 1)
        self.assertIsNone(one.sliding)


if __name__ == "__main__":
    unittest.main()
