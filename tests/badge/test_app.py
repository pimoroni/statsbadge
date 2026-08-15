"""The host's half of what the app does: the settings it sends, and the figures both
sides have to agree on.

The app itself is built and driven in tests/badge/wasm/test_app.py, and its HTTP client
against a real server in tests/badge/wasm/test_net.py. Two tests here still read source,
both on code that cannot be run: `__init__.py` starts the app on import, and `main()`
never returns.
"""

import json
import pathlib
import re
import socket
import sys

from statsbadge import install, layout


def test_the_entry_point_starts_the_app_and_can_be_quit():
    """__init__.py runs the app, and binds `on_exit` before main() blocks.

    Read rather than run: importing this module here would start the app. app.py
    starting nothing is checked by tests/badge/wasm/test_app.py, which imports it.
    """
    entry = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(
        encoding="utf-8")
    assert "app.main(APP_DIR)" in entry, "nothing starts the app"
    # The launcher reads on_exit off the module it imported, while main() is still going.
    assert entry.index("on_exit = app.on_exit") < entry.index("app.main("), \
        "HOME would quit without saving the page"


def test_setup_waves_through_a_server_already_paired():
    """Setup joins a host this badge already holds credentials for without enrolling
    again."""
    # Setup is reachable after a few failed polls, so it is easy to arrive at with nothing
    # wrong, and the host will not be in pairing mode.
    source = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text(encoding="utf-8")
    assert "_already_paired" in source, "no already-paired path in setup"
    ask = source[source.index("def _ask_to_join"):]
    assert ask.index("_already_paired") < ask.index("net.enrol("), (
        "the badge asks to enrol before noticing it is already paired")
    # A host that has refused this badge is the case where pairing again is the point.
    guard = source[source.index("def _already_paired"):source.index("def _ask_to_join")]
    assert "rejected" in guard, "a refused badge would be waved through with dead credentials"


def test_a_beacon_goes_out_on_every_interface_it_can_name():
    """A subnet broadcast is worked out per interface, since Windows reports none."""
    # Without one the only packet leaving is the global broadcast, which on a machine with
    # a virtual switch goes out by whichever interface holds the default route.
    from statsbadge import beacon

    assert beacon._subnet_broadcast("10.10.1.155", "255.255.255.0") == "10.10.1.255"
    assert beacon._subnet_broadcast("192.168.0.5", "255.255.0.0") == "192.168.255.255"
    # A point to point link has no broadcast address, and nothing to send to.
    assert beacon._subnet_broadcast("10.0.0.1", "255.255.255.254") is None
    assert beacon._subnet_broadcast("10.0.0.1", None) is None

    import psutil
    entry = type("Address", (), {"family": socket.AF_INET, "address": "10.10.1.155",
                                 "netmask": "255.255.255.0", "broadcast": None})
    was = psutil.net_if_addrs
    psutil.net_if_addrs = lambda: {"Ethernet": [entry]}
    try:
        addresses = beacon._broadcast_addresses()
    finally:
        psutil.net_if_addrs = was
    assert addresses == ["255.255.255.255", "10.10.1.255"], addresses


def test_the_badge_scans_for_longer_than_the_host_waits(badge_constants):
    """A scan runs for two beacon intervals, so it cannot fall between two beacons.

    The two sides never import each other, and `badge_constants` evaluates the badge's
    numbers rather than matching them as text.
    """
    from statsbadge import beacon

    badge = badge_constants("net.py")
    assert badge["BEACON_PORT"] == beacon.PORT, "the badge listens on another port"
    assert badge["BEACON_EVERY_MS"] == int(beacon.INTERVAL * 1000), (
        "the badge assumes a different interval")
    assert badge["DISCOVER_MS"] == 2 * badge["BEACON_EVERY_MS"], badge["DISCOVER_MS"]

    # The figure travels in the packet, so a server started with a different interval is
    # scanned for long enough without the badge being rebuilt.
    sent = beacon.Beacon(8420, "here", server_id="abc", interval=5.0).payload()
    assert sent["every_ms"] == 5000, sent
    assert len(json.dumps(sent)) < 256, "the packet is read into a 256 byte buffer"


def test_a_full_battery_is_not_an_alarm():
    """A battery is read the other way up from a load, so a full one is calm.

    `Gauges` in tests/badge/wasm/test_draw.py covers the drawing: the severity picks
    the colour, the reading sets the sweep.
    """

    sys.path.insert(0, install.app_source_dir())
    import pages

    assert pages.severity_of("power.battery_pct", 1.0) == 0.0
    assert pages.severity_of("power.battery_pct", 0.1) == 0.9
    # Everything else is coloured by where the reading sits.
    for ref in ("cpu.pct", "cpu.temp", "mem.pct", "disk.pct", "gpu.temp"):
        assert pages.severity_of(ref, 0.9) == 0.9, ref
    assert pages.severity_of("cpu.pct", None) is None


def test_the_badge_dims_to_suit_the_room(ui):
    """The scale tells a dark room, a curtained one and a lit one apart, and tops out below
    the sensor's rail."""
    # Measured on the badge as raw u16 stepping in sixteens: darkness 48, curtains closed
    # 320, a lit room 4500. A phone torch and a sunny sill both read 61400, railed.

    sys.path.insert(0, install.app_source_dir())
    import look

    dark, curtained, lit, railed = 48, 320, 4500, 61400
    assert look.ambient_fraction(dark) == 0.0
    assert look.ambient_fraction(curtained) < look.ambient_fraction(lit)
    assert look.ambient_fraction(lit) == 1.0
    assert look.ambient_fraction(railed) == look.ambient_fraction(65535) == 1.0
    # A curtained room lands between the two ends, or the setting is a switch.
    assert 0.25 < look.ambient_fraction(curtained) < 0.75, look.ambient_fraction(curtained)
    # Logarithmic: the first doubling is worth as much as the next.
    first = look.ambient_fraction(look.LIGHT_DIM * 2)
    assert 0.4 < first / look.ambient_fraction(look.LIGHT_DIM * 4) < 0.6, first

    # A dim room is dimmer, not dark.
    assert 0.0 < look.LIGHT_FLOOR < 1.0

    # Off by default, since it needs the light sensor and not every board has one.
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["auto_brightness"] is False
    assert layout.validate({"auto_brightness": True,
                            "pages": layout.DEFAULT_PAGES})["auto_brightness"] is True
    assert ui.bindings.get("autobright") == "auto_brightness", ui.bindings


def test_idle_paging_is_off_by_default_and_bounded(ui):
    """The badge side is `IdleAdvance` in tests/badge/wasm/test_app.py."""
    config = layout.validate({"pages": layout.DEFAULT_PAGES})
    assert config["idle_advance_s"] == 0, config["idle_advance_s"]
    assert config["advance_every_s"] == 10
    clamped = layout.validate({"idle_advance_s": 99999, "advance_every_s": 0,
                               "pages": layout.DEFAULT_PAGES})
    assert clamped["idle_advance_s"] == 3600, clamped
    # A page has to be up for a second to be read at all.
    assert clamped["advance_every_s"] == 1, clamped

    assert ui.bindings.get("idle") == "idle_advance_s", ui.bindings
    assert ui.bindings.get("advance") == "advance_every_s", ui.bindings


def test_the_host_offers_three_actions_the_badge_answers_itself():
    """The three actions `LocalActions` drives in tests/badge/wasm/test_app.py.

    Written out on both sides: the badge cannot import this, and the prefix is what keeps
    a press for one of them off the wire.
    """
    actions = dict(layout.LOCAL_ACTIONS)
    assert set(actions) == {"badge.prev", "badge.next", "badge.brightness"}, actions
    for action in actions:
        assert action.startswith("badge."), action


def test_a_press_that_closes_a_modal_screen_stops_there():
    """A modal screen returns with its button still down, so the press is rolled forward
    before `buttons()` sees it."""
    app = (pathlib.Path(install.app_source_dir()) / "app.py").read_text(encoding="utf-8")

    loop = app[app.index("def main("):]
    menu = loop[loop.index("pairing_ui().hosts_menu(app)"):]
    handled = menu[:menu.index("app.buttons()")]
    assert "badge.poll()" in handled, (
        "the menu press reaches buttons(): " + handled)


def test_the_badge_can_report_on_itself_with_no_host(ui):
    """The badge page reads the badge, so a prune on what the host can fill keeps it.

    `PageKinds` in tests/badge/wasm/test_pages.py renders every kind the app has a
    handler for, this one included.
    """
    config = layout.validate({"pages": [{"id": "b1", "kind": "badge", "title": "Badge"},
                                        {"id": "cpu", "kind": "dial", "field": "cpu.pct"}]})
    page = config["pages"][0]
    assert page == {"id": "b1", "kind": "badge", "title": "Badge"}, page
    # A host measuring none of them still keeps it, and drops the dial.
    kept = layout.prune(config["pages"], {"available": {}})
    assert [p["kind"] for p in kept] == ["badge"], kept

    # The kind picker is written out in the page rather than built from the API, so a new
    # kind can reach the badge and be forgotten in the browser.
    markup = ui.markup
    app = ui.script
    offered = set(re.findall(r'<option value="([a-z]+)">', markup))
    for kind in layout.KINDS:
        assert kind in offered, f"{kind} is not in the kind picker"
        assert f"  {kind}: {{" in app, f"{kind} has no field slots declared in app.js"
