"""Palettes: how a theme travels, is derived, and is drawn with."""

import builtins
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request

import badgefakes

from statsbadge import install, layout, themes


def test_every_cache_that_holds_a_colour_is_dropped_on_a_theme_change():
    """A cache added to draw.py and left out of clear_cache survives a theme switch.

    It shows as one widget in the palette before last, which looks like a rendering
    fault and is a missing line. Registering at the point of definition is what makes
    leaving one out hard.
    """
    import ast

    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    # Registered, or named here as holding no colour and why.
    exempt = {"_fonts", "_weights", "_CLEARS"}
    loose = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if not name.startswith("_") or name in exempt:
                continue
            if isinstance(node.value, (ast.Dict, ast.Set)) or (
                    isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) in ("dict", "set")):
                loose.append(name)
    assert not loose, f"caches in draw.py outside _CLEARS: {loose}"

    body = source[source.index("def clear_cache"):]
    body = body[:body.index("\ndef ", 1)]
    assert "for empty in _CLEARS" in body, "clear_cache is enumerating by hand again"

    # The waterfall's scroll buffer and worldmap's pens are state, not one container.
    assert "@clears\ndef waterfall_reset" in source
    world = (pathlib.Path(install.app_source_dir()) / "worldmap.py").read_text(encoding="utf-8")
    assert "@draw.clears\ndef forget" in world, "the map keeps the old theme's pens"


def test_the_theme_the_badge_boots_with_is_the_host_s_dark():
    """look.THEMES holds one palette, for the frames before the first layout arrives.

    The same colours are in themes.PALETTES, and MicroPython cannot import that, so the
    badge carries a copy. Drifting shows as the page changing colour a second after
    launch, which reads as a rendering fault and is a palette edit made once.
    """
    import ast

    source = (pathlib.Path(install.app_source_dir()) / "look.py").read_text(encoding="utf-8")
    call = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Theme":
            call = node
            break
    assert call is not None, "no Theme( in look.py"

    carried = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    assert ast.literal_eval(call.args[0]) == themes.DEFAULT, "a theme other than the default"
    wanted = themes.written()[themes.DEFAULT]
    assert carried == wanted, (
        f"the badge boots a different {themes.DEFAULT}: "
        f"{ {k: v for k, v in carried.items() if wanted.get(k) != v} }")


def test_a_theme_travels_as_its_colours():
    """A theme is a table of colours, so it is config: the badge is sent the palette as
    well as the name, and an unfamiliar one draws as well as one it ships with."""

    from statsbadge import themes

    sys.path.insert(0, install.app_source_dir())
    import look

    # Every palette is complete, ordered and usable by the badge's builder.
    assert layout.DEFAULT_CONFIG["theme"] == themes.DEFAULT
    for name, palette in themes.written().items():
        assert name in layout.THEMES, f"{name} is not offered"
        built = look.from_palette(name, palette)
        assert built is not None, f"the badge cannot build {name}"
        assert built.name == name, built.name
        stops = built.ramp
        assert stops[0][0] == 0.0 and stops[-1][0] == 1.0, name
        assert [pos for pos, _pen in stops] == sorted(pos for pos, _pen in stops), name
        # A theme is built out of palette data and holds `color` objects, so a pen takes
        # what it is handed, every colour arriving ready to draw with.
        for fraction in (0.0, 0.5, 1.0):
            assert isinstance(built.at(fraction), builtins.color), (name, fraction)
        assert built.at(0.0) == stops[0][1] and built.at(1.0) == stops[-1][1], name

    # The one the app carries to boot with agrees with the host's copy of it, or the
    # first frame is drawn in colours the config left behind.
    assert list(look.THEMES) == [themes.DEFAULT], list(look.THEMES)
    booted, sent = look.THEMES[themes.DEFAULT], themes.written()[themes.DEFAULT]
    for key in ("bg", "panel", "ink", "dim", "accent", "grid"):
        assert getattr(booted, key) == builtins.color.rgb(*sent[key]), key
    assert booted.ramp == tuple((pos, builtins.color.rgb(*rgb))
                                for pos, rgb in sent["ramp"])

    # The colours are on the payload the badge fetches, keyed to the theme it chose.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({"theme": "eva01", "pages": layout.DEFAULT_PAGES})
    sent = config.for_badge()
    assert sent["theme"] == "eva01"
    stored = themes.written()["eva01"]
    assert {key: sent["palette"][key] for key in stored} == stored, sent["palette"]
    assert look.from_palette(sent["theme"], sent["palette"]).accent == \
        builtins.color.rgb(143, 212, 0)

    # Plus the greys a picture is drawn in, derived from the accent's hue as `stripe` is.
    #
    # Their lightnesses are fixed and the same on every theme, which is the guarantee. The
    # host dithers a photograph to a position on this ramp with no say in which theme draws
    # it, so index 2 of four has to mean the same brightness everywhere.
    from statsbadge import derive

    wanted = None
    for name in layout.THEMES:
        greys = layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])["image"]
        assert sorted(greys) == ["4", "8"], sorted(greys)
        for count, ramp in greys.items():
            assert len(ramp) == int(count), (name, count)
        lightnesses = [derive.oklch(tuple(rgb))[0] for rgb in greys["4"]]
        assert lightnesses == sorted(lightnesses), (name, lightnesses)
        if wanted is None:
            wanted = lightnesses
        # Within a rounding of each other. The drift is where a colour lands on whole
        # bytes: at a saturated theme's chroma a channel step is worth more lightness.
        #
        # Measured across every theme and both level counts, at most 0.013.
        adrift = max(abs(one - other) for one, other in zip(lightnesses, wanted, strict=True))
        assert adrift <= 0.015, f"{name} draws a picture {adrift:.3f} off the levels"

    # How colourful it is comes off the theme: the same share of what the hue can hold
    # that the accent takes of its. A grey accent gives a grey picture, which is the
    # convention `mono` exists for, where a fixed tint gave it a coloured one.
    for name, coloured in (("mono", False), ("luminescence", True), ("eva01", True)):
        shades = layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])["image"]["8"]
        chroma = max(derive.oklch(tuple(rgb))[1] for rgb in shades)
        assert (chroma > 0.05) is coloured, f"{name} midtone chroma {chroma:.3f}"
    # The badge builds them keyed by how many, matching an indexed image's
    # table length says.
    built = look.from_palette("eva01", sent["palette"])
    assert sorted(built.image) == [4, 8], sorted(built.image)
    assert all(isinstance(pen, builtins.color) for pen in built.image[4])
    # A host too old to send them leaves a theme that draws no pictures, not one that fails
    assert look.from_palette("old", {k: v for k, v in sent["palette"].items()
                                     if k != "image"}).image == {}

    # Nonsense off the network is refused before it is drawn: a bad palette would otherwise
    # be a crash on every frame where a page in the boot theme is wanted.
    for bad in (None, {}, {"bg": "red"}, {"bg": (1, 2, 3), "ramp": ()}):
        assert look.from_palette("bad", bad) is None, bad


def test_a_palette_can_carry_a_second_accent(h, ui):
    """One more colour, used sparingly, a graph's second series being all of it. It is
    where the app used to hunt through the ramp for something that would show. A palette that
    names none gets the accent again, matching every palette without one."""

    from statsbadge import derive, themes

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    for rule in layout.ACCENT_B_RULES:
        assert layout.validate({"accent_b": rule,
                                "pages": layout.DEFAULT_PAGES})["accent_b"] == rule
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["accent_b"] == "same"
    assert layout.validate({"accent_b": "clashing",
                            "pages": layout.DEFAULT_PAGES})["accent_b"] == "same"

    # Each rule keeps the accent's family: the same lightness and the same share of what
    # its hue can hold - so the two look like one palette's two colours.
    accent = derive.accents("normal")[6]
    assert tuple(derive.second_accent(accent, "same")) == tuple(accent)
    lightness, chroma, hue = derive.oklch(accent)
    for rule in ("complementary", "triadic", "contrasting"):
        other = derive.second_accent(accent, rule)
        second = derive.oklch(other)
        assert abs(second[0] - lightness) < 0.03, (rule, second[0], lightness)
        assert derive.apart(accent, other) > 10.0, (rule, derive.apart(accent, other))
    # Complementary is the wheel's opposite; contrasting is whichever offered hue lands
    # furthest away once lightness and chroma are counted, which is the maximum.
    opposite = derive.second_accent(accent, "complementary")
    furthest = derive.second_accent(accent, "contrasting")
    assert derive.apart(accent, furthest) >= derive.apart(accent, opposite)
    turn = derive.oklch(opposite)[2] - hue
    assert abs((turn - 180.0 + 180.0) % 360.0 - 180.0) < 2.0, turn

    # It reaches the badge in the palette, where a second series takes it.
    palette = layout.palette_for("tinted-dark", accent, "contrasting")
    theme = look.from_palette("tinted", palette)
    assert theme is not None
    assert theme.accent_b == badgefakes.Colour.rgb(*palette["accent_b"])
    assert draw._series_colour(theme, 1) == theme.accent_b
    assert draw._series_colour(theme, 0) == theme.accent
    # A palette with none: the accent again, and the ramp still answers for the second series.
    plain = look.from_palette("dark", themes.written()["dark"])
    assert plain.accent_b == plain.accent
    assert draw._series_colour(plain, 1) != plain.accent

    # The one written-down palette that needed it: a page that pink shows its green nowhere
    # else, the ramp's cold end being a reading that goes unused.
    melon = look.from_palette("watermelon-light", themes.written()["watermelon-light"])
    assert melon.accent_b != melon.accent
    written = themes.written()["watermelon-light"]
    assert derive.apart(written["accent_b"], written["accent"]) > 20.0

    # Where it shows: the chrome takes it, so the first accent is left for what a reading is
    # drawn in. A palette with none has the two the same colour, and it holds.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    header = source[source.index("def furniture("):]
    header = header[:header.index("\ndef ", 1)]
    assert "screen.pen = theme.accent_b" in header, "the header rule is not the second accent"
    pips = source[source.index("def _pips("):]
    pips = pips[:pips.index("\ndef ", 1)]
    assert "theme.accent_b if i == index" in pips, "the current pip is not the second accent"

    # Not one of the bound controls: a second accent is picked per theme, so the page
    # carries the select and the script writes the setting where it renders the tint.
    assert "accentb" in ui.ids, "no control in the UI"
    assert "config.accent_b" in ui.script, "the control is not bound"
    status, shown = h.raw("GET", "/api/theme?theme=tinted-dark&second=triadic")
    assert status == 200 and shown["palette"]["accent_b"] != shown["palette"]["accent"]
    status, _bad = h.raw("GET", "/api/theme?theme=tinted-dark&second=nonesuch")
    assert status == 400, status


def test_the_single_hue_themes_are_the_bold_variant_now():
    """Red, green, cyan, amber and blueprint were five palettes doing one thing.

    Everything in one hue, the accent as saturated as sRGB allows, and a ramp sweeping
    lightness inside that hue while holding off red.

    Measured, each sat within 0.003 of its hue's chroma limit, and `red` differed from the
    derived palette at the same hue by 8 counts in the accent alone. So they are the bold
    variant with an accent, and the names still resolve."""
    from statsbadge import derive, themes

    for retired in themes.ALIASES:
        assert retired not in themes.written(), f"{retired} is still a written-down palette"
        name, accent = layout.resolve_theme(retired, None)
        assert themes.THEMES[name]["derived"].get("bold"), (retired, name)
        assert tuple(accent) in derive.offered(), (retired, accent)

    # A stored name keeps drawing: resolved once when the file is read, so nothing
    # downstream has to know it ever existed.
    path = os.path.join(tempfile.mkdtemp(prefix="statsbadge-alias-"), "layout.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"rev": 3, "theme": "amber", "pages": layout.DEFAULT_PAGES,
                   "badges": {"badgeone": {"rev": 4, "theme": "cyan",
                                           "pages": layout.DEFAULT_PAGES}}}, handle)
    stored = layout.Config(path)
    assert stored.layout_for()["theme"] == "tinted-bold-dark"
    assert stored.layout_for("badgeone")["theme"] == "tinted-bold-dark"
    # Each brings the colour it named, not whatever tint was stored beside it.
    amber = derive.oklch(stored.layout_for()["tint"])[2]
    cyan = derive.oklch(stored.layout_for("badgeone")["tint"])[2]
    assert abs(amber - 60.0) < 1.0 and abs(cyan - 210.0) < 1.0, (amber, cyan)
    assert stored.for_badge()["palette"]["ramp"][-1][1] != list(
        themes.written()["dark"]["ramp"][-1][1])
    # A PUT carrying an old name is taken as well, an open browser being older than the host.
    assert layout.validate({"theme": "red", "pages": layout.DEFAULT_PAGES})["theme"] == (
        "tinted-bold-dark")
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # The saturated family covers the retired ones: at the limit, and different per hue.
    lightness = derive.ACCENT_FAMILIES["saturated"][0]
    for accent in derive.accents("saturated"):
        _l, chroma, hue = derive.oklch(accent)
        assert chroma >= derive.max_chroma(lightness, hue) * 0.9, (hue, chroma)
    spread = [round(derive.oklch(a)[1], 3) for a in derive.accents("saturated")]
    assert max(spread) - min(spread) > 0.1, spread
    # A bold ramp stays in the accent's hue, where the even variant's travels to red.
    for accent in derive.accents("saturated"):
        ramp = derive.palette(accent, "dark", True)["ramp"]
        hues = [derive.oklch(rgb)[2] for _pos, rgb in ramp]
        span = max(abs((hue - hues[0] + 180.0) % 360.0 - 180.0) for hue in hues)
        assert span < 20.0, (accent, hues)


def test_a_theme_with_a_counterpart_has_one_in_the_other_mode():
    """Four of the hand-written themes come as a pair, one for a lit room and one for a dark
    one. Each is placed against the background it goes on and measured here; inverting
    channel by channel lands ink on a white page at the wrong lightness."""
    from statsbadge import derive, themes

    modes = {record["name"]: record["mode"] for record in layout.theme_records()}
    for dark, light in (("mono", "mono-light"), ("watermelon", "watermelon-light"),
                        ("shell", "shell-light"), ("luminescence-dark", "luminescence")):
        assert modes[dark] == "dark" and modes[light] == "light", (dark, light)

    # Every palette clears the bar the shipped ones set: AAA for ink, since it
    # what a reading is drawn in, and a hot end that shows against the page at all.
    for name, palette in themes.written().items():
        ink = derive.contrast(palette["ink"], palette["bg"])
        dim = derive.contrast(palette["dim"], palette["bg"])
        hot = derive.contrast(palette["ramp"][-1][1], palette["bg"])
        assert ink >= 7.0, (name, ink)
        assert dim >= 2.5, (name, dim)
        assert hot >= 1.9, (name, hot)
        cold = palette["ramp"][0][1]
        apart = sum((a - b) ** 2 for a, b in zip(cold, palette["ramp"][-1][1], strict=True))
        assert apart > 1600, (name, apart)


def test_a_re_tinted_theme_is_a_different_theme_to_a_cache():
    """A derived theme keeps its name when it is built again from another accent, so
    anything baked under the name went on being drawn in the colours it was baked in.

    The clock's face was the visible one: re-tinting left the old dial behind.
    """

    from statsbadge import derive

    sys.path.insert(0, install.app_source_dir())
    import look

    accents = derive.accents("saturated")
    one = layout.palette_for("tinted-dark", accents[0])
    other = layout.palette_for("tinted-dark", accents[6])
    first = look.from_palette("tinted-dark", one)
    second = look.from_palette("tinted-dark", other)

    assert first.name == second.name, "the names were expected to match"
    assert first.key != second.key, "a cache cannot tell the two apart"
    # The same accent twice is one key, or every poll would throw the caches away.
    assert first.key == look.from_palette("tinted-dark", one).key

    # Every cache that holds something baked in a theme's colours keys on that.
    app = pathlib.Path(install.app_source_dir())
    clock = pathlib.Path("extensions/statsbadge-clock/src/statsbadge_clock/badge/clockface.py")
    for source in [app / "draw.py", app / "worldmap.py", app / "__init__.py", clock]:
        body = source.read_text(encoding="utf-8")
        assert "theme.name" not in body, f"{source.name} still keys a cache on the name"


def _palette_of(name):
    """A theme's palette as it reaches the badge, for a from_palette check."""
    return layout.palette_for(name, layout.DEFAULT_CONFIG["tint"])


def test_the_case_lights_follow_the_backlight():
    """A case light is one brightness and not a colour, so a theme leaves it alone.

    They track the panel instead. A dark room otherwise dims the screen and leaves four
    lights burning at the level a palette happened to name."""
    import ast

    sys.path.insert(0, install.app_source_dir())
    import look

    # The palette, the theme and the wire have all dropped it.
    assert not hasattr(look.THEMES[look.DEFAULT], "case")
    assert look.from_palette("d", {**_palette_of("dark"), "case": 0.9}).__dict__.get("case") is None

    source = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies = {node.name: node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef)}

    lights = ast.dump(bodies["apply_caselights"])
    assert "wanted_brightness" in lights, "the case lights do not follow the backlight"
    # The attribute and not the word; the docstring still uses it.
    assert "attr='case'" not in lights, "the case lights still read the theme"
    assert "CASELIGHT_FLOOR" in lights, "a followed reading lost its floor"
    # Reapplied wherever the brightness moves, and not only when a layout lands: a button
    # press would otherwise dim the panel alone.
    assert "apply_caselights" in ast.dump(bodies["apply_backlight"])


def test_the_themes_are_a_data_file():
    """A theme is data, so it lives in a data file, where every entry is the same shape.

    A special case was what put a second half-description of it in layout.py before."""
    from importlib import resources

    from statsbadge import derive, themes

    raw = tomllib.loads(resources.files("statsbadge").joinpath("themes.toml").read_text(encoding="utf-8"))
    raw.pop("aliases")
    assert set(raw) == set(themes.THEMES) and len(raw) == 22, len(raw)

    for name, record in raw.items():
        spec = record.get("derived")
        named = [role for role in themes.ROLES if role in record]
        assert bool(spec) != bool(named), f"{name} is both written down and derived"
        if spec:
            assert spec["shape"] in derive.SHAPES, (name, spec)
            continue
        assert set(named) == set(themes.ROLES), f"{name} is missing {set(themes.ROLES) - set(named)}"
        for role in named + (["accent_b"] if "accent_b" in record else []):
            channels = record[role]
            assert len(channels) == 3 and all(0 <= v <= 255 for v in channels), (name, role)
        at = [position for position, _rgb in record["ramp"]]
        assert at[0] == 0.0 and at[-1] == 1.0 and at == sorted(at), (name, at)

    # The retired names still resolve, so a badge showing one carries on showing it.
    for retired, aliased in themes.ALIASES.items():
        assert aliased["theme"] in themes.THEMES, (retired, aliased)

    # A case light is a brightness and not a colour, so no palette carries one.
    assert not any("case" in record for record in raw.values())
    palette = layout.palette_for("tinted-dark", layout.DEFAULT_CONFIG["tint"])
    assert "case" not in palette


def test_a_lit_theme_is_one_hue_throughout():
    """Glow is Tinted Bold with a lit page: monochromatic, soft, a vintage screen.

    Which means it has to take the bold ramp. A ramp that travels to red is not monochrome
    whatever the furniture does, and that is the whole flavour rather than a detail.
    """
    from statsbadge import derive, themes

    def wander(palette):
        """The furthest any colour sits from the rest in hue, ignoring the near-greys."""
        hues = []
        for role in ("bg", "panel", "grid", "dim", "ink", "accent"):
            lightness, chroma, hue = derive.oklch(palette[role])
            if chroma > 0.01:
                hues.append(hue)
        for _at, colour in palette["ramp"]:
            lightness, chroma, hue = derive.oklch(colour)
            if chroma > 0.01:
                hues.append(hue)
        return max(abs((hue - hues[0] + 180.0) % 360.0 - 180.0) for hue in hues)

    for name in ("tinted-glow-dark", "tinted-glow-light"):
        assert themes.THEMES[name]["derived"].get("bold"), f"{name} would travel to red"

    worst = {}
    for name, record in themes.THEMES.items():
        spec = record.get("derived")
        if not spec:
            continue
        for family in derive.ACCENT_FAMILIES:
            for accent in derive.accents(family):
                moved = wander(layout.palette_for(name, accent))
                worst[name] = max(worst.get(name, 0.0), moved)
    # Measured: a signal ramp takes a palette most of the way round the wheel.
    for name in ("tinted-glow-dark", "tinted-glow-light", "tinted-bold-dark"):
        assert worst[name] < 20.0, (name, worst[name])
    assert worst["tinted-dark"] > 100.0, worst["tinted-dark"]
    # The hand-tuned originals it was shaped from, for scale.
    for name in ("luminescence", "luminescence-dark"):
        assert wander(themes.written()[name]) < 20.0, name

    # The other half is the page. A lit one carries more of the hue at less contrast,
    # and both move together or readable_on walks the ink back out.
    for lit, plain in (("glow-dark", "dark"), ("glow-light", "light")):
        assert derive.SHAPES[lit]["ink_ratio"] < derive.INK_RATIO
        for family in derive.ACCENT_FAMILIES:
            for accent in derive.accents(family):
                shaped = derive.palette(accent, lit, bold=True)
                flat = derive.palette(accent, plain)
                assert (derive.contrast(shaped["ink"], shaped["bg"])
                        < derive.contrast(flat["ink"], flat["bg"])), (lit, accent)
                assert (derive.oklch(shaped["bg"])[1] > derive.oklch(flat["bg"])[1]), (lit, accent)


def test_a_graph_s_two_series_read_apart():
    """The second series is the badge's choice, worked out on the host for the preview.

    Whichever end of the ramp sits furthest from the accent and can still be seen, with
    `dim` last. Measured, no theme lands on that last resort."""

    from statsbadge import derive, themes

    sys.path.insert(0, install.app_source_dir())
    import draw
    import look

    assert layout.SERIES_FLOOR == draw.SERIES_FLOOR
    assert layout.SERIES_ALPHA == draw.SERIES_ALPHA
    assert layout.PALE_SUM == look.PALE_SUM

    for name in themes.THEMES:
        for accent in (derive.accents()[6], derive.accents("saturated")[0]):
            palette = layout.palette_for(name, accent)
            first, second = layout.series_colours(palette)
            assert tuple(first) == tuple(palette["accent"])
            # The candidates are the badge's. Which one it lands on is not asserted
            # against `draw._series_colour`: that runs on FakeColour, whose `difference`
            # stands in for the firmware's with sRGB distance, and the two part company
            # on shell-light.
            theme = look.from_palette(name, palette)
            assert draw._series_colour(theme, 0) == theme.accent
            # `dim` is the last resort. Membership and not colour: mono's dim and its
            # cold end are the same grey.
            chosen = {tuple(palette["ramp"][0][1]), tuple(palette["ramp"][-1][1])}
            if "accent_b" in palette:
                chosen.add(tuple(palette["accent_b"]))
            assert tuple(second) in chosen, f"{name} fell back to dim"
            # Seen against the page, which the floor is there to keep true.
            alpha = layout.SERIES_ALPHA[0 if sum(palette["bg"]) >= layout.PALE_SUM else 1]
            shown = tuple(round(pen * alpha / 255.0 + bg * (1 - alpha / 255.0))
                          for pen, bg in zip(second, palette["bg"], strict=True))
            assert derive.apart(palette["bg"], shown) >= layout.SERIES_FLOOR, (name, second)


def test_the_preview_reads_a_number_as_the_badge_does(ui):
    """The preview formats and scales readings itself, pages.py importing draw and draw
    expecting the firmware's globals, so the host cannot answer for it.

    The tables are the half that drifts, so they are compared entry by entry, parsed out of
    the source: this job has no node to run it with.
    """
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages as pages_module

    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")

    def table(name):
        body = script.split(f"const {name} = {{")[1].split("}")[0]
        return {key.strip('"'): value.strip().strip('"')
                for key, value in re.findall(r'([\w".]+):\s*([^,\n]+)', body)}

    # A subset, since the preview draws four pages: every entry it carries has to agree,
    # and every ref it draws has to be one of them.
    shown = table("NAMES")
    assert shown, "the preview carries no names"
    for ref, name in shown.items():
        assert pages_module.NAMES[ref] == name, (ref, name, pages_module.NAMES.get(ref))
    for group in ("cpu.pct", "cpu.temp", "net.down_bps", "disk.pct", "disk.used_mb"):
        assert group in shown, group
    assert set(re.findall(r'"([\w.]+)"', script.split("const PERCENT_FIELDS = [")[1]
                          .split("]")[0])) == set(pages_module.PERCENT)
    assert set(re.findall(r'"(_\w+)"', script.split("const UNIT_SUFFIXES = [")[1]
                          .split("]")[0])) == set(pages_module.UNIT_SUFFIXES)

    scale = {key: float(value.replace("e6", "e6"))
             for key, value in table("SCALE").items()}
    assert scale == pages_module.SCALE

    # The sizes a page is laid out to are look.py's too.
    import look

    for name, value in (("HEADER_H", look.HEADER_H), ("FOOTER_H", look.FOOTER_H),
                        ("PAD", look.PAD), ("SIZE_TITLE", look.SIZE_TITLE),
                        ("SIZE_HUGE", look.SIZE_HUGE), ("DIAL_OUTER", look.DIAL_OUTER),
                        ("DIAL_INNER", look.DIAL_INNER), ("DIAL_FROM", int(look.DIAL_FROM)),
                        ("DIAL_TO", int(look.DIAL_TO))):
        assert ui.constants.get(name) == value, (name, value, ui.constants.get(name))


def test_the_preview_draws_in_the_badge_s_own_faces():
    """Four pages at 320x240, in Lexend and the badge's symbols. The icon font is subset
    from the corpus badge_app/icons.af is built from, so the two cannot differ."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages as pages_module

    web = pathlib.Path("src/statsbadge/web")
    for name in ("lexend-var.ttf", "icons.woff2"):
        assert (web / name).is_file(), f"{name} is not shipped with the UI"
    sheet = (web / "app.css").read_text(encoding="utf-8")
    assert "@font-face" in sheet and "lexend-var.ttf" in sheet and "icons.woff2" in sheet

    corpus = pathlib.Path("ci/badge-icons.txt").read_text(encoding="utf-8")
    rows = [m.groups() for m in
            (re.match(r"^(\w+)\s+([0-9a-f]{4})\s+(\S)\s*$", line)
             for line in corpus.splitlines()) if m]
    assert len(rows) == 18, len(rows)

    # The JS addresses a symbol by the same letter badge-side code does.
    script = (web / "app.js").read_text(encoding="utf-8")
    shown = dict(re.findall(r"(\w): 0x([0-9a-f]{4})", script.split("const ICONS = {")[1]
                            .split("}")[0]))
    assert shown == {letter: point for _name, point, letter in rows}, shown
    drawn = set(pages_module.GROUP_ICONS.values()) | set(pages_module.FIELD_ICONS.values())
    assert drawn <= set(shown), drawn - set(shown)


def test_the_dark_theme_s_colours_are_not_copied_by_hand(h):
    """The UI's accent, its ramp and the mark on its tab all come from the dark theme.

    Generated or checked. A copy typed in goes stale the moment a palette moves."""
    from statsbadge import themes

    dark = themes.written()[themes.DEFAULT]
    def hexed(colour):
        red, green, blue = colour
        return f"#{red:02x}{green:02x}{blue:02x}"

    # Served generated, so the sheet holds no stale copy. Fetched rather than rebuilt,
    # which would check a second copy of the generator instead.
    with urllib.request.urlopen(h.url("/tokens.css"), timeout=5) as response:
        tokens = response.read().decode()
        assert response.headers["Content-Type"].startswith("text/css")
    assert f"--accent: {hexed(dark['accent'])};" in tokens
    for at, colour in enumerate(rgb for _pos, rgb in dark["ramp"]):
        assert f"--ramp-{at}: {hexed(colour)};" in tokens, (at, colour)

    # The splash mark is the badge's, so the tab icon is too. Two copies of it, with
    # icon.svg's comment asking for a test rather than a build step.
    marks = [pathlib.Path("src/statsbadge/web/icon.svg").read_text(encoding="utf-8"),
             pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")]
    for role in ("bg", "grid", "accent", "ink"):
        for mark in marks:
            assert hexed(dark[role]) in mark.lower(), (role, hexed(dark[role]))


def test_the_themes_are_offered_light_and_dark(h):
    """Which of them suits a lit room is the first thing anybody chooses between, so the picker
    groups them by that, read off each palette's background, since a theme that had to
    declare its mode could declare it wrong."""
    records = {record["name"]: record for record in layout.theme_records()}
    assert set(records) == set(layout.THEMES)
    for name, mode in (("dark", "dark"), ("light", "light"), ("frost", "light"),
                       ("sakura", "light"), ("luminescence", "light"), ("shell", "dark"),
                       ("mono", "dark"), ("tinted-light", "light")):
        assert records[name]["mode"] == mode, (name, records[name])
    # The two that came first are named for what they are.
    assert records["dark"]["label"] == "Default Dark"
    assert records["light"]["label"] == "Default Light"
    # Every theme arrives named: the UI holds no rule for turning a slug into a title.
    assert records["sakura"]["label"] == "Sakura"
    assert records["mono-light"]["label"] == "Mono Light"
    assert all(record["label"] for record in records.values()), "a theme arrived unnamed"

    _status, caps = h.raw("GET", "/api/capabilities")
    assert {record["name"] for record in caps["themes"]} == set(layout.THEMES)
    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    assert "optgroup" in script, "the picker is still one flat list"
    assert "record.label" in script
    assert "titleCase(record.name)" not in script, "the UI still titles a theme itself"


def themes_bg(name):
    from statsbadge import themes
    return themes.written()[name]["bg"]


def test_a_theme_can_be_derived_from_one_accent(h):
    """The tinted pair are derived: a whole palette from the one accent chosen, so the
    choice is stored and not its result. Restricted to a measured list, every accent on
    offer being checked here, so a pickable one cannot make a page nobody can read."""

    from statsbadge import derive

    sys.path.insert(0, install.app_source_dir())
    import look

    derived = {name for name, record in themes.THEMES.items() if "derived" in record}
    assert derived <= set(layout.THEMES)
    assert {themes.THEMES[name]["derived"]["shape"] for name in derived} == set(derive.SHAPES)
    assert len(derive.accents()) == len(derive.ACCENT_HUES) == 12
    assert len(derive.ACCENT_FAMILIES) == 4

    # Every accent of every family, in both modes and both variants. The ramp follows:
    # the even variant travels to red where the accent has somewhere to travel and stays in its
    # accent's hue where it has not, and the bold one always stays in it.
    checked = 0
    for theme in sorted(derived):
      for family in derive.ACCENT_FAMILIES:
        for accent in derive.accents(family):
            palette = layout.palette_for(theme, accent)
            shape = derive.SHAPES[themes.THEMES[theme]["derived"]["shape"]]
            assert (derive.contrast(palette["ink"], palette["bg"])
                    >= shape.get("ink_ratio", derive.INK_RATIO)), (theme, accent)
            assert (derive.contrast(palette["dim"], palette["bg"])
                    >= shape.get("dim_ratio", derive.DIM_RATIO)), (theme, accent)
            # The hot end has to be seen against the page, or a gauge goes blank when it
            # matters most - which is the fault the shipped `cyan` ramp has.
            assert derive.contrast(palette["ramp"][-1][1], palette["bg"]) >= 1.9
            cold, hot = palette["ramp"][0][1], palette["ramp"][-1][1]
            apart = sum((a - b) ** 2 for a, b in zip(cold, hot, strict=True))
            assert apart > 1600, (theme, accent, apart)
            # The badge can build it, as the app does.
            assert look.from_palette("tinted", palette) is not None
            checked += 1
    # Six derived themes, four families, twelve accents.
    assert checked == 288, checked

    reds = [a for a in derive.accents() if derive.ramp_for(a) == "mono"]
    assert reds, "every accent claims it can travel to red"
    assert derive.ramp_for(derive.accents()[6]) == "signal"

    # An unrecognised accent falls back, and the request stands: this arrives from a UI,
    # and a theme is too small a thing to refuse a whole config over.
    kept = layout.validate({"theme": "tinted-dark", "tint": [7, 7, 7],
                            "pages": layout.DEFAULT_PAGES})
    assert tuple(kept["tint"]) in derive.offered()

    # A palette like any other travels, and the badge cannot tell it was derived.
    config = layout.Config(os.path.join(tempfile.mkdtemp(), "layout.json"))
    config.replace({"theme": "tinted-light", "pages": layout.DEFAULT_PAGES,
                    "tint": list(derive.accents("saturated")[8])})
    sent = config.for_badge()
    assert sent["theme"] == "tinted-light"
    assert set(sent["palette"]) >= {"bg", "panel", "ink", "dim", "accent", "grid", "ramp"}
    assert look.from_palette("tinted", sent["palette"]) is not None

    # One preview path for every theme, tinted or not, so what is shown and what reaches the
    # badge cannot drift apart.
    picked = ",".join(str(part) for part in derive.accents("saturated")[8])
    status, shown = h.raw("GET", f"/api/theme?theme=tinted-light&accent={picked}")
    assert status == 200, (status, shown)
    assert shown["palette"]["bg"] == list(sent["palette"]["bg"]), "the preview would differ"
    status, plain = h.raw("GET", "/api/theme?theme=mono")
    assert status == 200 and plain["palette"]["bg"] == list(themes_bg("mono")), plain
    status, bad = h.raw("GET", "/api/theme?theme=nonesuch")
    assert status == 400, status

    # The UI offers exactly what the host will accept.
    status, caps = h.raw("GET", "/api/capabilities")
    assert {r["name"] for r in caps["themes"] if r["derived"]} == derived
    assert set(caps["accents"]) == set(derive.ACCENT_FAMILIES)
    assert caps["accents"]["saturated"] == [list(a) for a in derive.accents("saturated")]
    web = pathlib.Path("src/statsbadge/web")
    page, script = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    assert "data-tint" in page and 'id="screens"' in page, "no picker or preview in the UI"
    # Which themes take an accent is the host's answer, and the UI holds no list.
    assert "record.derived" in script and "config.tint" in script
    assert "caps.tinted" not in script, "the UI still keeps a list of tinted themes"
    # Clicking along the swatches starts several previews; the last click has to win rather
    # than the last reply, or the panel shows a colour nobody chose.
    assert "previewWanted" in script, "a stale preview reply can win"


def test_the_ui_takes_its_colours_from_the_host():
    """The UI asks for the palette of whatever theme is selected, so there is nowhere for
    a copy to live and drift from the badge's tables."""
    web = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    assert "THEME_COLOURS" not in web, "the UI still carries a palette table"
    assert "/api/theme?" in web, "the UI does not ask the host for a palette"
    # Four pages at 320x240, drawn from what the host sent.
    assert "drawDial" in web and "drawGraph" in web, "the preview does not draw the pages"
    assert "--pv-" not in web + sheet, "the preview still keeps colours in the sheet"
    # The rule for a graph's second series is the badge's, resolved on the host and sent.
    assert "shown.series" in web, "the UI picks the second series itself"
    assert '"series"' in pathlib.Path("src/statsbadge/server.py").read_text(encoding="utf-8")
    # The UI's accent and ramp are generated from the dark theme, not typed in.
    assert "--ramp-0:" not in sheet, "the sheet still declares the ramp by hand"
    assert "--accent:" not in sheet, "the sheet still declares the accent by hand"
    assert "var(--ramp-0)" in sheet, "the sheet stopped using the generated tokens"
    assert "/tokens.css" in pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
