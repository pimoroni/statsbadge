"""The config UI's markup: the controls it offers and how they group."""

import re

from statsbadge import layout


def test_every_control_is_bound_to_a_setting_the_server_takes(ui):
    """Every binding in the script names a control in the page and a setting `validate`
    keeps."""
    # Three files that have to agree and none imports another, so they are checked as the
    # pairs they are.
    assert ui.bindings, "no bindings were read out of app.js"
    for control, setting in ui.bindings.items():
        assert control in ui.ids, f"{control} is bound but not in the page"
        assert ui.ids[control] in ("input", "select"), (control, ui.ids[control])
        assert setting in layout.DEFAULT_CONFIG, f"{control} is bound to {setting}, not a setting"

    # A default round-trips through validate unchanged.
    kept = layout.validate({**layout.DEFAULT_CONFIG, "pages": layout.DEFAULT_PAGES})
    for control, setting in ui.bindings.items():
        assert setting in kept, f"validate drops {setting}, which {control} sets"


def test_a_hint_beside_a_secret_leaves_room_for_the_field(ui):
    """Prose in a settings grid spans both columns, so it cannot set the first track's
    width."""
    # The first column is max-content: a hint left in it sizes the track to the paragraph
    # unwrapped, and an octopus key field beside one measured 16px.
    sheet = ui.css
    block = sheet[sheet.index(".secrets {"):]
    block = block[:block.index("\n}")]
    assert "max-content" in block, "the first column no longer sizes to its content"
    spans = re.search(r"\n\s*p \{([^}]*)\}", block)
    assert spans, "a hint in the secrets block has no rule of its own"
    assert "grid-column: 1 / -1" in spans.group(1), spans.group(1)

    # The block builds a label, a field and a hint per secret, so all three need a column.
    script = ui.script
    built = script[script.index("function secretsBlock"):]
    built = built[:built.index("\n}")]
    assert 'el("p"' in built, "the hint is no longer drawn here"
    for ruled in ("label {", 'input[type="text"] {', "p {", "button {"):
        assert ruled in block, f"{ruled} has no column in .secrets"


def test_the_theme_box_spans_the_panels_beside_it(ui):
    """The theme box's row span is a count, and matches the panels it stands beside."""
    page = ui.markup
    settings = page.split('aria-label="Settings"')[1].split('<section id="badges">')[0]
    beside = len(re.findall(r"<section(?: class=\"[^\"]*\")?>", settings))
    assert beside == 6, beside

    sheet = ui.css
    spanned = re.search(r'section\[aria-label="Theme"\] \{ grid-column: 1; grid-row: span (\d+)',
                        sheet)
    assert spanned, "the theme box no longer spans the panels"
    assert int(spanned.group(1)) == beside, (spanned.group(1), beside)

    # Past this a preview stops being a picture of a 320x240 screen and becomes a poster.
    assert "--page: 1280px" in sheet
    assert "max-width: var(--page)" in sheet


def sections_of(page):
    """The config UI's sections, keyed by heading.

    Only the ones that are a `section`: the page list is a column to itself and would
    otherwise swallow the heading after it."""
    found = {}
    for part in page.split("<h2>")[1:]:
        heading, rest = part.split("</h2>", 1)
        found[heading] = rest.split("</section>")[0]
    return found


def test_the_settings_are_grouped_by_what_they_do(ui):
    """Every control sits under the heading for what it changes, and under no other."""
    page = ui.markup
    sections = sections_of(page)
    wanted = {
        "Look &amp; Feel": ("theme", "accentb"),
        "Readings": ("interval", "points"),
        "Plots and gauges": ("smooth", "rows", "gaugefill"),
        "Movement": ("plotanim", "animate"),
        "Paging and auto advance": ("slide", "idle", "advance"),
        "Backlight and case lights": ("brightness", "autobright", "caselights"),
    }
    for heading, controls in wanted.items():
        assert heading in sections, heading
        for control in controls:
            assert f'id="{control}"' in sections[heading], (heading, control)
            # A moved control leaves its old place empty.
            for other in wanted:
                assert other == heading or f'id="{control}"' not in sections[other], (
                    control, other)


def test_the_general_settings_have_a_place_of_their_own(ui):
    """Between Look & Feel and Badges, so a host-wide setting is not filed under Help.

    The location lived in the Help tab first, which is for what a platform needs set up by
    hand. Nothing read that tab, so nothing said the control had gone to the wrong place.
    """
    page = ui.markup
    assert "<h2>General Settings</h2>" in page, "no General Settings heading"
    assert 'id="general"' in page, "the section the script fills is not in the page"

    order = [page.index(mark) for mark in
             ("<h2>Look &amp; Feel</h2>", "<h2>General Settings</h2>", "<h2>Badges</h2>")]
    assert order == sorted(order), order


def test_the_general_settings_are_built_and_saved(ui):
    """The script fills that section and writes it to `/api/settings`.

    Read out of app.js, as the bindings above are: these controls are built at runtime, so
    there is no markup to check and no DOM here to build them in.
    """
    script = ui.script
    assert "function renderGeneral(" in script, "nothing builds the section"
    assert "renderGeneral()" in script.replace("function renderGeneral()", ""), \
        "renderGeneral is defined but never called"

    body = script[script.index("function renderGeneral("):]
    body = body[:body.index("\n}\n")]
    assert '$("general")' in body, "renderGeneral does not fill the general section"
    # replaceChildren takes the markup's heading with it unless the heading is restored.
    # Caught in a browser and not here: the markup still had the h2 either way.
    assert 'querySelector("h2")' in body, "a redraw would drop the General Settings heading"
    for control in ("hostplace", "hostlat", "hostlon"):
        assert control in body, f"{control} is not offered"
    assert '"/api/settings"' in body, "the settings are not saved to /api/settings"

    # The Help tab reports and no longer writes, so a location cannot be saved from it.
    assert '"/api/help", {' not in script, "the help tab still posts settings"
