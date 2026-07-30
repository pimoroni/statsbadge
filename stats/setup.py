"""Pairing from the badge, for when the app was installed without the USB flow.

Typing an IP address on six buttons is miserable, so the host broadcasts a beacon and
this listens for it. All that leaves to enter is the 8-character code the host shows,
picked one character at a time. The code's alphabet has no vowels and no 0/O/1/I, so
nothing in it can be misread.

Returns True to carry on into the app, False if the user backed all the way out.
"""

import time

import draw
import look
import net

ALPHABET = "23456789BCDFGHJKLMNPQRSTVWXYZ"
CODE_LENGTH = 8


def run(app):
    hosts = _find_hosts(app)
    if hosts is None:
        return False
    if not hosts:
        return _no_host(app)

    chosen = _choose_host(app, hosts)
    if chosen is None:
        return False

    code = _enter_code(app, chosen)
    if code is None:
        return False

    host, port = chosen["host"], chosen["port"]
    draw.banner(app.theme, "Pairing", f"{host}:{port}")
    badge.update()

    reply, error = net.pair(host, port, code, badge.uid)
    if not reply:
        draw.banner(app.theme, "Refused", error or "wrong code", "press A to retry")
        badge.update()
        if _wait_for(BUTTON_A, BUTTON_HOME) == BUTTON_A:
            return run(app)
        return False

    app.config.badge_id = badge.uid
    # Keyed on the server's id, not its address, and added rather than replacing: a
    # badge can be paired with several machines and follow whichever is up. A fresh
    # pairing starts the server's counter at 0, so start level with it.
    app.config.remember(reply.get("id") or chosen.get("id"), host, port,
                        reply["secret"], reply.get("name") or chosen.get("name"),
                        seq=0)
    if not app.config.paired:
        draw.banner(app.theme, "Paired", "but could not save", "/state is not writable")
        badge.update()
        time.sleep(2)
        return True
    draw.banner(app.theme, "Paired", app.config.name or host,
                f"{len(app.config.hosts)} host(s) known")
    badge.update()
    time.sleep_ms(1200)
    return True


# -- steps ------------------------------------------------------------------

def _find_hosts(app):
    """Listen for the host's beacon, with a visible countdown."""
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
    draw.banner(app.theme, "No host", "nothing answered", "A retry   HOME quit")
    badge.update()
    pressed = _wait_for(BUTTON_A, BUTTON_HOME)
    if pressed == BUTTON_A:
        return run(app)
    return False


def _choose_host(app, hosts):
    """Pick from what answered. Usually one, so this is normally a single press."""
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
        draw.blit_label("UP/DOWN choose    A select    HOME quit", look.SIZE_SMALL,
                        theme.dim, look.W // 2, look.H - 18, align=1)
        badge.update()

        if badge.pressed(BUTTON_UP):
            index = (index - 1) % min(len(hosts), 5)
        if badge.pressed(BUTTON_DOWN):
            index = (index + 1) % min(len(hosts), 5)
        if badge.pressed(BUTTON_A):
            return hosts[index]
        if badge.pressed(BUTTON_HOME):
            return None


def _enter_code(app, chosen):
    """Pick the code one character at a time. UP/DOWN change, A accepts, B deletes."""
    entered = []
    cursor = 0
    while True:
        theme = app.theme
        screen.pen = color.rgb(*theme.bg)
        screen.rectangle(rect(0, 0, look.W, look.H))
        draw.blit_label("ENTER THE CODE", look.SIZE_TITLE, theme.ink,
                        look.W // 2, 12, align=1)
        draw.blit_label(chosen[2], look.SIZE_SMALL, theme.dim, look.W // 2, 38, align=1)

        slot_w = 30
        span = CODE_LENGTH * slot_w + (CODE_LENGTH - 1) * 4
        left = (look.W - span) // 2
        for i in range(CODE_LENGTH):
            x = left + i * (slot_w + 4)
            filled = i < len(entered)
            active = i == len(entered)
            screen.pen = color.rgb(*(theme.accent if active else theme.panel))
            screen.shape(shape.rounded_rectangle(rect(x, 74, slot_w, 40), 4))
            if filled:
                draw.blit_label(entered[i], look.SIZE_BIG, theme.ink,
                                x + slot_w // 2, 80, align=1)
            elif active:
                draw.blit_label(ALPHABET[cursor], look.SIZE_BIG, theme.bg,
                                x + slot_w // 2, 80, align=1)

        draw.blit_label("UP/DOWN letter    A next    B back    HOME quit",
                        look.SIZE_SMALL, theme.dim, look.W // 2, look.H - 18, align=1)
        badge.update()

        if badge.pressed(BUTTON_UP):
            cursor = (cursor - 1) % len(ALPHABET)
        if badge.pressed(BUTTON_DOWN):
            cursor = (cursor + 1) % len(ALPHABET)
        if badge.pressed(BUTTON_A):
            entered.append(ALPHABET[cursor])
            if len(entered) == CODE_LENGTH:
                return "".join(entered)
        if badge.pressed(BUTTON_B):
            if entered:
                entered.pop()
            else:
                return None
        if badge.pressed(BUTTON_HOME):
            return None


def _wait_for(*buttons):
    while True:
        badge.update()
        for button in buttons:
            if badge.pressed(button):
                return button
