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


def _choose_host(app, hosts):
    """Pick from what answered. Skipped when only one host replied."""
    index = 0
    while True:
        theme = app.theme
        screen.pen = color.rgb(*theme.bg)
        screen.rectangle(rect(0, 0, look.W, look.H))
        draw.blit_label("CHOOSE A HOST", look.SIZE_TITLE, theme.ink,
                        look.W // 2, 16, align=1)
        for i, found in enumerate(hosts[:5]):
            y = 56 + i * 30
            selected = i == index
            screen.pen = color.rgb(*(theme.accent if selected else theme.panel))
            screen.shape(shape.rounded_rectangle(rect(24, y, look.W - 48, 26), 5))
            ink = theme.bg if selected else theme.ink
            known = found.get("id") in app.config.hosts
            label = found.get("name") or found["host"]
            draw.blit_label(f"{label} (paired)" if known else label,
                            look.SIZE_VALUE, ink, 34, y + 4)
            draw.blit_label(f"{found['host']}:{found['port']}", look.SIZE_SMALL,
                            ink, look.W - 34, y + 7, align=2)
        draw.blit_label("UP/DOWN choose    B select    HOME quit", look.SIZE_SMALL,
                        theme.dim, look.W // 2, look.H - 18, align=1)
        badge.update()

        if badge.pressed(BUTTON_UP):
            index = (index - 1) % min(len(hosts), 5)
        if badge.pressed(BUTTON_DOWN):
            index = (index + 1) % min(len(hosts), 5)
        if badge.pressed(BUTTON_B):
            return hosts[index]
        if badge.pressed(BUTTON_HOME):
            return None


def _ask_to_join(app, chosen):
    """Ask the host to let us in, show its code, and wait. True if approved and saved,
    False to go back, None to quit."""
    host, port = chosen["host"], chosen["port"]
    label = chosen.get("name") or host

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
        _draw_code(app.theme, code, label)
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
    # Keyed on the host id and added, not replaced, so a badge can hold several. Both
    # counters start at 0.
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


def _draw_code(theme, code, label):
    """Draw the code and what to do with it."""
    screen.pen = color.rgb(*theme.bg)
    screen.rectangle(rect(0, 0, look.W, look.H))
    draw.blit_label("APPROVE ON THE HOST", look.SIZE_TITLE, theme.ink,
                    look.W // 2, 12, align=1)
    draw.blit_label(label, look.SIZE_SMALL, theme.dim, look.W // 2, 36, align=1)

    screen.pen = color.rgb(*theme.accent)
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
