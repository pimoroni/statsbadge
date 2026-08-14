"""The config UI's markup: the controls it offers and how they group."""

import pathlib
import re

from statsbadge import layout


def test_every_control_is_bound_to_a_setting_the_server_takes(ui):
    """A control in the page, a binding in the script, and a setting the server keeps.

    Three files have to agree and none imports another. Checked as the pairs they are:
    a control bound to a setting `validate` drops is one the browser appears to offer and
    the badge never sees, which no amount of matching either file's text would catch.
    """
    assert ui.bindings, "no bindings were read out of app.js"
    for control, setting in ui.bindings.items():
        assert control in ui.ids, f"{control} is bound but not in the page"
        assert ui.ids[control] in ("input", "select"), (control, ui.ids[control])
        assert setting in layout.DEFAULT_CONFIG, f"{control} is bound to {setting}, not a setting"

    # And the server keeps each of them: a default round-trips through validate unchanged.
    kept = layout.validate({**layout.DEFAULT_CONFIG, "pages": layout.DEFAULT_PAGES})
    for control, setting in ui.bindings.items():
        assert setting in kept, f"validate drops {setting}, which {control} sets"


def test_a_hint_beside_a_secret_leaves_room_for_the_field():
    """An extension's API key sits in a two-column grid whose first column is max-content.

    A hint left in that column sets the track to the width of the paragraph unwrapped, and
    the field beside it collapses: an octopus key with a one-line hint measured 16px. Every
    block of prose in the settings grids spans both columns for this reason.
    """

    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    block = sheet[sheet.index(".secrets {"):]
    block = block[:block.index("\n}")]
    assert "max-content" in block, "the first column no longer sizes to its content"
    spans = re.search(r"\n\s*p \{([^}]*)\}", block)
    assert spans, "a hint in the secrets block has no rule of its own"
    assert "grid-column: 1 / -1" in spans.group(1), spans.group(1)

    # The block builds a label, a field and a hint per secret, so all three need a column.
    script = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    built = script[script.index("function secretsBlock"):]
    built = built[:built.index("\n}")]
    assert 'el("p"' in built, "the hint is no longer drawn here"
    for ruled in ("label {", 'input[type="text"] {', "p {", "button {"):
        assert ruled in block, f"{ruled} has no column in .secrets"


def test_the_theme_box_spans_the_panels_beside_it():
    """The theme box stands beside the settings panels, which it does by spanning their
    rows. The span is a count, so it has to match how many there are."""

    page = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    settings = page.split('aria-label="Settings"')[1].split('<section id="badges">')[0]
    beside = len(re.findall(r"<section(?: class=\"[^\"]*\")?>", settings))
    assert beside == 6, beside

    sheet = pathlib.Path("src/statsbadge/web/app.css").read_text(encoding="utf-8")
    spanned = re.search(r'section\[aria-label="Theme"\] \{ grid-column: 1; grid-row: span (\d+)',
                        sheet)
    assert spanned, "the theme box no longer spans the panels"
    assert int(spanned.group(1)) == beside, (spanned.group(1), beside)

    # Past this a preview stops being a picture of a 320x240 screen and becomes a poster.
    assert "--page: 1280px" in sheet
    assert "max-width: var(--page)" in sheet


def sections_of(page):
    """The config UI's sections, keyed by heading. Only the ones that are a `section`: the
    page list is a column to itself and would otherwise swallow the heading after it."""
    found = {}
    for part in page.split("<h2>")[1:]:
        heading, rest = part.split("</h2>", 1)
        found[heading] = rest.split("</section>")[0]
    return found


def test_the_settings_are_grouped_by_what_they_do():
    """One list of every control was a soup. A setting is grouped under the heading it sits
    under, so the panel can be read by what somebody came to change."""
    page = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
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
            # In that one only, so a moved control leaves its old place empty.
            for other in wanted:
                assert other == heading or f'id="{control}"' not in sections[other], (
                    control, other)
