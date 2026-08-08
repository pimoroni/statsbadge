"""Pairing from the badge. Nothing is typed on it.

Finds hosts by beacon, asks the one you pick to let it in, and shows a short code. Check
it against the host and approve it there, in the config UI or the terminal.

The host mints the code per request. Not derived from badge.uid: that travels as
X-Badge-Id over plain HTTP, so anyone on the network could show a matching code.

A back, B select, C next. HOME quits. Returns True to carry on into the app.
"""

import time

import draw
import look
import net

POLL_INTERVAL_MS = 1200
# The host beacons every 2s, so a scan has to be longer than that to catch every server.
BEACON_INTERVAL_MS = 2000


def hosts_menu(app):
    """Switch host, add one, or leave. Opened with HOME.

    Rescans on open, so a server started after the app did turns up without a restart.
    Returns "exit" if the user chose to leave the app.
    """
    while True:
        rows = _host_rows(app)
        picked = _pick_row(app, rows)
        if picked is None:
            return None
        if picked["kind"] == "exit":
            return "exit"
        if picked["kind"] == "rescan":
            continue
        if picked["kind"] == "known":
            if app.config.switch(picked["id"]):
                app.forget_host()
            return None
        if _ask_to_join(app, picked["host_entry"]):
            app.forget_host()
            return None


def _host_rows(app):
    """Known hosts, then any unpaired ones answering now, then rescan and exit."""
    draw.banner(app.theme, "Hosts", "looking for servers")
    badge.update()
    seen = {}
    # Longer than the beacon interval, or a server that has just broadcast is missed and
    # the list silently comes back short.
    for entry in net.discover(timeout_ms=2 * BEACON_INTERVAL_MS):
        if entry.get("id"):
            seen[entry["id"]] = entry

    rows = []
    for server_id, host in app.config.hosts.items():
        live = seen.pop(server_id, None)
        if live:
            app.config.note_address(server_id, live["host"], live["port"],
                                    live.get("name"))
        rows.append({
            "kind": "known", "id": server_id,
            "label": host.get("name") or host.get("host"),
            "detail": f"{host.get('host')}:{host.get('port')}",
            "note": "active" if server_id == app.config.active else
                    ("here" if live else "not seen"),
        })
    for server_id, entry in seen.items():
        rows.append({
            "kind": "new", "id": server_id, "host_entry": entry,
            "label": entry.get("name") or entry["host"],
            "detail": f"{entry['host']}:{entry['port']}",
            "note": "add",
        })
    rows.append({"kind": "rescan", "label": "Look again", "detail": "", "note": ""})
    rows.append({"kind": "exit", "label": "Leave the app", "detail": "", "note": ""})
    return rows


MAX_ROWS = 6


def _pick_row(app, rows):
    """Draw a list and return the chosen row, or None to close."""
    index = 0
    shown = rows[:MAX_ROWS]
    while True:
        draw_rows(app.theme, shown, index)
        badge.update()

        if badge.pressed(BUTTON_UP):
            index = (index - 1) % len(shown)
        if badge.pressed(BUTTON_DOWN):
            index = (index + 1) % len(shown)
        if badge.pressed(BUTTON_B):
            return shown[index]
        if badge.pressed(BUTTON_A) or badge.pressed(BUTTON_HOME):
            return None


def draw_rows(theme, shown, index):
    screen.pen = theme.bg
    screen.rectangle(rect(0, 0, look.W, look.H))
    draw.blit_label("HOSTS", look.SIZE_TITLE, theme.ink, look.PAD, 10)
    draw.blit_label("A close", look.SIZE_SMALL, theme.dim,
                    look.W - look.PAD, 16, align=2)

    top = 38
    height = 26
    for i, row in enumerate(shown):
        y = top + i * height
        selected = i == index
        screen.pen = theme.accent if selected else theme.panel
        screen.shape(shape.rounded_rectangle(rect(look.PAD, y, look.W - look.PAD * 2,
                                                  height - 4), 4))
        ink = theme.bg if selected else theme.ink
        dim = ink if selected else theme.dim
        draw.blit_label(row["label"], look.SIZE_VALUE, ink, look.PAD + 8, y + 2)
        # The note goes first and the address is right-aligned to clear it: "not seen" is wider
        # than a fixed column allows.
        right = look.W - look.PAD - 8
        if row["note"]:
            right -= draw.blit_label(row["note"], look.SIZE_SMALL, dim,
                                     right, y + 6, align=2) + 8
        if row["detail"]:
            draw.blit_label(row["detail"], look.SIZE_SMALL, dim, right, y + 6, align=2)
    draw.blit_label("UP/DOWN move    B select    HOME back", look.SIZE_SMALL,
                    theme.dim, look.W // 2, look.H - 16, align=1)


def run(app):
    hosts = _find_hosts(app)
    if hosts is None:
        return False
    if not hosts:
        return _no_host(app)

    while True:
        chosen = _choose_host(app, hosts) if len(hosts) > 1 else hosts[0]
        if chosen is None:
            return False
        outcome = _ask_to_join(app, chosen)
        if outcome is None:
            return False          # HOME
        if outcome:
            return True           # approved and saved
        if len(hosts) == 1:
            return False          # backed out, and nowhere else to try


# -- steps ------------------------------------------------------------------

def _find_hosts(app):
    """Listen for beacons, with a visible countdown."""
    draw.banner(app.theme, "Looking", "for a host on the network",
                "start: statsbadge pair")
    badge.update()
    found = []
    deadline = time.ticks_add(time.ticks_ms(), 6000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        found = net.discover(timeout_ms=600)
        if found:
            return found
        remaining = time.ticks_diff(deadline, time.ticks_ms()) // 1000
        draw.banner(app.theme, "Looking", "for a host on the network",
                    f"{remaining}s  -  HOME to cancel")
        badge.update()
        if badge.pressed(BUTTON_HOME):
            return None
    return found


def _no_host(app):
    draw.banner(app.theme, "No host", "nothing answered", "B retry   HOME quit")
    badge.update()
    if _wait_for(BUTTON_B, BUTTON_HOME) is BUTTON_B:
        return run(app)
    return False


MAX_HOSTS = 5


def _choose_host(app, hosts):
    """Pick from what answered. Skipped when only one host replied."""
    index = 0
    while True:
        draw_hosts(app.theme, hosts, index, app.config.hosts)
        badge.update()

        if badge.pressed(BUTTON_UP):
            index = (index - 1) % min(len(hosts), MAX_HOSTS)
        if badge.pressed(BUTTON_DOWN):
            index = (index + 1) % min(len(hosts), MAX_HOSTS)
        if badge.pressed(BUTTON_B):
            return hosts[index]
        if badge.pressed(BUTTON_HOME):
            return None


def draw_hosts(theme, hosts, index, known):
    screen.pen = theme.bg
    screen.rectangle(rect(0, 0, look.W, look.H))
    draw.blit_label("CHOOSE A HOST", look.SIZE_TITLE, theme.ink,
                    look.W // 2, 16, align=1)
    for i, found in enumerate(hosts[:MAX_HOSTS]):
        y = 56 + i * 30
        selected = i == index
        screen.pen = theme.accent if selected else theme.panel
        screen.shape(shape.rounded_rectangle(rect(24, y, look.W - 48, 26), 5))
        ink = theme.bg if selected else theme.ink
        label = found.get("name") or found["host"]
        draw.blit_label(f"{label} (paired)" if found.get("id") in known else label,
                        look.SIZE_VALUE, ink, 34, y + 4)
        draw.blit_label(f"{found['host']}:{found['port']}", look.SIZE_SMALL,
                        ink, look.W - 34, y + 7, align=2)
    draw.blit_label("UP/DOWN choose    B select    HOME quit", look.SIZE_SMALL,
                    theme.dim, look.W // 2, look.H - 18, align=1)


def _already_paired(app, chosen):
    """Whether this badge already holds credentials for this server that it can use.

    Not whether it holds any. A host that has dropped the badge sets `rejected`, and
    pairing again is then exactly the point.
    """
    server_id = chosen.get("id")
    if not server_id or not (app.config.hosts.get(server_id) or {}).get("secret"):
        return False
    return not (app.rejected and server_id == app.config.active)


def _ask_to_join(app, chosen):
    """Ask the host to let us in, show its code, and wait. True if approved and saved,
    False to go back, None to quit.

    A server this badge is already paired with is waved through instead. Setup is offered
    after a few failed polls as well as when unpaired, so this screen is easy to reach with
    nothing wrong with the pairing.

    Asking again would need the host in pairing mode, and would mint a second secret for
    one machine.
    """
    host, port = chosen["host"], chosen["port"]
    label = chosen.get("name") or host

    if _already_paired(app, chosen):
        # Its address may have moved since, which is the other thing this screen is for.
        app.config.note_address(chosen["id"], host, port, chosen.get("name"))
        app.config.switch(chosen["id"])
        draw.banner(app.theme, "Already paired", label, "using the credentials it has")
        badge.update()
        time.sleep_ms(1200)
        return True

    draw.banner(app.theme, "Asking", label)
    badge.update()
    reply, error = net.enrol(host, port, badge.uid, badge.model)
    if not reply:
        message = (error or {}).get("error") or "refused"
        draw.banner(app.theme, "Refused", message, "B retry   A back   HOME quit")
        badge.update()
        pressed = _wait_for(BUTTON_B, BUTTON_A, BUTTON_HOME)
        if pressed is BUTTON_B:
            return _ask_to_join(app, chosen)
        return False if pressed is BUTTON_A else None

    code = reply.get("code") or "??????"
    request_id = reply.get("request_id")
    next_poll = time.ticks_ms()

    while True:
        draw_code(app.theme, code, label)
        badge.update()

        if badge.pressed(BUTTON_HOME):
            return None
        if badge.pressed(BUTTON_A):
            return False

        if time.ticks_diff(time.ticks_ms(), next_poll) < 0:
            continue
        next_poll = time.ticks_add(time.ticks_ms(), POLL_INTERVAL_MS)

        outcome, error = net.enrol_status(host, port, request_id)
        if error:
            continue                # transient; the code on screen is still valid
        status = (outcome or {}).get("status")
        if status == "approved":
            return _remember(app, chosen, outcome, host, port)
        if status == "gone":
            draw.banner(app.theme, "Expired", "nobody answered in time",
                        "B ask again   A back")
            badge.update()
            pressed = _wait_for(BUTTON_B, BUTTON_A, BUTTON_HOME)
            if pressed is BUTTON_B:
                return _ask_to_join(app, chosen)
            return False if pressed is BUTTON_A else None


def _remember(app, chosen, outcome, host, port):
    app.config.badge_id = badge.uid
    # Keyed on the host id and added, so a badge can hold several. Both counters start at 0.
    app.config.remember(outcome.get("id") or chosen.get("id"), host, port,
                        outcome["secret"], outcome.get("name") or chosen.get("name"),
                        seq=0)
    if not app.config.paired:
        draw.banner(app.theme, "Approved", "but could not save",
                    "/state is not writable")
        badge.update()
        time.sleep(2)
        return True
    draw.banner(app.theme, "Paired", app.config.name or host,
                f"{len(app.config.hosts)} host(s) known")
    badge.update()
    time.sleep_ms(1200)
    return True


def draw_code(theme, code, label):
    """Draw the code and what to do with it."""
    screen.pen = theme.bg
    screen.rectangle(rect(0, 0, look.W, look.H))
    draw.blit_label("APPROVE ON THE HOST", look.SIZE_TITLE, theme.ink,
                    look.W // 2, 12, align=1)
    draw.blit_label(label, look.SIZE_SMALL, theme.dim, look.W // 2, 36, align=1)

    screen.pen = theme.accent
    screen.shape(shape.rounded_rectangle(rect(34, 60, look.W - 68, 64), 8))
    # Spaced for readability, unless that overflows the box.
    spaced = " ".join(code)
    if screen.measure_text(spaced, font_size=look.SIZE_HUGE)[0] > look.W - 90:
        spaced = code
    draw.blit_label(spaced, look.SIZE_HUGE, theme.bg, look.W // 2, 66, align=1)

    draw.blit_label("check it matches, then approve there", look.SIZE_SMALL, theme.dim,
                    look.W // 2, 136, align=1)
    draw.blit_label("A back    HOME quit", look.SIZE_SMALL, theme.dim,
                    look.W // 2, look.H - 18, align=1)


def _wait_for(*buttons):
    while True:
        badge.update()
        for button in buttons:
            if badge.pressed(button):
                return button
