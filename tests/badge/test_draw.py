"""Drawing: gauges, plots, rows and the room each takes."""

import builtins
import html.parser
import pathlib
import re
import sys

import badgefakes

import pytest

from statsbadge import install, layout, themes


def test_a_row_of_text_and_a_plot_measures_its_columns():
    """A fixed column either leaves a gap after the names or runs the readings into the
    plots, and which of the two it does depends on the fields the page carries."""
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    for widget in ("def bars", "def sparklines", "def graph"):
        body = source[source.index(widget):]
        body = body[:body.index("\ndef ", 1)]
        assert "column_width(" in body, f"{widget} still lays out to a fixed column"

    # The gauge and its column sit in the band on one gap, so no part of the pair can be
    # placed on a number it picked.
    look_source = (pathlib.Path(install.app_source_dir()) / "look.py").read_text(encoding="utf-8")
    for name in ("DIAL_C = (DIAL_GAP", "READOUT_X = DIAL_C[0]",
                 "READOUT_W = W - READOUT_X - DIAL_GAP"):
        assert name in look_source, f"{name} is not derived from the dial's gap"


def test_a_split_page_takes_the_layout_it_is_given():
    """A dial, a ring stack and a clock face all split the band into something round and a
    column, and the pages are paged between: anything choosing a centre or margin
    moves under the reader when they press a button."""
    app = pathlib.Path(install.app_source_dir())

    # Four rings have to fit the same radius a single gauge draws in.
    look_source = (app / "look.py").read_text(encoding="utf-8")
    draw_source = (app / "draw.py").read_text(encoding="utf-8")
    scope = {}
    for line in look_source.splitlines():
        if line.startswith(("DIAL_OUTER", "DIAL_GAP", "READOUT_H", "READOUT_NOTE_H")):
            exec(line, scope)  # noqa: S102  a module in this repo, four constants off the top
    band = int(re.search(r"^RING_BAND = (\d+)", draw_source, re.M).group(1))
    gap = int(re.search(r"^RING_GAP = (\d+)", draw_source, re.M).group(1))
    innermost = scope["DIAL_OUTER"] - 4 * band - 3 * gap
    assert innermost >= 8, f"the fourth ring is {innermost} across; it would be dropped"

    # The clock takes both from the app, restating neither, and puts its column
    # where every other split page puts it.
    clock = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge")
             / "clockface.py").read_text(encoding="utf-8")
    assert "CENTRE = look.DIAL_C" in clock, "the clock face has a centre of its own"
    assert "RADIUS = look.DIAL_OUTER" in clock, "the clock face has a radius of its own"
    assert "look.READOUT_X" in clock and "draw.column_lines" in clock, (
        "the clock face lays its column out by hand")


def test_a_gauge_can_sweep_to_its_reading():
    """A reading lands once a second and the gauge may ease to it instead of stepping.

    The needle has to leave from where it *is*: a second reading arriving mid-sweep must
    carry on from the drawn position, not jump to the one it was heading for. And the frames
    only come while something is moving, or a sweeping page would redraw all second."""
    import sys

    config = layout.validate({"animate": True, "pages": layout.DEFAULT_PAGES})
    assert config["animate"] is True
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["animate"] is False, (
        "off by default")

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="animate"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert 'bindCheck("animate", "animate")' in (web / "app.js").read_text(encoding="utf-8"), \
        "the control is not bound"

    sys.path.insert(0, install.app_source_dir())
    import pages

    # A stand-in for the firmware's tween, driven by hand: the easing is picovector's, so
    # what is worth checking here is which endpoints each sweep is given.
    class FakeTween:
        CUBIC_OUT = "cubic_out"
        made = []

        def __init__(self, start, end, duration, _easing):
            self.from_, self.to, self.duration = start, end, duration
            self.progress = 0.0
            FakeTween.made.append((start, end))

        def start(self):
            return self

        @property
        def now(self):
            return self.from_ + (self.to - self.from_) * self.progress

        @property
        def done(self):
            return self.progress >= 1.0

    pages.__dict__["tween"] = FakeTween
    was = pages.ANIMATE
    try:
        pages.ANIMATE = False
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 40.0) == 0.4, "stepping when the setting is off"
        assert not FakeTween.made, "a sweep was started with the setting off"

        pages.ANIMATE = True
        # The first reading is drawn where it is: there is nowhere to come from.
        assert pages.fraction_of("cpu.pct", 40.0) == 0.4
        assert FakeTween.made == [(0.4, 0.4)], FakeTween.made
        assert not pages.moving, "a gauge with nowhere to go asked for another frame"

        # A new reading sweeps from the last, and asks for frames until it lands.
        assert pages.fraction_of("cpu.pct", 80.0) == 0.4, "the needle jumped to the reading"
        assert FakeTween.made[-1] == (0.4, 0.8), FakeTween.made
        assert pages.moving

        # Interrupted half way: from the drawn position, not from 0.8.
        pages._sweeps["cpu.pct"].progress = 0.5
        assert abs(pages.fraction_of("cpu.pct", 20.0) - 0.6) < 1e-9
        started, heading = FakeTween.made[-1]
        assert abs(started - 0.6) < 1e-9 and heading == 0.2, FakeTween.made

        # The same reading again continues the sweep in flight.
        made = len(FakeTween.made)
        pages.fraction_of("cpu.pct", 20.0)
        assert len(FakeTween.made) == made, "an unchanged reading restarted the sweep"

        # A page turn drops where everything stood, a turn not being a change in the
        # machine.
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 20.0) == 0.2
        assert FakeTween.made[-1] == (0.2, 0.2)
    finally:
        pages.ANIMATE = was
        pages.sweep_reset()
        pages.__dict__.pop("tween", None)

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    assert "pages_module.sweep_reset()" in app[app.index("def turn"):], (
        "a page turn keeps the last page's needle positions")
    assert "pages_module.moving" in app, "nothing asks for a frame while a gauge is moving"


def test_a_page_can_slide_on_like_a_card():
    """A window of the screen carries an origin, so a page drawn into one lands shifted and
    clipped: that is the card, and the rasteriser costs the window alone.
    `over` leaves the outgoing page standing under it; `deck` moves both, which needs a copy
    of the page that is leaving because a window cannot start at a negative origin."""
    for style in layout.SLIDE_STYLES:
        assert layout.validate({"slide": style,
                                "pages": layout.DEFAULT_PAGES})["slide"] == style
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["slide"] == "off", (
        "immediate by default")
    assert layout.validate({"slide": "sideways",
                            "pages": layout.DEFAULT_PAGES})["slide"] == "off"
    # A bool still works, from before there was a choice of styles.
    assert layout.validate({"slide": True, "pages": layout.DEFAULT_PAGES})["slide"] == "over"
    assert layout.validate({"slide": False, "pages": layout.DEFAULT_PAGES})["slide"] == "off"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="slide"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.slide" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    for style in layout.SLIDE_STYLES:
        assert f'value="{style}"' in (web / "index.html").read_text(encoding="utf-8"), style

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    sliding = app[app.index("def render_sliding"):]
    sliding = sliding[:sliding.index("\n    def ", 1)]
    # Both cards are a rect out of an image, which makes the direction free: a window
    # cannot start at a negative origin, so a page cannot be drawn part way off the left.
    assert "self.arriving.window(" in sliding and "self.leaving.window(" in sliding
    assert "self.slide_back" in sliding, "both directions look the same"

    into = app[app.index("def draw_page_into"):]
    into = into[:into.index("\n    def ", 1)]
    # Rebound, in place of passing it. An extension's renderer draws through the same
    # builtin, and
    # would otherwise put its page on the screen while the app drew into the image.
    assert "builtins.screen = target" in into and "builtins.screen = was" in into
    # From whatever screen is now: badge.mode replaces it, and a copy taken at import time is
    # the 160x120 screen the app started with - a 320-wide page drawn into that wraps.
    assert "was = screen" in into
    assert "target.font" in into, "an image starts with no font, and label() restores it"

    # The turn only starts one when the layout asks, keeping the screen only for a deck.
    turn = app[app.index("def turn"):]
    turn = turn[:turn.index("\n    def ", 1)]
    assert 'setting("slide")' in turn and "delta < 0" in turn
    # A press schedules the movement, so a burst is one slide onto the page it landed on
    # and one only, several fighting over the screen being the fault.
    assert "SLIDE_WAIT_MS" in turn
    due = app[app.index("def slide_due"):]
    due = due[:due.index("\n    def ", 1)]
    assert "self.sliding is not None" in due, "a second slide can start over a running one"

    # The title and the pip answer every press, including presses that land during a slide,
    # so paging through five pages moves the pip five times and slides once. That takes two
    # things together, and either alone is a bug that shipped:
    #
    #   - the wait is drawn ahead of a running slide, or a press mid-slide leaves the pip
    #     stuck until the movement finishes;
    #   - a press abandons the slide it lands in, clearing whatever the wait
    #     is now suppressing. Queueing behind it instead gave a slide per press, each late.
    body = app[app.index("    def render(self):"):]
    body = body[:body.index("\n    def ", 1)]
    assert body.index("self._slide_at") < body.index("self.sliding is not None"), (
        "a press during a slide cannot move the pip")
    assert "self.sliding = None" in turn, "a press queues behind the slide it lands in"
    assert "draw.furniture(" in body, "the press does not answer until the body catches up"
    # The body is withheld on a deadline, the flag alone being unsafe, so it can
    # hold it back for longer than the wait however the state is arrived at.
    assert "time.ticks_diff(self._slide_at" in body, (
        "the body can be withheld for longer than the wait")
    start = app[app.index("def start_slide"):]
    assert 'style == "deck"' in start[:start.index("\n    def ", 1)]


def test_smooth_graphs_are_a_setting_that_reaches_the_badge():
    """A drawing switch, so it is one setting covering every graph on the badge."""
    config = layout.validate({"smooth": False, "pages": layout.DEFAULT_PAGES})
    assert config["smooth"] is False
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["smooth"] is True, "on by default"
    # Anything truthy, since the UI sends a checkbox and a command line sends a string.
    assert layout.validate({"smooth": "yes", "pages": layout.DEFAULT_PAGES})["smooth"] is True

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="smooth"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.smooth" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    # The badge applies it where it applies the rest of the layout.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.SMOOTH" in applied[:applied.index("\n    def ", 1)]


def test_the_big_gauge_can_show_the_whole_ramp():
    """A conical gradient follows the arc, so the ramp lays round the gauge with the part
    past the reading left faint, and the scale shows as well as the reading. Only the dial
    page's gauge, which is the one with a page to itself."""
    import sys

    for fill in layout.GAUGE_FILLS:
        assert layout.validate({"gauge_fill": fill,
                                "pages": layout.DEFAULT_PAGES})["gauge_fill"] == fill
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid", (
        "one colour by default")
    assert layout.validate({"gauge_fill": "rainbow",
                            "pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="gaugefill"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.gauge_fill" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.GAUGE_FILL" in applied[:applied.index("\n    def ", 1)]

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    theme = look.get("dark")
    turn = (look.DIAL_TO - look.DIAL_FROM) / 360.0
    fill, track = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)
    assert fill.kind == badgefakes.Brush.CONICAL
    # Fractions of a whole turn, so a 270 degree gauge lays the ramp over three quarters of
    # one, and the ramp's positions in order.
    assert [pos for pos, _ in fill.stops] == [pos * turn for pos, _ in theme.ramp]
    assert [pen for _, pen in fill.stops] == [pen for _, pen in theme.ramp]
    # The track is the same ramp, dimmed by the colours themselves: a gradient brush ignores
    # screen.alpha.
    assert [pos for pos, _ in track.stops] == [pos for pos, _ in fill.stops]
    assert {pen.a for _, pen in track.stops} == {draw.TRACK_ALPHA}
    assert {pen.a for _, pen in fill.stops} == {255}

    # Read backwards for a field whose severity is, so the sweep's end is still the reading's
    # colour it sits at: a battery at 100% is a machine doing well.
    backwards, _ = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, True)
    positions = [pos for pos, _ in backwards.stops]
    assert positions == sorted(positions), positions
    assert backwards.stops[0][1] == theme.ramp[-1][1], "it does not start at the hot end"
    assert backwards.stops[-1][1] == theme.ramp[0][1]
    pages_source = (pathlib.Path(install.app_source_dir()) / "pages.py").read_text(encoding="utf-8")
    assert "backwards=field in GOOD_HIGH" in pages_source, (
        "nothing tells the gradient which way the field is read"
    )

    # Built once a theme: a pair from OKLCH stops is 3.4ms, where moving the geometry is 12us
    # and the arc costs the same to draw either way.
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is fill
    draw.clear_cache()
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is not fill, (
        "a theme change would leave the old ramp round the gauge")

    # The setting settles it, with the solid fill asking for no brush at all.
    seen = {}
    real = draw.gauge
    draw.gauge = lambda *_args, **named: seen.update(named)
    try:
        draw.GAUGE_FILL = "solid"
        draw.dial(theme, 0.5, "50", "%")
        assert seen["swept"] is None
        draw.GAUGE_FILL = "ramp"
        draw.dial(theme, 0.5, "50", "%")
        assert seen["swept"] is not None and len(seen["swept"]) == 2
    finally:
        draw.gauge = real
        draw.GAUGE_FILL = "solid"
        draw.clear_cache()


def test_a_smoothed_graph_still_reads_as_the_data():
    """A curve through the samples, and never near them. This graphs a machine, so a peak
    drawn where there was none, or short of the one there was, misreports it."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw

    values = [0.2, 0.9, 0.3, 0.31, 0.8, 0.1, 0.5]
    dense = draw.curve(values, steps=4)
    assert len(dense) == (len(values) - 1) * 4 + 1, len(dense)
    # Every sample is still on the curve, at the position it was in.
    for index, value in enumerate(values):
        assert abs(dense[index * 4] - value) < 1e-9, (index, dense[index * 4], value)
    # A spline's overshoot is held to the range of the data, or an area fill would run
    # under the baseline where the reading touched zero.
    assert min(dense) >= min(values) and max(dense) <= max(values), (
        min(dense), max(dense))

    # Fewer than three points cannot be interpolated.
    assert draw.curve([0.5, 0.6], steps=4) == [0.5, 0.6]

    # `curve_steps` settles whether to interpolate, answering 1 for "draw it straight".
    # With the switch off, with too few samples, and when the plot is too
    # short for a curve to show - a sparkline is 22px tall and reads the same either way.
    assert draw.curve_steps(250, 150, len(values)) > 1
    assert draw.curve_steps(250, 22, len(values)) == 1
    assert draw.curve_steps(250, 150, 2) == 1
    draw.SMOOTH = False
    try:
        assert draw.curve_steps(250, 150, len(values)) == 1
    finally:
        draw.SMOOTH = True

    # The weights are worked out once: evaluating the polynomial per point cost 265us a
    # point on the badge, which is 50ms for one series.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def curve("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_basis(steps)" in body, "the weights are not taken from the table"

    # An axis with no full scale steps to round numbers, holding off a fit to
    # window, or it creeps on every poll as samples arrive and leave - the plot rescaling
    # slightly each time, which shows as the graph twitching. A byte rate steps in
    # 1024s so the label is a number a reader can place a sample against.
    assert draw.axis_top(900, "down_bps") == 1024
    assert draw.axis_top(6 * 1024 ** 2, "down_bps") == 10 * 1024 ** 2
    assert draw.axis_top(41943040, "down_bps") == 50 * 1024 ** 2
    assert draw.reading(draw.axis_top(41943040, "down_bps"), "down_bps") == "50.0MB/s"
    # Anything else steps in tens, so a temperature plot tops out at 100 and not at 81.6.
    assert draw.axis_top(71.0, "temp") == 100
    assert draw.axis_top(30.0, "temp") == 50
    # It holds still while the busiest sample moves, which is the point.
    for peak in (6.1, 6.5, 7.0, 9.9):
        assert draw.axis_top(peak * 1024 ** 2, "down_bps") == 10 * 1024 ** 2, peak

    # A gap in a ring is a None, and the axis works its top out from the samples. Comparing
    # one against a float is a TypeError, which took the app down mid-slide on a machine
    # with an intermittent sensor.
    axis = source[source.index("def graph("):]
    axis = axis[:axis.index("\ndef ", 1)]
    axis = axis[axis.index("if maximum is None:"):axis.index("    peak_text")]
    assert "is not None" in axis, f"a None in a series reaches max(): {axis}"

    # Every widget draws a gap at the axis, decided in one place: six sites each said
    # `or 0.0` and one of them was reached with the raw series and crashed.
    assert "or 0.0" not in source, "a widget is deciding what a gap looks like on its own"
    assert draw.flat([0.5, None, 0.25]) == [0.5, 0.0, 0.25]
    same = [0.5, 0.25]
    assert draw.flat(same) is same, "a series with no gaps is copied every frame"
    layout = source[source.index("def _lay_out("):]
    layout = layout[:layout.index("\ndef ", 1)]
    assert "values = flat(values)" in layout, layout
    # The series as the ring hands it over, gaps and all, through the widget that crashed.
    gappy = [0.5, None, 0.25, 0.9, None, None, 0.1, 0.4]
    assert draw._lay_out(60, 40, 250, 150, gappy, 1.0, None) > 0  # noqa: SLF001

    # A fill and a line are the same layout with different ends on it, so both go through
    # _lay_out, each scaling its samples once.
    for name in ("def area(", "def line("):
        widget = source[source.index(name):]
        widget = widget[:widget.index("\ndef ", 1)]
        assert "_lay_out(" in widget, f"{name} lays its points out separately"

    # A sparkline is stroked, and how it is stroked sets the cost: a round join is an arc
    # at every sample and 3.5ms a page, where the weight is free.
    trace = source[source.index("LINE_FLAGS = "):]
    trace = trace[:trace.index("\n")]
    assert "JOIN_MITER" in trace and "PATH_OPEN" in trace, trace
    # Centred, or the band grows to one side of the samples it is drawn from.
    assert "ALIGN_CENTER" in trace, trace
    sparks = source[source.index("def sparklines("):]
    sparks = sparks[:sparks.index("\ndef ", 1)]
    assert "line(plot_x" in sparks, "the sparkline page is not drawing lines"
    assert "screen.alpha" not in sparks, "a line does not need to let the page through"


def test_a_plot_is_placed_by_when_its_readings_were_taken():
    """Three clocks are in play. The host samples every `serve --interval`, the badge polls
    every `interval_ms`, and each is blind to the other's rate.

    A plot animated off an index axis has to guess, and did: it walked at the wrong pace,
    paused, and jumped. The host sends how far apart its points are and how old the newest
    is, and everything follows from that."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import pages

    # How many of the host's points one poll covers: both figures known, and divided.
    pages.note_spacing(1000, 1000)
    assert (pages.EVERY_MS, pages.LEAD) == (1000, 1)
    pages.note_spacing(1000, 5000)
    assert pages.LEAD == 5, "a 5s refresh against a 1s host is handed five at a time"
    pages.note_spacing(250, 1000)
    assert pages.LEAD == 4
    pages.note_spacing(1000, 1250)
    assert pages.LEAD == 2, "rounded up, or the plot is short of room"

    # How far back in the series now is: the age the host quoted plus the time since.
    pages.note_spacing(1000, 1000)
    assert pages.behind_at(0, 0) == 0.0
    assert abs(pages.behind_at(200, 300) - 0.5) < 0.001
    assert abs(pages.behind_at(0, 2500) - 2.5) < 0.001
    # A host that has stopped answering does not scroll a plot off into nothing.
    assert pages.behind_at(0, 600_000) == pages.BEHIND_MAX

    # Motion needs the setting, and it is a setting apart: sweeping a gauge and
    # animating a plot are different choices.
    was = pages.PLOT_ANIMATION
    try:
        pages.PLOT_ANIMATION = False
        assert pages._walk() is None
        pages.PLOT_ANIMATION = True
        pages.BEHIND = 0.5
        assert pages._walk() == 0.5
    finally:
        pages.PLOT_ANIMATION = was
        pages.BEHIND = 0.0
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["plot_animation"] is False
    assert layout.validate({"plot_animation": True,
                            "pages": layout.DEFAULT_PAGES})["plot_animation"] is True
    web = pathlib.Path("src/statsbadge/web")
    assert 'id="plotanim"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert 'bindCheck("plotanim", "plot_animation")' in (web / "app.js").read_text(encoding="utf-8"), \
        "it is not bound"

    # A graph keeps room on its right for the samples still coming in. Laid across the
    # width alone it shifts left and leaves a gap that grows and snaps back.
    flat = [50.0] * 48

    def ends(shift, lead=1):
        draw.WALK_LEAD = lead
        try:
            written = draw._lay_out(60, 40, 250, 150, flat, 100.0, shift)
        finally:
            draw.WALK_LEAD = 2
        return draw._points[0], draw._points[written - 2]

    first, last = ends(None)
    assert abs(first - 60) < 0.01 and abs(last - 310) < 0.01, (first, last)
    for lead in (1, 2, 5):
        for tenth in range(11):
            _first, last = ends(lead * tenth / 10.0, lead)
            assert last >= 310 - 0.01, (lead, tenth, last)

    # A series too short to walk is drawn where it stands. `_graph` plots a field it has no
    # ring for as its live reading twice, and a step is the box over the samples on it: two
    # of them put a whole plot width in one reading, which swept a slab across the page and
    # off the side of it every poll.
    for samples in range(2, draw.WALK_MIN):
        held = [50.0] * samples
        written = draw._lay_out(60, 40, 250, 150, held, 100.0, 0.75)
        assert abs(draw._points[0] - 60) < 0.01, (samples, draw._points[0])
        assert abs(draw._points[written - 2] - 310) < 0.01, (samples,
                                                             draw._points[written - 2])
    walked = draw._lay_out(60, 40, 250, 150, [50.0] * draw.WALK_MIN, 100.0, 0.75)
    assert draw._points[0] < 60 - 0.01, "a series long enough to walk is standing still"
    assert walked

    # A sparkline is drawn still whatever the setting says. 22px tall with a sample every
    # 5px, it has nowhere to scroll, and interpolating at fixed x is a translation
    # whatever it is called, which shows as a jump and not as points settling.
    assert pages.SCROLLS == ("graph", "trend"), pages.SCROLLS
    assert "spark" in pages.PLOTS, "it still wants a series fetched for it"
    sparks = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = sparks[sparks.index("def sparklines("):]
    body = body[:body.index("\n# --", 1)]
    assert "shift" not in body, "a sparkline is still being handed an offset"
    assert "if trace is not None:" in body, "a None can still reach the renderer"
    # Two readings is where a field with no history yet lands, and it must still draw.
    assert draw.line(0, 0, 470, 30, [5.0, 5.0], 47.0) is not None
    assert draw.line(0, 0, 470, 30, [5.0], 47.0) is None

    # Every page that draws a series asks for one, not only the graph pages: a sparkline was
    # plotting the live value twice, a flat line whatever the machine was doing.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    refs = app[app.index("    def _plot_refs(self"):]
    refs = refs[:refs.index("\n    def ", 1)]
    assert "pages_module.PLOTS" in refs, "only the graph pages ask for a series"

    # One poll cannot ask for every ref a layout plots, and the ones it does ask for start
    # at the page on screen. In page order the refs at the end were never fetched at all,
    # and the page holding them drew its live reading twice instead of its history.
    keys = app[app.index("    def _graph_keys(self):"):]
    keys = keys[:keys.index("\n    def ", 1)]
    assert "self._plot_refs(self.page_index)" in keys, "the ask does not follow the reader"
    assert "[:GRAPH_KEYS]" in keys, "the ask is unbounded"
    plotted = {ref for page in layout.DEFAULT_PAGES if page.get("kind") in pages.PLOTS
               for ref in page.get("fields", []) + [page.get("field")] if ref}
    cap = int(re.search(r"^GRAPH_KEYS = (\d+)", app, re.M).group(1))
    assert cap >= len(plotted), (cap, sorted(plotted))
    # Merged into what is held, or turning a page drops the rings the last ask covered and
    # every plot on the new one falls back to a pair until the next poll lands.
    landed = app[app.index('elif what == "history":'):]
    landed = landed[:landed.index("\n        self.dirty", 1)]
    assert "self.history.update(" in landed, "a reply replaces the rings it did not carry"
    assert "self._plot_refs()" in landed, "nothing drops a ring the layout stopped plotting"
    # It comes with its age, every poll, on the stats' schedule. v=3, since
    # a source may answer for a ring that is not on the host's clock.
    assert "&v=3" in app, "the series is fetched without the times it needs"

    # A ring a source answers for itself is on whatever clock its readings are really on -
    # an hour, for a domain's traffic - and a plot is translated as a whole, so one of those
    # is drawn still. Walking it by a number counted in the collector's samples would slide
    # it a year an hour.
    try:
        pages.note_series_spacing({"cf_pinout_xyz.requests": {"every_ms": 3600000}})
        pages.PLOT_ANIMATION = True
        pages.BEHIND = 0.5
        assert pages._walk(("cpu.pct",)) == 0.5
        assert pages._walk(("cf_pinout_xyz.requests",)) is None
    finally:
        pages.note_series_spacing({})
        pages.PLOT_ANIMATION = was
        pages.BEHIND = 0.0
    assert "note_spacing" in app and "behind_at" in app


def test_sparkline_rows_can_be_told_apart():
    """Six lines on one page looked like one plot with six traces, so the rows are banded.

    The band is worked out from the theme and not named in a palette: a step of
    lightness from the page, which is a step in the same direction whatever the page is.
    """
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    for style in layout.ROW_STYLES:
        assert layout.validate({"rows": style,
                                "pages": layout.DEFAULT_PAGES})["rows"] == style
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["rows"] == "zebra", (
        "banded by default")
    assert layout.validate({"rows": "stripey",
                            "pages": layout.DEFAULT_PAGES})["rows"] == "zebra"

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="rows"' in (web / "index.html").read_text(encoding="utf-8"), "no control in the UI"
    assert "config.rows" in (web / "app.js").read_text(encoding="utf-8"), "the control is not bound"
    for style in layout.ROW_STYLES:
        assert f'value="{style}"' in (web / "index.html").read_text(encoding="utf-8"), style

    # The badge applies it where it applies the rest of the layout.
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    applied = app[app.index("def apply_layout"):]
    assert "draw.ROWS" in applied[:applied.index("\n    def ", 1)]

    # A lift, not the panel colour: a panel can be a different hue as well as a different
    # level, which on a near-black page draws a stripe of colour.
    dark = look.THEMES["dark"]
    assert (dark.stripe.r - dark.bg.r == dark.stripe.g - dark.bg.g
            == dark.stripe.b - dark.bg.b == look.STRIPE), "the band shifts hue"
    # Toward the ink on a dark page and away from it on a pale one, since lighten has
    # nowhere to go on a background that is already near white.

    pale = look.from_palette("light", themes.written()["light"])
    assert pale.pale and not dark.pale
    assert pale.stripe.r < pale.bg.r and dark.stripe.r > dark.bg.r

    # The axis rule under a plot is drawn only where the rows are otherwise unseparated.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    sparks = source[source.index("def sparklines("):]
    sparks = sparks[:sparks.index("\ndef ", 1)]
    assert "if ROWS == ROW_NONE:" in sparks, "the axis is drawn whatever separates the rows"
    assert draw.ROWS == "zebra" and draw.ROW_NONE == "none"


def test_a_symbol_centres_on_the_words_beside_it():
    """An icon and a string on one baseline do not line up: the icon's box stands a fifth
    taller than a capital and its ink sits in the middle of that box, so the symbol floats.
    """
    import sys

    sys.path.insert(0, install.app_source_dir())
    sys.path.insert(0, str(pathlib.Path("tools")))
    import draw
    import read_af

    # The placement holds only while an icon's ink is centred in a box sat on the baseline,
    # so that is read out of the fonts every time. Through the tool, so a font
    # repacked wide is read as wide, the flag saying which.
    fonts = (pathlib.Path(install.app_source_dir()) / "icons.af",
             pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"
                          "/icons.af"))
    for path in fonts:
        font = read_af.read(str(path))
        box = draw.ICON_BOX * font["units_per_em"]
        for glyph in font["glyphs"]:
            if not glyph["contours"]:
                continue
            assert -1 <= glyph["bbox_y"] and glyph["bbox_y"] + glyph["bbox_h"] <= box + 1, (
                path.name, glyph, box)
            assert abs(glyph["bbox_y"] - (box - glyph["bbox_h"]) / 2.0) <= 1, (
                f"{path.name} {chr(glyph['codepoint'])!r} is not centred in its box")

    text_y, text_size, icon_size = 100, 26, 32
    icon_y = draw.icon_baseline(text_y, text_size, icon_size)
    cap_middle = text_y + text_size * (1.0 - draw.CAP / 2.0)
    ink_middle = icon_y + icon_size * (1.0 - draw.ICON_BOX / 2.0)
    assert abs(cap_middle - ink_middle) <= 1, (cap_middle, ink_middle)
    # Lower than a shared baseline puts it, which was the bug.
    assert icon_y > text_y + text_size - icon_size


def test_the_shipped_fonts_are_packed_as_the_metrics_assume():
    """draw.CAP and draw.ICON_BOX are fractions of the size a string is drawn at, and hold
    only while the fonts keep the em those numbers came from. A wide font is the same ratios
    at a finer grid, so nothing here cares which a font is - but one repacked to different
    proportions would move every symbol and mis-size every big number."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    sys.path.insert(0, str(pathlib.Path("tools")))
    import draw
    import read_af

    text = read_af.read(str(pathlib.Path(install.app_source_dir())
                            / "fonts" / "lexend-regular.af"))
    cap = next(g for g in text["glyphs"] if g["codepoint"] == ord("H"))
    assert abs(cap["bbox_h"] / text["units_per_em"] - draw.CAP) < 0.01, (
        cap["bbox_h"], text["units_per_em"], draw.CAP)

    # The LCD face's digits stand where a capital does, or the clock sizes one of its faces
    # by numbers that do not describe it.
    lcd = read_af.read("extensions/statsbadge-clock/src/statsbadge_clock/badge/lcd.af")
    eight = next(g for g in lcd["glyphs"] if g["codepoint"] == ord("8"))
    assert abs(eight["bbox_h"] / lcd["units_per_em"] - draw.CAP) < 0.01, (
        eight["bbox_h"], lcd["units_per_em"], draw.CAP)

    # The other digital face draws its colon as two circles, at the positions this one's
    # glyph puts them: paging between the faces should leave the colon where it was. The
    # pair sits a little low and is not symmetric about the digits, which is why the
    # numbers are measured here and not chosen.
    clockface = pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"
                             "/clockface.py").read_text(encoding="utf-8")
    at = re.search(r"^COLON_AT = \(([\d.]+), ([\d.]+)\)", clockface, re.M)
    radius = re.search(r"^COLON_W, COLON_DOT = [\d.]+, ([\d.]+)", clockface, re.M)
    assert at and radius, "the dot colon is not measured in fractions of the digit height"

    colon = next(g for g in lcd["glyphs"] if g["codepoint"] == ord(":"))
    up = [-y for y in colon["points"][1::2]]      # points run down from the baseline
    span = eight["bbox_h"]
    halfway = (min(up) + max(up)) / 2.0
    for drawn, dot in zip(at.groups(), ([y for y in up if y > halfway],
                                        [y for y in up if y < halfway]), strict=True):
        centre = (min(dot) + max(dot)) / 2.0
        assert abs((span - centre) / span - float(drawn)) < 0.005, (drawn, centre, span)
        assert abs((max(dot) - min(dot)) / 2.0 / span - float(radius.group(1))) < 0.005, dot

    # The digital face's digits are the app's face at a finer grid, so they have to agree
    # with it on both counts: the cap it is sized from and the width it is placed by. A
    # mismatch draws a time that is the wrong height or does not sit in its column.
    digits = read_af.read(
        "extensions/statsbadge-clock/src/statsbadge_clock/badge/digits.af")
    assert digits["wide"], "the face that draws digits 84pt tall wants the finer grid"
    for char in "0123456789:":
        assert any(g["codepoint"] == ord(char) for g in digits["glyphs"]), char
    for char in ("H", "0"):
        theirs = next(g for g in digits["glyphs"] if g["codepoint"] == ord(char))
        ours = next(g for g in text["glyphs"] if g["codepoint"] == ord(char))
        assert abs(theirs["bbox_h"] / digits["units_per_em"]
                   - ours["bbox_h"] / text["units_per_em"]) < 0.01, char
        assert abs(theirs["advance"] / digits["units_per_em"]
                   - ours["advance"] / text["units_per_em"]) < 0.01, char


def test_every_clock_face_the_ui_offers_can_be_drawn():
    """The face list is host side and the renderers are badge side, so a face added to one
    and not the other is a page that draws the default and says nothing."""
    badge = (pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge"))
    source = (badge / "clockface.py").read_text(encoding="utf-8")
    Clock = pytest.importorskip("statsbadge_clock").Clock

    offered = next(s for s in Clock.page_settings if s["key"] == "face")["options"]
    # The dials and the dial-less faces are two tables; between them they are the renderers.
    drawn = set()
    for table in ("FACES = {", "DIGITAL = {"):
        block = source[source.index(table):]
        block = block[:block.index("\n}\n")]
        drawn.update(re.findall(r'^    "([a-z]+)": \{', block, re.M))
    assert set(offered) == drawn, (sorted(offered), sorted(drawn))

    # The seven-segment face needs a font, which is an asset and not code, so it
    # travels only if it is declared.
    assert any(path.endswith("lcd.af") for path in Clock.badge_assets), Clock.badge_assets
    assert (badge / "lcd.af").exists(), "the LCD face's font is not built"
    # Shipped, so its licence ships with it.
    licence = pathlib.Path("licences/OFL-DSEG.txt").read_text(encoding="utf-8")
    assert "keshikan" in licence and "SIL Open Font License" in licence

    # The unlit segments go down before the lit ones, or they cover them.
    body = source[source.index("def _digital"):]
    body = body[:body.index("\ndef ", 1)]
    assert body.index('spec["ghost"]') < body.index("draw.blit_label(hours,"), (
        "the ghost is drawn over the digits")


def test_a_notifications_page_sorts_messages_from_counters():
    """One slot list holding two sorts of thing, told apart by looking at the reading.

    That lets one page kind be a feed, a mention, a headline and a follower count in
    whatever mixture: a message is a dict carrying `text`, everything else is a number. The
    alternative was two slot lists and a UI that has to know which is which.
    """
    sys.path.insert(0, install.app_source_dir())
    import draw
    import look
    import pages

    frame = {"feed": {
        "home": {"title": "Maaike", "text": "a post", "age_s": 420, "note": "boosted"},
        "mention": {"title": "dinkster75", "text": "a mention", "age_s": 34200},
        "followers": 1350, "posts": 6466}}

    drawn = {}
    was = draw.notification
    draw.notification = lambda _theme, items, counters: drawn.update(
        items=items, counters=counters)
    try:
        def render(fields):
            drawn.clear()
            pages._notify({"kind": "notify", "fields": fields}, frame, {},
                          look.get("dark"))
            return drawn

        out = render(["feed.home", "feed.mention", "feed.followers", "feed.posts"])
        assert [item["title"] for item in out["items"]] == ["Maaike", "dinkster75"]
        assert out["counters"] == [("FOLLOWERS", "1350"), ("POSTS", "6466")], out["counters"]

        # Order in the slot list does not have to be messages first
        out = render(["feed.followers", "feed.home"])
        assert len(out["items"]) == 1 and len(out["counters"]) == 1, out

        # A page of only one or the other still draws
        assert render(["feed.home"])["counters"] == []
        assert render(["feed.followers"])["items"] == []
        # A field the host stopped producing draws a counter of "--", where it crashed
        assert render(["feed.gone"])["counters"] == [("GONE", "--")], render(["feed.gone"])
    finally:
        draw.notification = was

    # The message shape is four things, and the age is drawn to suit its size. Minutes up
    # to ninety, then hours, then days, off the one function the quake page reads.
    assert [draw.ago(s) for s in (None, 5, 90, 4000, 100000, 400000)] == [
        None, "just now", "1m ago", "66m ago", "27h ago", "4d ago"]


def rules_of(css):
    """Every rule in the sheet, as its full selector and the declarations under it. The
    sheet nests, so a rule's selector is the chain of the ones it sits inside."""
    chain, declarations, found, buffer = [], [], [], ""
    for char in css:
        if char == "{":
            above, _, selector = buffer.rpartition(";")
            if declarations:
                declarations[-1] += above
            chain.append(selector.strip())
            declarations.append("")
            buffer = ""
        elif char == "}":
            declarations[-1] += buffer
            found.append((" ".join(chain), declarations[-1]))
            chain.pop()
            declarations.pop()
            buffer = ""
        else:
            buffer += char
    return found


def test_a_hidden_row_is_actually_hidden(web_dir):
    """The browser's rule for `hidden` is one attribute selector, so anything naming a
    class or an attribute outranks it and the row stays on screen.

    The second accent takes `display: flex`, to sit its swatch beside the select, and showed
    for every theme - where only a derived palette works one out.
    """
    css = (web_dir / "app.css").read_text(encoding="utf-8")
    markup = (web_dir / "index.html").read_text(encoding="utf-8")

    class Hidden(html.parser.HTMLParser):
        """Every element the page starts out hiding, and how a rule could name it."""

        def __init__(self):
            super().__init__()
            self.depth, self.found = 0, []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "hidden" in attrs:
                named = {tag} | {f".{name}" for name in attrs.get("class", "").split()}
                named |= {f"[{key}]" for key in attrs if key.startswith("data-")}
                self.found.append((tag, self.depth, named))
            if tag not in ("input", "link", "meta", "br"):
                self.depth += 1

        def handle_endtag(self, _tag):
            self.depth -= 1

    parser = Hidden()
    parser.feed(markup)
    # The sheet each tab shows, and the rows only a derived palette has.
    assert len({depth for _tag, depth, _named in parser.found}) > 1, parser.found

    # A rule that gives one of them a display has to stand aside when it is hidden. The
    # two selectors are otherwise close enough for source order to settle which wins.
    for selector, declarations in rules_of(css):
        last = re.split(r"[\s>+~]+", selector)[-1]
        if "display:" not in declarations or "[hidden]" in selector:
            continue
        if not (selector.startswith("main") or selector == last):
            continue            # it cannot reach inside a sheet
        for tag, depth, named in parser.found:
            if tag == "section":
                continue        # the sheets, hidden by a rule elsewhere
            for each in named:
                found = (re.search(rf"\b{each}\b", last) if each.isalpha()
                         else each in last)
                assert not found, (selector, tag, depth, each)


def test_a_picture_is_cropped_to_the_block_it_is_in():
    """A message three to a page has 52px of block and the large preset is 96 tall.

    Cropped, holding off any scale: the pixels are palette indices, so halfway between two of
    them is a third colour and not a blend of the two.
    """
    sys.path.insert(0, install.app_source_dir())
    import draw

    class FakeRect:
        """`rect` is the firmware's; the crop only needs somewhere to put four numbers."""

        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    class Picture:
        """Enough of an indexed image to be cropped: a size, and a view of part of it."""

        def __init__(self, width, height):
            self.width, self.height, self.taken = width, height, None

        def window(self, box):
            self.taken = box
            return Picture(box.w, box.h)

    was = getattr(builtins, "rect", None)
    builtins.rect = FakeRect
    try:
        _check_cropping(draw, Picture)
    finally:
        if was is None:
            del builtins.rect
        else:
            builtins.rect = was


def _check_cropping(draw, Picture):
    # Room to spare, so it is drawn whole and kept
    whole = Picture(128, 96)
    assert draw.fitted(whole, 96) is whole and whole.taken is None
    assert draw.fitted(whole, 200) is whole and whole.taken is None

    # Two messages to a page: 78px of block, less its padding
    tall = Picture(128, 96)
    band = draw.fitted(tall, 70)
    assert (band.width, band.height) == (128, 70), (band.width, band.height)
    # From the middle, the crop that made the picture having put what matters there
    assert (tall.taken.x, tall.taken.y) == (0, 13), (tall.taken.x, tall.taken.y)

    # Below a band worth looking at, none: a smear is worse than the room it takes
    assert draw.fitted(Picture(128, 96), 12) is None
    assert draw.fitted(None, 70) is None


def test_a_message_shortens_the_way_the_firmware_does():
    """A post is whatever length it is and the block has room for two or three lines.

    The firmware flows and truncates, `screen.text` taking a rect and an overflow, so this
    checks the page asks for that and reimplements none of it. Doing that here is a
    `measure_text` a word to find the breaks and another per character to trim the last
    line, in Python, on every draw. Measured on a Tufty, that was the page at 34.9ms
    settled against 24.8 for the same page drawn by the firmware.
    """
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def flow("):source.index("def picture(")]
    assert "overflow=ELLIPSES" in body, "the page is not asking for the truncation"
    assert "screen.measure_text(" not in body, "still measuring text to lay it out"
    assert "def wrap(" not in source, "the hand-rolled wrapper is still here"

    # `fit` is still needed for a single line - a name beside a time - and halves rather
    # than trimming a character per measurement.
    fitting = source[source.index("def fit("):]
    fitting = fitting[:fitting.index("\n\n\n")]
    assert "low, high" in fitting and "middle" in fitting, "fit is back to one at a time"
