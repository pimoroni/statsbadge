"""Pairing from the badge, for when the app was installed without the USB flow.

Typing an IP address on six buttons is miserable, so the host broadcasts a beacon and
this listens for it. All that leaves is the short numeric code the host shows, spun in
one digit at a time - the host reports the code's length and alphabet in /v1/hello, so
this does not carry its own copy of them.

The three buttons along the bottom are used in the order they sit in: A back, B select,
C next. UP/DOWN change a value. HOME quits, and is the only key that abandons anything -
moving around the code wraps rather than deleting, so no press can lose work.

Returns True to carry on into the app, False if the user backed all the way out.
"""

import time

import draw
import look
import net

# Fallbacks only. The host reports the real values in /v1/hello, so there is one
# source of truth and this cannot drift out of step with the server.
ALPHABET = "0123456789"
CODE_LENGTH = 6


def run(app):
    hosts = _find_hosts(app)
    if hosts is None:
        return False
    if not hosts:
        return _no_host(app)

    chosen = _choose_host(app, hosts)
    if chosen is None:
        return False

    # Ask the host what its code looks like, so the entry screen matches whatever the
    # server is actually generating.
    greeting = net.hello(chosen["host"], chosen["port"]) or {}
    alphabet = greeting.get("code_alphabet") or ALPHABET
    length = int(greeting.get("code_length") or CODE_LENGTH)
    if greeting.get("name"):
        chosen["name"] = greeting["name"]
    if greeting.get("id"):
        chosen["id"] = greeting["id"]

    host, port = chosen["host"], chosen["port"]

    # Keep the digits and the chosen host across a refusal. One mistyped digit should
    # cost one digit, not the whole code and a walk back through host discovery.
    digits = None
    while True:
        digits = _enter_code(app, chosen, alphabet, length, digits)
        if digits is None:
            return False

        draw.banner(app.theme, "Pairing", chosen.get("name") or host)
        badge.update()
        reply, error = net.pair(host, port, "".join(digits), badge.uid)
        if reply:
            break

        # The host rate limits guesses and says how long to wait, so sit the wait out
        # here: the alternative is the next attempt being refused for being early.
        error = error or {}
        if not _after_refusal(app, error.get("error"),
                              float(error.get("retry_after") or 0)):
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
    draw.banner(app.theme, "No host", "nothing answered", "B retry   HOME quit")
    badge.update()
    if _wait_for(BUTTON_B, BUTTON_HOME) is BUTTON_B:
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


HOLD_DELAY_MS = 320
HOLD_INTERVAL_MS = 110


class _Scroller:
    """UP/DOWN as a spinner: one press steps once, holding repeats.

    Without the repeat, entering a code is a press per step - about 27 for six digits,
    and three times that for an alphabet of twenty-nine. Holding turns the whole thing
    into a couple of seconds and is why the code does not need to be short enough to
    tap out one press at a time.
    """

    def __init__(self):
        self.held_button = None
        self.next_at = 0

    def delta(self):
        for button, step in ((BUTTON_UP, -1), (BUTTON_DOWN, 1)):
            if badge.pressed(button):
                self.held_button = button
                self.next_at = badge.ticks + HOLD_DELAY_MS
                return step
        if self.held_button is not None:
            step = -1 if self.held_button is BUTTON_UP else 1
            if not badge.held(self.held_button):
                self.held_button = None
            elif badge.ticks >= self.next_at:
                self.next_at = badge.ticks + HOLD_INTERVAL_MS
                return step
        return 0


def _enter_code(app, chosen, alphabet=ALPHABET, length=CODE_LENGTH, digits=None):
    """Edit the code in place. UP/DOWN spin the slot, A back, C next, B sends.

    A field of slots rather than an append-only list, so moving is navigation and never
    deletion: go back to fix one digit and the ones after it are still there, and a
    refused code comes back with everything in it. A and C wrap around, so there is no
    press that discards the code - only HOME does that, and it says so.
    """
    if digits and len(digits) == length:
        slots = list(digits)
    else:
        slots = [alphabet[0]] * length
    position = 0
    scroller = _Scroller()

    while True:
        theme = app.theme
        screen.pen = color.rgb(*theme.bg)
        screen.rectangle(rect(0, 0, look.W, look.H))
        draw.blit_label("ENTER THE CODE", look.SIZE_TITLE, theme.ink,
                        look.W // 2, 10, align=1)
        draw.blit_label(chosen.get("name") or chosen["host"], look.SIZE_SMALL,
                        theme.dim, look.W // 2, 34, align=1)

        slot_w = 30 if length > 6 else 36
        gap = 5
        span = length * slot_w + (length - 1) * gap
        left = (look.W - span) // 2
        top = 62
        cursor = alphabet.index(slots[position]) if slots[position] in alphabet else 0
        for i in range(length):
            x = left + i * (slot_w + gap)
            active = i == position
            screen.pen = color.rgb(*(theme.accent if active else theme.panel))
            screen.shape(shape.rounded_rectangle(rect(x, top, slot_w, 52), 4))
            middle = x + slot_w // 2
            if active:
                # The neighbours, so which way to spin is visible rather than a guess.
                draw.blit_label(alphabet[(cursor - 1) % len(alphabet)],
                                look.SIZE_SMALL, theme.bg, middle, top + 1, align=1)
                draw.blit_label(slots[i], look.SIZE_BIG, theme.bg,
                                middle, top + 12, align=1)
                draw.blit_label(alphabet[(cursor + 1) % len(alphabet)],
                                look.SIZE_SMALL, theme.bg, middle, top + 40, align=1)
            else:
                draw.blit_label(slots[i], look.SIZE_BIG, theme.ink,
                                middle, top + 12, align=1)

        draw.blit_label("UP/DOWN spin (hold to run)   A back   B send   C next",
                        look.SIZE_SMALL, theme.dim, look.W // 2, look.H - 18, align=1)
        badge.update()

        step = scroller.delta()
        if step:
            slots[position] = alphabet[(cursor + step) % len(alphabet)]
        if badge.pressed(BUTTON_A):
            position = (position - 1) % length
        if badge.pressed(BUTTON_C):
            position = (position + 1) % length
        if badge.pressed(BUTTON_B):
            return slots
        if badge.pressed(BUTTON_HOME):
            return None


def _after_refusal(app, error, wait):
    """Show why a code was refused and sit out any rate-limit wait.

    Returns True to go back to the editor with the digits intact, False to give up.
    """
    theme = app.theme
    deadline = time.ticks_add(time.ticks_ms(), int(wait * 1000))
    while wait > 0:
        remaining = time.ticks_diff(deadline, time.ticks_ms())
        if remaining <= 0:
            break
        draw.banner(theme, "Refused", error or "wrong code",
                    f"retry in {remaining // 1000 + 1}s   HOME quit")
        badge.update()
        if badge.pressed(BUTTON_HOME):
            return False
    draw.banner(theme, "Refused", error or "wrong code", "B edit the code   HOME quit")
    badge.update()
    return _wait_for(BUTTON_B, BUTTON_HOME) is BUTTON_B


def _wait_for(*buttons):
    while True:
        badge.update()
        for button in buttons:
            if badge.pressed(button):
                return button
