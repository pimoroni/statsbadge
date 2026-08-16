"""The clock extension's own logic: setting the badge's clock, and a page in another zone.

What `render` draws is checked against the firmware under `badge/wasm/`. Everything here
is arithmetic and runs on the host.
"""

import ast
import sys
import types

import pytest

CLOCK_BADGE = "extensions/statsbadge-clock/src/statsbadge_clock/badge"


@pytest.fixture
def clock_badge(monkeypatch, badge_modules, repo_root):
    """`clockface`, with a clock the RTC sets and localtime reads back.

    badgefakes does not carry `machine`: it is a MicroPython module, not one of the
    builtins the firmware injects. Coupling the RTC to localtime is what gives RESYNC_S any
    meaning. A stand-in that only records the call leaves localtime() on the host's clock,
    where the drift never closes and every reading resyncs.
    """
    class Clock:
        def __init__(self):
            self.at = [2026, 8, 16, 4, 5, 0, 5, 228]
            self.sets = []

        def datetime(self, parts):
            year, month, day, _weekday, hour, minute, second, _sub = parts
            self.at = [year, month, day, hour, minute, second, 5, 228]
            self.sets.append((hour, minute, second))

        def localtime(self):
            return tuple(self.at)

        def tick(self, seconds):
            """Run the badge's clock on, as it does between polls."""
            at = self.at[3] * 3600 + self.at[4] * 60 + self.at[5] + seconds
            self.at[3:6] = [at // 3600 % 24, at % 3600 // 60, at % 60]

    clock = Clock()
    machine = types.ModuleType("machine")
    machine.RTC = lambda: clock
    faketime = types.ModuleType("time")
    faketime.localtime = clock.localtime

    monkeypatch.syspath_prepend(str(repo_root / CLOCK_BADGE))
    # In place before the import below, which is a module-level `import machine`.
    monkeypatch.setitem(sys.modules, "machine", machine)

    pages = badge_modules["pages"]
    # Importing the module registers the page kind, and sys.modules keeps it. Restore the
    # registries, or a later test sees an extension it never loaded.
    extra, animated = dict(pages.EXTRA), set(pages.ANIMATED)

    import clockface

    # Set on the module too: the import is cached, so a second test would otherwise keep
    # the first one's clock.
    monkeypatch.setattr(clockface, "machine", machine)
    monkeypatch.setattr(clockface, "time", faketime)
    monkeypatch.setattr(clockface, "_synced", False)
    monkeypatch.setattr(clockface, "_synced_seq", None)

    yield clockface, clock

    pages.EXTRA.clear()
    pages.EXTRA.update(extra)
    pages.ANIMATED.clear()
    pages.ANIMATED.update(animated)


def test_the_clock_only_syncs_from_a_fresh_reading(clock_badge):
    """The clock is set once per reading, so a frame redrawn 45 times a second cannot drag
    the hands back."""
    clockface, clock = clock_badge
    reading = {"hour": 4, "minute": 5, "seconds": 0}

    clockface._resync(reading, seq=1)
    assert clock.sets == [(4, 5, 0)], "the first reading did not set the clock"

    # With the host away, the badge's clock runs on while the frame holds the time it was
    # polled at, so the disagreement passes RESYNC_S with no new reading behind it. The
    # seq is the only thing that stops the hands being dragged back to the last poll.
    clock.tick(clockface.RESYNC_S + 15)
    for _ in range(45):
        clockface._resync(reading, seq=1)
    assert len(clock.sets) == 1, "a frame redrawn on one reading dragged the hands back"


def test_a_reading_is_followed_only_once_it_disagrees_far_enough(clock_badge):
    """A fresh reading close to the badge's clock is ignored, since the correction is what
    shows and not the drift."""
    clockface, clock = clock_badge

    clockface._resync({"hour": 4, "minute": 5, "seconds": 0}, seq=1)
    clockface._resync({"hour": 4, "minute": 5, "seconds": 2}, seq=2)
    assert len(clock.sets) == 1, "a reading inside RESYNC_S restarted the sweep"

    # Past RESYNC_S, which no amount of pipeline latency accounts for.
    clockface._resync({"hour": 4, "minute": 6, "seconds": 5}, seq=3)
    assert clock.sets == [(4, 5, 0), (4, 6, 5)], clock.sets


def test_a_reading_missing_a_field_never_sets_the_clock(clock_badge):
    """A half-built reading leaves the clock where it is."""
    clockface, clock = clock_badge

    for reading in ({"hour": None, "minute": 5, "seconds": 0},
                    {"hour": 4, "minute": None, "seconds": 0},
                    {"hour": 4, "minute": 5, "seconds": None},
                    {}):
        clockface._resync(reading, seq=id(reading))
    assert clock.sets == [], clock.sets


def test_a_page_elsewhere_is_offset_from_the_host(clock_badge):
    """A zone is the difference between two readings in the frame, the shortest way round
    the day."""
    clockface, _clock = clock_badge
    here = {"hour": 12, "minute": 0, "seconds": 0}

    assert clockface._zone_offset(here, here) == 0
    assert clockface._zone_offset(here, {"hour": 15, "minute": 0, "seconds": 0}) == 3 * 3600
    assert clockface._zone_offset(here, {"hour": 9, "minute": 0, "seconds": 0}) == -3 * 3600

    # Either side of midnight is an hour apart, not twenty-three.
    late = {"hour": 23, "minute": 30, "seconds": 0}
    assert clockface._zone_offset(late, {"hour": 0, "minute": 30, "seconds": 0}) == 3600
    assert clockface._zone_offset({"hour": 0, "minute": 30, "seconds": 0}, late) == -3600

    # Nothing to work it out from is no offset, not a guess.
    assert clockface._zone_offset(None, here) == 0
    assert clockface._zone_offset(here, {}) == 0


def test_the_clock_is_set_from_the_host_alone(repo_root):
    """There is one hardware clock, so two pages in two zones must not each set it to
    theirs on being turned to.

    `render` draws, so it cannot be called here; what it is wired to is read off the
    parsed tree instead. Matching the source as text breaks on a reflow or a docstring
    that happens to name RTC().
    """
    source = (repo_root / CLOCK_BADGE / "clockface.py").read_text(encoding="utf-8")
    render = next(node for node in ast.parse(source).body
                  if isinstance(node, ast.FunctionDef) and node.name == "render")

    def calls(name):
        return [node for node in ast.walk(render)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == name]

    synced = calls("_resync")
    assert synced, "render no longer syncs the clock at all"
    for call in synced:
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "host", \
            "the clock is set from the page's zone rather than the host's"

    offsets = calls("_zone_offset")
    assert offsets, "a page elsewhere is not offset from the host"
    for call in offsets:
        assert [argument.id for argument in call.args] == ["host", "here"], \
            "the offset is not the host against the place the page shows"
