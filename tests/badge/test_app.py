"""The host's half of what the app does: the settings it sends, and the shared figures."""

import json
import socket
import sys

from statsbadge import install, layout


def test_a_beacon_goes_out_on_every_interface_it_can_name():
    """A subnet broadcast is worked out per interface, since Windows reports none."""
    # Without one the only packet leaving is the global broadcast, which on a machine
    # with a virtual switch goes out by whichever interface holds the default route.
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
    """A scan runs for two beacon intervals, so it cannot fall between two beacons."""
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
    """A battery is read the other way up from a load, so a full one is calm."""

    sys.path.insert(0, install.app_source_dir())
    import pages

    assert pages.severity_of("power.battery_pct", 1.0) == 0.0
    assert pages.severity_of("power.battery_pct", 0.1) == 0.9
    # Everything else is coloured by where the reading sits.
    for ref in ("cpu.pct", "cpu.temp", "mem.pct", "disk.pct", "gpu.temp"):
        assert pages.severity_of(ref, 0.9) == 0.9, ref
    assert pages.severity_of("cpu.pct", None) is None


def test_the_badge_dims_to_suit_the_room(ui):
    """The scale separates a dark room, a curtained one and a lit one, below the rail."""
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


def test_the_badge_can_report_on_itself_with_no_host():
    """The badge page reads the badge, so a prune on what the host can fill keeps it."""
    config = layout.validate({"pages": [{"id": "b1", "kind": "badge", "title": "Badge"},
                                        {"id": "cpu", "kind": "dial", "field": "cpu.pct"}]})
    page = config["pages"][0]
    assert page == {"id": "b1", "kind": "badge", "title": "Badge"}, page
    # A host measuring none of them still keeps it, and drops the dial.
    kept = layout.prune(config["pages"], {"available": {}})
    assert [p["kind"] for p in kept] == ["badge"], kept
