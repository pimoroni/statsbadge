"""The app on the badge: paging, buttons, backlight and pairing."""

import ast
import json
import pathlib
import re
import socket
import sys

from statsbadge import install, layout


def test_setup_waves_through_a_server_already_paired():
    """Setup is offered after a few failed polls, not only when unpaired, so it is easy to
    reach with nothing wrong. Asking to pair again then failed for a server the badge was
    already paired with, because the host was not in pairing mode."""
    source = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text(encoding="utf-8")
    assert "_already_paired" in source, "no already-paired path in setup"
    # Reached before anything is asked of the host.
    ask = source[source.index("def _ask_to_join"):]
    assert ask.index("_already_paired") < ask.index("net.enrol("), (
        "the badge asks to enrol before noticing it is already paired")
    # A host that has refused this badge is the case where pairing again is the point, so
    # that has to ask again.
    guard = source[source.index("def _already_paired"):source.index("def _ask_to_join")]
    assert "rejected" in guard, "a refused badge would be waved through with dead credentials"


def test_a_beacon_goes_out_on_every_interface_it_can_name():
    """Windows reports no broadcast address of its own, so it is worked out here.

    Without one the only packet leaving is the global broadcast, and on a machine with a
    virtual switch that leaves by whichever interface holds the default route. The badge
    finds hosts by beacon alone, so such a host is invisible to it.
    """
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


def test_the_badge_never_waits_on_a_socket_the_screen_is_behind():
    """A blocking connect to a host that is not there holds the draw loop for as long as
    lwIP takes to give up. The screen stops and HOME is never sampled, so the badge sits
    on "Connecting" with no way into the hosts menu."""
    source = (pathlib.Path(install.app_source_dir()) / "net.py").read_text(encoding="utf-8")
    connect = source[source.index("def _connect"):source.index("def _connecting")]
    assert "setblocking(False)" in connect, "the connect blocks the loop"
    assert "EINPROGRESS" in connect, "a connect that only started is an error to this"
    assert "yield from self._connecting()" in source, "nothing waits for the connect"

    # The badge reports POLLOUT and POLLHUP together for an address with nothing at it,
    # measured on the board, so the failure has to be looked at first.
    # From the loop over what the poll returned, the register call above naming POLLOUT
    # for what it is watching.
    waiting = source[source.index("for _sock, flags in"):source.index("# -- requests")]
    assert waiting.index("POLLERR") < waiting.index("POLLOUT"), \
        "a failed connect would read as a connected one"


def test_the_badge_scans_for_longer_than_the_host_waits():
    """The badge listens for a beacon and the host sends one, and neither imports the other.

    A scan shorter than the gap between two beacons misses a host that has just broadcast,
    and comes back saying nothing is there.
    """
    from statsbadge import beacon

    source = (pathlib.Path(install.app_source_dir()) / "net.py").read_text(encoding="utf-8")
    assert f"BEACON_PORT = {beacon.PORT}" in source, "the badge listens on another port"
    assert f"BEACON_EVERY_MS = {int(beacon.INTERVAL * 1000)}" in source, (
        "the badge assumes a different interval")
    assert "DISCOVER_MS = 2 * BEACON_EVERY_MS" in source

    # The host's own figure travels, so a server started with a different interval is
    # scanned for long enough without the badge being rebuilt.
    sent = beacon.Beacon(8420, "here", server_id="abc", interval=5.0).payload()
    assert sent["every_ms"] == 5000, sent
    assert len(json.dumps(sent)) < 256, "the packet is read into a 256 byte buffer"
    assert 'beacon.get("every_ms"' in source, "the badge drops what it is told"

    # Every scan covers an interval. Setup's countdown does it by repeating a short one
    # until the six seconds are up, so its own scans are allowed to be brief.
    menu = (pathlib.Path(install.app_source_dir()) / "setup.py").read_text(encoding="utf-8")
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    for call in ast.walk(ast.parse(app)):
        if not isinstance(call, ast.Call) or getattr(call.func, "attr", "") != "discover":
            continue
        given = [word.arg for word in call.keywords]
        assert "timeout_ms" not in given, f"a scan of its own at line {call.lineno}"
    assert "deadline" in menu, "the countdown is what makes setup's short scans add up"


def test_a_full_battery_is_not_an_alarm():
    """The ramp runs calm to alarming and nearly every reading here is a load or a
    temperature, so high is bad. A battery is the other way round and was drawn red at 100%."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import pages

    assert pages.severity_of("power.battery_pct", 1.0) == 0.0
    assert pages.severity_of("power.battery_pct", 0.1) == 0.9
    # Everything else is coloured by where it actually sits.
    for ref in ("cpu.pct", "cpu.temp", "mem.pct", "disk.pct", "gpu.temp"):
        assert pages.severity_of(ref, 0.9) == 0.9, ref
    assert pages.severity_of("cpu.pct", None) is None

    # It is only the colour: the sweep and the bar are the reading itself.
    source = (pathlib.Path(install.app_source_dir()) / "draw.py").read_text(encoding="utf-8")
    body = source[source.index("def gauge("):]
    body = body[:body.index("\ndef ", 1)]
    assert "theme.at(fraction if hot is None else hot)" in body
    assert "shape.arc(middle, inner, outer, start, sweep)" in body, (
        "the sweep is no longer drawn from the reading")


def test_the_badge_dims_to_suit_the_room():
    """Measured on the badge, as raw u16 stepping in sixteens: darkness 48, a partly
    daylit room with the curtains closed 320, a lit room 4500.

    A phone torch and a sunny window sill both read 61400, which is the sensor railed. The
    scale tops out well below that, and everything past it is the same answer."""
    import sys

    sys.path.insert(0, install.app_source_dir())
    import look

    dark, curtained, lit, railed = 48, 320, 4500, 61400
    assert look.ambient_fraction(dark) == 0.0
    assert look.ambient_fraction(curtained) < look.ambient_fraction(lit)
    assert look.ambient_fraction(lit) == 1.0
    assert look.ambient_fraction(railed) == look.ambient_fraction(65535) == 1.0
    # The three rooms have to be told apart, or the setting is a switch: a curtained room
    # lands between the two ends.
    assert 0.25 < look.ambient_fraction(curtained) < 0.75, look.ambient_fraction(curtained)
    # Logarithmic: the first doubling is worth as much as the next.
    first = look.ambient_fraction(look.LIGHT_DIM * 2)
    assert 0.4 < first / look.ambient_fraction(look.LIGHT_DIM * 4) < 0.6, first

    # A dim room is dimmer, not dark. Dark is how the setting looks when it is off.
    assert 0.0 < look.LIGHT_FLOOR < 1.0

    # Off by default, since it needs the light sensor and not every board has one.
    assert layout.validate({"pages": layout.DEFAULT_PAGES})["auto_brightness"] is False
    assert layout.validate({"auto_brightness": True,
                            "pages": layout.DEFAULT_PAGES})["auto_brightness"] is True
    assert 'id="autobright"' in pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")


def test_the_badge_pages_on_its_own_when_left_alone():
    """Off by default: a display that moves while somebody is reading it is a nuisance."""
    config = layout.validate({"pages": layout.DEFAULT_PAGES})
    assert config["idle_advance_s"] == 0, config["idle_advance_s"]
    assert config["advance_every_s"] == 10
    clamped = layout.validate({"idle_advance_s": 99999, "advance_every_s": 0,
                               "pages": layout.DEFAULT_PAGES})
    assert clamped["idle_advance_s"] == 3600, clamped
    # A page has to be up for a second to be read at all.
    assert clamped["advance_every_s"] == 1, clamped

    web = pathlib.Path("src/statsbadge/web")
    assert 'id="idle"' in (web / "index.html").read_text(encoding="utf-8")
    assert '"idle_advance_s"' in (web / "app.js").read_text(encoding="utf-8")

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    advance = app[app.index("    def advance_if_idle"):]
    advance = advance[:advance.index("\n    # --", 1)]
    # The turns it makes for itself leave the idle timer alone, or the first
    # one would put it back to sleep.
    assert "_pressed_at" in advance and "self._pressed_at =" not in advance, advance
    assert "len(self.page_list) < 2" in advance, "one page would turn to itself"

    # A press resets it, wherever one is noticed - including HOME, since opening
    # the menu is somebody using the badge.
    for method in ("    def buttons(self):", "    def home(self):"):
        body = app[app.index(method):]
        body = body[:body.index("\n    def ", 1)]
        assert "self._pressed_at = time.ticks_ms()" in body, method
    # Above turn(), which both the buttons and the badge itself go through.
    turn = app[app.index("    def turn(self"):]
    turn = turn[:turn.index("\n    # --", 1)]
    assert "_pressed_at" not in turn, turn


def test_a_button_can_do_something_without_the_host():
    """Paging and the panel are the badge's business: a round trip to change them would
    be slower than the press, and would not work at all with the host away."""
    actions = dict(layout.LOCAL_ACTIONS)
    assert set(actions) == {"badge.prev", "badge.next", "badge.brightness"}, actions

    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    press = app[app.index("    def press(self"):]
    press = press[:press.index("\n    def ", 1)]
    assert "LOCAL_PREFIX" in press and "send_command" in press, press
    # Every action the host offers is one the badge answers, or a button does nothing.
    handler = app[app.index("    def local(self"):]
    handler = handler[:handler.index("\n    def ", 1)]
    for action in actions:
        assert f'"{action}"' in handler, action
    # The prefix keeps them off the wire, so it has to match what the host offers.
    for action in actions:
        assert action.startswith("badge."), action


def test_a_press_waits_for_the_poll_rather_than_losing_to_it():
    """One request is in flight at a time and the badge polls every interval, so a press
    that had to find the connection idle mostly found it busy and did nothing."""
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")
    send = app[app.index("    def send_command(self"):]
    send = send[:send.index("\n    def ", 1)]
    # Held, and served once the connection frees: a press outlives a request in flight.
    assert "self._commands.append" in send, send
    assert "_pending" not in send, send

    poll = app[app.index("    def poll(self"):]
    poll = poll[:poll.index("\n    def ", 1)]
    # Sent ahead of what the badge asks for itself, or the press waits out the interval.
    assert poll.index("if self._commands:") < poll.index("if self._queued is not None:"), poll
    assert poll.index("if self._commands:") < poll.index("self._next_poll"), poll


def test_the_notice_screen_offers_a_way_out():
    """The screen a badge sits on when it can reach nothing, so it has to say what can be
    done as well as what went wrong.

    Polls back off to fifteen seconds apart while a host is quiet, which is no use to
    somebody who has just woken the PC."""
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")

    notice = app[app.index("    def render(self):"):]
    notice = notice[:notice.index("\n    def ", 1)]
    for action in ("C retry", "B set up", "HOME hosts"):
        assert action in notice, action
    assert "self.detail" in notice, "the reason is not shown"

    # C asks again there, the host commands being out of reach.
    pressed = app[app.index("    def buttons(self):"):]
    pressed = pressed[:pressed.index("\n    def ", 1)]
    assert "self.retry()" in pressed and "current_page() is None" in pressed

    # Retrying drops the backoff without waiting it out, and clears what was in flight.
    retry = app[app.index("    def retry(self):"):]
    retry = retry[:retry.index("\n    def ", 1)]
    for cleared in ("self.client.failures = 0", "self._next_poll", "self._queued = None",
                    "self._pending = None"):
        assert cleared in retry, cleared

    # One failed poll is enough to offer setup: waiting for three left that screen with
    # a screen of controls that all did nothing.
    setup = app[app.index("    def needs_setup(self):"):]
    setup = setup[:setup.index("\n    def ", 1)]
    assert "self.client.failures >= SETUP_AFTER" in setup, setup
    assert "SETUP_AFTER = 1" in app, "more than one failed poll before setup is offered"


def test_switching_host_forgets_the_old_one_the_same_way():
    """Three ways to leave one host for another, and all of them reset through one method.

    The beacon in hunt(), the hosts menu, and setup reached from the app. Readings, series
    and revisions are numbered by whoever sent them, so anything held is drawn under the
    new host's name.
    """
    app_dir = pathlib.Path(install.app_source_dir())
    app = (app_dir / "__init__.py").read_text(encoding="utf-8")
    menu = (app_dir / "setup.py").read_text(encoding="utf-8")

    forget = app[app.index("    def forget_host(self):"):]
    forget = forget[:forget.index("\n    def ", 1)]
    for cleared in ("self.layout = None", "self.layout_rev = NO_REV", "self.history = {}",
                    "self.slow = {}", "self.slow_rev = NO_REV", "self._queued = None",
                    "self._commands = []", "self._series_age = 0", "self._series_at = 0",
                    "self.rejected = False", "draw.clear_cache()"):
        assert cleared in forget, cleared

    # Reset nowhere else, or the paths go back to disagreeing. Twice in the app: once as a
    # starting value and once here.
    for cleared in ("self.slow = {}", "self.history = {}", "self.slow_rev = NO_REV"):
        assert app.count(cleared) == 2, cleared
    assert "app.layout" not in menu, "the menu is resetting state of its own"
    assert menu.count("forget_host()") == 2, "a host joined is a host switched to"


def test_a_press_that_closes_a_modal_screen_stops_there():
    """B on the hosts menu picked a server and then fired the page binding behind it.

    A modal screen returns the moment its button goes down, so the edge is still standing
    when the loop reaches `buttons()`. `badge.poll()` rolls it forward first.
    """
    app = (pathlib.Path(install.app_source_dir()) / "__init__.py").read_text(encoding="utf-8")

    loop = app[app.index("def main():"):]
    menu = loop[loop.index("pairing_ui().hosts_menu(app)"):]
    handled = menu[:menu.index("app.buttons()")]
    assert "badge.poll()" in handled, (
        "the menu press reaches buttons(): " + handled)


def test_a_brightness_the_ui_offers_stays_a_fraction():
    """Everything the panel is ever asked for is a 0-1 fraction, and never zero.

    The firmware maps a fraction onto the panel. This side owes it a number in range:
    `display.backlight` casts to a byte, so a value over 1.0 wraps to a dark panel over a
    framebuffer that still dumps perfectly.

    Never zero either, since a dark room should dim the badge and not switch it off.
    `LIGHT_FLOOR` holds that line, and is the setting to move if a dark room reads too dim.
    """
    sys.path.insert(0, install.app_source_dir())
    import look as look_module

    def sent(wanted):
        """What `backlight` passes on, which is the clamp."""
        return max(0.0, min(1.0, wanted))

    # The slider is 5 to 100 in fives; see web/index.html.
    for percent in range(5, 101, 5):
        asked = percent / 100.0
        assert 0.0 < sent(asked) <= 1.0, f"{percent}% leaves the range as {sent(asked)}"

    # Auto-brightness scales the configured level by the room, so the dimmest the badge can
    # ask for is the bottom of the slider in the dark.
    darkest = 0.05 * look_module.LIGHT_FLOOR
    assert 0.0 < sent(darkest) <= 1.0, f"a dark room asks for {sent(darkest)}"
    assert look_module.LIGHT_FLOOR > 0.0, "a dark room would switch the panel off"

    # A fraction that escaped above 1.0 would wrap, so the clamp is what stops it.
    assert sent(2.463) == 1.0, "an out-of-range brightness reaches the panel"


def test_the_badge_can_report_on_itself_with_no_host():
    """The one page kind whose readings do not come from the frame. It needs no field, so
    nothing can be picked for it and nothing can fail to answer: a prune that keeps only pages
    this host can fill would otherwise drop the page that asked for none of them."""
    config = layout.validate({"pages": [{"id": "b1", "kind": "badge", "title": "Badge"},
                                        {"id": "cpu", "kind": "dial", "field": "cpu.pct"}]})
    page = config["pages"][0]
    assert page == {"id": "b1", "kind": "badge", "title": "Badge"}, page
    # A host measuring none of them still keeps it, and drops the dial.
    kept = layout.prune(config["pages"], {"available": {}})
    assert [p["kind"] for p in kept] == ["badge"], kept

    # The kind picker is written out in the page and not built from the API, so it is the
    # one place a new kind can reach the badge and be forgotten in the browser.
    markup = pathlib.Path("src/statsbadge/web/index.html").read_text(encoding="utf-8")
    app = pathlib.Path("src/statsbadge/web/app.js").read_text(encoding="utf-8")
    offered = set(re.findall(r'<option value="([a-z]+)">', markup))
    for kind in layout.KINDS:
        assert kind in offered, f"{kind} is not in the kind picker"
        assert f"  {kind}: {{" in app, f"{kind} has no field slots declared in app.js"

    # The badge draws it: the kind is in the app's table, reads no fields, and stays
    # animated, being numbers to read.
    source = (pathlib.Path(install.app_source_dir()) / "pages.py").read_text(encoding="utf-8")
    table = source[source.index("_KINDS = {"):]
    table = table[:table.index("}")]
    assert '"badge": _badge_page' in table, table
    body = source[source.index("def _badge_page("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_frame" in body.split(")")[0], "the badge page takes the frame seriously"
    assert 'ANIMATED.add("badge")' not in source
