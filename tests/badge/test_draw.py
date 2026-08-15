"""Drawing: gauges, plots, rows and the room each takes."""

import builtins
import html.parser
import pathlib
import re
import sys

import badgefakes

import pytest

from statsbadge import install, layout, themes


def test_the_gauge_and_its_column_sit_on_one_gap(badge_constants):
    """One gap left of the dial, one between it and the column, one at the right edge."""
    look = badge_constants("look.py")
    gap, outer = look["DIAL_GAP"], look["DIAL_OUTER"]

    assert look["DIAL_C"][0] - outer == gap, (look["DIAL_C"], outer, gap)
    assert look["READOUT_X"] == look["DIAL_C"][0] + outer + gap, look["READOUT_X"]
    assert look["READOUT_W"] == look["W"] - look["READOUT_X"] - gap, look["READOUT_W"]
    assert look["READOUT_W"] > 0, "the column has no room left"


def test_a_gauge_can_sweep_to_its_reading(ui):
    """A reading arriving mid-sweep carries on from the drawn position."""
    import sys

    config = layout.validate({"animate": True, "pages": layout.DEFAULT_PAGES})
    assert config["animate"] is True
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["animate"] is False, (
        "off by default")

    assert ui.bindings.get("animate") == "animate", "the UI control sets something else"

    sys.path.insert(0, install.app_source_dir())
    import pages

    # The easing is picovector's, so only the endpoints of each sweep are checked.
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

        # A page turn drops the positions everything was drawn at.
        pages.sweep_reset()
        assert pages.fraction_of("cpu.pct", 20.0) == 0.2
        assert FakeTween.made[-1] == (0.2, 0.2)
    finally:
        pages.ANIMATE = was
        pages.sweep_reset()
        pages.__dict__.pop("tween", None)

def test_a_slide_is_a_style_the_ui_offers(ui):
    """`Slides`, `Rendering` and `DrawingElsewhere` in tests/badge/wasm/test_app.py drive
    the movement itself."""
    for style in layout.SLIDE_STYLES:
        assert layout.validate({"slide": style,
                                "pages": layout.DEFAULT_PAGES})["slide"] == style
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["slide"] == "off", (
        "immediate by default")
    assert layout.validate({"slide": "sideways",
                            "pages": layout.DEFAULT_PAGES})["slide"] == "off"
    assert layout.validate({"slide": True, "pages": layout.DEFAULT_PAGES})["slide"] == "over"
    assert layout.validate({"slide": False, "pages": layout.DEFAULT_PAGES})["slide"] == "off"
    assert 'id="slide"' in ui.markup, "no control in the UI"
    assert "config.slide" in ui.script, "the control is not bound"
    for style in layout.SLIDE_STYLES:
        assert f'value="{style}"' in ui.markup, style

def test_smooth_graphs_are_a_setting_that_reaches_the_badge(ui):
    """One setting smooths every graph on the badge."""
    config = layout.validate({"smooth": False, "pages": layout.DEFAULT_PAGES})
    assert config["smooth"] is False
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["smooth"] is True, "on by default"
    # Anything truthy, since the UI sends a checkbox and a command line sends a string.
    assert layout.validate({"smooth": "yes", "pages": layout.DEFAULT_PAGES})["smooth"] is True
    assert 'id="smooth"' in ui.markup, "no control in the UI"
    assert "config.smooth" in ui.script, "the control is not bound"


def test_the_big_gauge_can_show_the_whole_ramp(ui):
    """The gauge's gradient is the theme ramp, in order, round the arc it sweeps."""
    import sys

    for fill in layout.GAUGE_FILLS:
        assert layout.validate({"gauge_fill": fill,
                                "pages": layout.DEFAULT_PAGES})["gauge_fill"] == fill
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid", (
        "one colour by default")
    assert layout.validate({"gauge_fill": "rainbow",
                            "pages": layout.DEFAULT_PAGES})["gauge_fill"] == "solid"
    assert 'id="gaugefill"' in ui.markup, "no control in the UI"
    assert "config.gauge_fill" in ui.script, "the control is not bound"


    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    theme = look.get("dark")
    turn = (look.DIAL_TO - look.DIAL_FROM) / 360.0
    fill, track = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)
    assert fill.kind == badgefakes.Brush.CONICAL
    # Fractions of a whole turn, so a 270 degree gauge lays the ramp over three quarters.
    assert [pos for pos, _ in fill.stops] == [pos * turn for pos, _ in theme.ramp]
    assert [pen for _, pen in fill.stops] == [pen for _, pen in theme.ramp]
    # The track is the same ramp, dimmed by the colours themselves: a gradient brush ignores
    # screen.alpha.
    assert [pos for pos, _ in track.stops] == [pos for pos, _ in fill.stops]
    assert {pen.a for _, pen in track.stops} == {draw.TRACK_ALPHA}
    assert {pen.a for _, pen in fill.stops} == {255}

    # A field read backwards still ends at the reading's colour: a battery at 100% is a
    # machine doing well.
    backwards, _ = draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, True)
    positions = [pos for pos, _ in backwards.stops]
    assert positions == sorted(positions), positions
    assert backwards.stops[0][1] == theme.ramp[-1][1], "it does not start at the hot end"
    assert backwards.stops[-1][1] == theme.ramp[0][1]

    # Built once a theme: a pair from OKLCH stops is 3.4ms, moving the geometry is 12us.
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is fill
    draw.clear_cache()
    assert draw.swept_pens(theme, look.DIAL_C, look.DIAL_OUTER)[0] is not fill, (
        "a theme change would leave the old ramp round the gauge")

    # A solid fill is drawn with no brush at all.
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
    """A smoothed curve passes through every sample and stays in the range of the data."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw

    values = [0.2, 0.9, 0.3, 0.31, 0.8, 0.1, 0.5]
    dense = draw.curve(values, steps=4)
    assert len(dense) == (len(values) - 1) * 4 + 1, len(dense)
    for index, value in enumerate(values):
        assert abs(dense[index * 4] - value) < 1e-9, (index, dense[index * 4], value)
    # Overshoot is clamped, or an area fill runs under the baseline where a reading touched
    # zero.
    assert min(dense) >= min(values) and max(dense) <= max(values), (
        min(dense), max(dense))

    # Fewer than three points cannot be interpolated.
    assert draw.curve([0.5, 0.6], steps=4) == [0.5, 0.6]

    # `curve_steps` returns 1 for a plot drawn straight: switch off, too few samples, or a
    # plot too short for a curve to show.
    assert draw.curve_steps(250, 150, len(values)) > 1
    assert draw.curve_steps(250, 22, len(values)) == 1
    assert draw.curve_steps(250, 150, 2) == 1
    draw.SMOOTH = False
    try:
        assert draw.curve_steps(250, 150, len(values)) == 1
    finally:
        draw.SMOOTH = True

    # An axis with no full scale steps to round numbers rather than fitting the window, or
    # it rescales on every poll as samples arrive and leave. A byte rate steps in 1024s.
    assert draw.axis_top(900, "down_bps") == 1024
    assert draw.axis_top(6 * 1024 ** 2, "down_bps") == 10 * 1024 ** 2
    assert draw.axis_top(41943040, "down_bps") == 50 * 1024 ** 2
    assert draw.reading(draw.axis_top(41943040, "down_bps"), "down_bps") == "50.0MB/s"
    # Anything else steps in tens, so a temperature plot tops out at 100 and not 81.6.
    assert draw.axis_top(71.0, "temp") == 100
    assert draw.axis_top(30.0, "temp") == 50
    # It holds still while the busiest sample moves, which is the point.
    for peak in (6.1, 6.5, 7.0, 9.9):
        assert draw.axis_top(peak * 1024 ** 2, "down_bps") == 10 * 1024 ** 2, peak

    # A gap in a ring is a None, and comparing one against a float is a TypeError.
    # Every widget draws a gap at the axis, decided in one place.
    assert draw.flat([0.5, None, 0.25]) == [0.5, 0.0, 0.25]
    same = [0.5, 0.25]
    assert draw.flat(same) is same, "a series with no gaps is copied every frame"
    # The series as the ring hands it over, gaps and all.
    gappy = [0.5, None, 0.25, 0.9, None, None, 0.1, 0.4]
    assert draw._lay_out(60, 40, 250, 150, gappy, 1.0, None) > 0  # noqa: SLF001



def test_a_plot_is_placed_by_when_its_readings_were_taken(ui, badge_constants):
    """A plot walks by the host's spacing and the age of its newest point, not by an index."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import draw
    import pages

    # How many of the host's points one poll covers.
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

    # Animating a plot is a separate setting from sweeping a gauge.
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
    assert 'id="plotanim"' in ui.markup, "no control in the UI"
    assert 'bindCheck("plotanim", "plot_animation")' in ui.script, \
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

    # A series too short to walk is drawn where it stands: two samples put a whole plot
    # width in one step.
    for samples in range(2, draw.WALK_MIN):
        held = [50.0] * samples
        written = draw._lay_out(60, 40, 250, 150, held, 100.0, 0.75)
        assert abs(draw._points[0] - 60) < 0.01, (samples, draw._points[0])
        assert abs(draw._points[written - 2] - 310) < 0.01, (samples,
                                                             draw._points[written - 2])
    walked = draw._lay_out(60, 40, 250, 150, [50.0] * draw.WALK_MIN, 100.0, 0.75)
    assert draw._points[0] < 60 - 0.01, "a series long enough to walk is standing still"
    assert walked

    # A sparkline is drawn still whatever the setting says: 22px tall with a sample every
    # 5px, it has nowhere to scroll.
    assert pages.SCROLLS == ("graph", "trend"), pages.SCROLLS
    assert "spark" in pages.PLOTS, "it still wants a series fetched for it"
    # Two readings, which is all a field with no history has, must still draw.
    assert draw.line(0, 0, 470, 30, [5.0, 5.0], 47.0) is not None
    assert draw.line(0, 0, 470, 30, [5.0], 47.0) is None

    # A layout must not plot more refs than one poll can fetch.
    plotted = {ref for page in layout.DEFAULT_PAGES if page.get("kind") in pages.PLOTS
               for ref in page.get("fields", []) + [page.get("field")] if ref}
    assert badge_constants("app.py")["GRAPH_KEYS"] >= len(plotted), sorted(plotted)

    # A source that supplies its own history runs on a different clock - an hour, for a
    # domain's traffic - so the collector's spacing would slide it a year an hour.
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


def test_sparkline_rows_can_be_told_apart(ui):
    """Rows are banded by a step of lightness from the page, so six lines read as six rows."""
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
    assert 'id="rows"' in ui.markup, "no control in the UI"
    assert "config.rows" in ui.script, "the control is not bound"
    for style in layout.ROW_STYLES:
        assert f'value="{style}"' in ui.markup, style


    # A lift, not the panel colour: a panel can be a different hue as well as a different
    # level, which on a near-black page draws a stripe of colour.
    dark = look.THEMES["dark"]
    assert (dark.stripe.r - dark.bg.r == dark.stripe.g - dark.bg.g
            == dark.stripe.b - dark.bg.b == look.STRIPE), "the band shifts hue"

    # Toward the ink on a dark page and away from it on a pale one: lighten has nowhere to
    # go on a background that is already near white.
    pale = look.from_palette("light", themes.written()["light"])
    assert pale.pale and not dark.pale
    assert pale.stripe.r < pale.bg.r and dark.stripe.r > dark.bg.r

    # The axis rule under a plot is drawn only where the rows are otherwise unseparated.
    assert draw.ROWS == "zebra" and draw.ROW_NONE == "none"


def test_a_symbol_centres_on_the_words_beside_it():
    """An icon is placed by its ink and not by the baseline the words sit on."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    sys.path.insert(0, str(pathlib.Path("tools")))
    import draw
    import read_af

    # The placement holds only while an icon's ink is centred in a box sat on the baseline.
    # Read through the tool, so a font repacked wide is read as wide.
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
    # Lower than a shared baseline puts it.
    assert icon_y > text_y + text_size - icon_size


def test_the_shipped_fonts_are_packed_as_the_metrics_assume(badge_constants):
    """draw.CAP and draw.ICON_BOX hold only while the fonts keep the proportions they
    were measured from."""
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
    # glyph puts them. The pair sits low and is not symmetric about the digits, so the
    # numbers are measured and not chosen.
    clockface = badge_constants("extensions/statsbadge-clock/src/statsbadge_clock/badge"
                                "/clockface.py")
    at, radius = clockface["COLON_AT"], clockface["COLON_DOT"]

    colon = next(g for g in lcd["glyphs"] if g["codepoint"] == ord(":"))
    up = [-y for y in colon["points"][1::2]]      # points run down from the baseline
    span = eight["bbox_h"]
    halfway = (min(up) + max(up)) / 2.0
    for drawn, dot in zip(at, ([y for y in up if y > halfway],
                               [y for y in up if y < halfway]), strict=True):
        centre = (min(dot) + max(dot)) / 2.0
        assert abs((span - centre) / span - drawn) < 0.005, (drawn, centre, span)
        assert abs((max(dot) - min(dot)) / 2.0 / span - radius) < 0.005, dot

    # The digital face draws the app's face at a finer grid, so it has to agree on the cap
    # it is sized from and the width it is placed by.
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


def test_every_clock_face_the_ui_offers_has_a_renderer(badge_constants):
    """The face list is host side and the tables are badge side, and the badge cannot be
    imported here.

    `ClockFaces` in tests/badge/wasm/test_draw.py draws each of them, and checks no two
    faces draw the same thing.
    """
    badge = pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge")
    Clock = pytest.importorskip("statsbadge_clock").Clock

    offered = next(s for s in Clock.page_settings if s["key"] == "face")["options"]
    # The dials and the dial-less faces are two tables; between them they are the renderers.
    tables = badge_constants(str(badge / "clockface.py"))
    drawn = set(tables["FACES"]) | set(tables["DIGITAL"])
    assert set(offered) == drawn, (sorted(offered), sorted(drawn))

    # The seven-segment face needs a font, and an asset travels only if it is declared.
    assert any(path.endswith("lcd.af") for path in Clock.badge_assets), Clock.badge_assets
    assert (badge / "lcd.af").exists(), "the LCD face's font is not built"
    # Shipped, so its licence ships with it.
    licence = pathlib.Path("licences/OFL-DSEG.txt").read_text(encoding="utf-8")
    assert "keshikan" in licence and "SIL Open Font License" in licence


def test_a_notifications_page_sorts_messages_from_counters():
    """One slot list holds both: a message is a dict carrying `text`, a counter is a
    number."""
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

        # The slot list need not put the messages first
        out = render(["feed.followers", "feed.home"])
        assert len(out["items"]) == 1 and len(out["counters"]) == 1, out

        # A page of only one or the other still draws
        assert render(["feed.home"])["counters"] == []
        assert render(["feed.followers"])["items"] == []
        # A field the host stopped producing draws a counter of "--"
        assert render(["feed.gone"])["counters"] == [("GONE", "--")], render(["feed.gone"])
    finally:
        draw.notification = was

    # Minutes up to ninety, then hours, then days.
    assert [draw.ago(s) for s in (None, 5, 90, 4000, 100000, 400000)] == [
        None, "just now", "1m ago", "66m ago", "27h ago", "4d ago"]


def rules_of(css):
    """Every rule in the sheet, as its full selector and the declarations under it.

    The sheet nests, so a rule's selector is the chain of the ones it sits inside."""
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
    """No rule giving a row a display outranks the browser's one selector for `hidden`."""
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
    assert len({depth for _tag, depth, _named in parser.found}) > 1, parser.found

    # The two selectors are otherwise close enough for source order to settle which wins.
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
    """A picture too tall for its block is cropped, never scaled."""
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
    # The pixels are palette indices, so halfway between two of them is a third colour.
    whole = Picture(128, 96)
    assert draw.fitted(whole, 96) is whole and whole.taken is None
    assert draw.fitted(whole, 200) is whole and whole.taken is None

    # Two messages to a page: 78px of block, less its padding
    tall = Picture(128, 96)
    band = draw.fitted(tall, 70)
    assert (band.width, band.height) == (128, 70), (band.width, band.height)
    # From the middle, where the crop that made the picture put what matters
    assert (tall.taken.x, tall.taken.y) == (0, 13), (tall.taken.x, tall.taken.y)

    # Below a band worth looking at, none
    assert draw.fitted(Picture(128, 96), 12) is None
    assert draw.fitted(None, 70) is None
