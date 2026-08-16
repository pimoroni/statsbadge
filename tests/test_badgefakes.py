"""The stand-ins are held to the firmware's builtins list, so neither side drifts."""

import tomllib

import badgefakes


def test_every_badge_builtin_has_a_decision(repo_root):
    """A name the firmware injects is either faked here or listed in NOT_FAKED."""
    # `tools/check_app.py` reads the same list to find undefined names in the app.
    with (repo_root / "ci" / "ruff.toml").open("rb") as handle:
        declared = set(tomllib.load(handle)["builtins"])
    decided = set(badgefakes.FAKES) | set(badgefakes.NOT_FAKED)
    assert not declared - decided, f"no decision in badgefakes: {sorted(declared - decided)}"
    assert not decided - declared, f"not a badge builtin: {sorted(decided - declared)}"


def test_every_badge_module_imports_on_the_shim(badge_modules):
    """Every badge module but the app itself imports the way the badge imports it."""
    # `look` builds a Theme at import, which calls `color.rgb`, so this also checks the
    # stand-ins are installed before the first import.
    assert badge_modules["look"].get(badge_modules["look"].DEFAULT) is not None
    assert badge_modules["draw"].LINE_FLAGS
    assert badge_modules["pages"].EXTRA == {}
