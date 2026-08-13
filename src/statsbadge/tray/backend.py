"""Talking to pystray, and working out whether there is anything to talk to.

pystray is imported inside functions. `statsbadge tray --check` runs where the extra is
missing, and `serve` must not care either way.
"""

import collections
import importlib.util
import os
import sys

from . import icons

# A separator in a menu model. pystray drops runs of them and trims the ends, so a
# builder can emit one after a section that turned out to be empty.
SEPARATOR = object()

Item = collections.namedtuple(
    "Item", "label action checked enabled default submenu",
    defaults=(None, None, True, False, None))

INSTALL = ("pystray is not installed. Add the tray extra:\n"
           "  uv tool install --force 'statsbadge[tray]'")

LINUX = (
    "The tray needs the desktop's own bits, which pip cannot supply:\n"
    "  Debian, Ubuntu:  sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1\n"
    "  Fedora:          sudo dnf install python3-gobject libayatana-appindicator-gtk3\n"
    "  Arch:            sudo pacman -S python-gobject libayatana-appindicator\n"
    "A uv tool environment cannot see those, so install statsbadge into a virtualenv\n"
    "made with --system-site-packages.\n"
    "GNOME hosts no tray without an extension:\n"
    "  https://extensions.gnome.org/extension/615/appindicator-support/")


def why_not():
    """What stops a tray working here, or None."""
    try:
        if importlib.util.find_spec("pystray") is None:
            return INSTALL
    except (ImportError, ValueError):
        return INSTALL
    if sys.platform not in ("darwin", "win32") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "No DISPLAY or WAYLAND_DISPLAY. This session has no desktop to sit in."
    try:
        import pystray  # noqa: F401
    except Exception as exc:
        found = f"pystray found no tray to use here: {exc}"
        return f"{found}\n{LINUX}" if os.name == "posix" else found
    return None


def name():
    """Which pystray backend took, or None."""
    try:
        import pystray
    except Exception:
        return None
    return pystray.Icon.__module__.rpartition(".")[2].lstrip("_")


class Tray:
    """One icon, its menu rebuilt from `build` whenever the toolkit reads it."""

    def __init__(self, title, build):
        import pystray
        self._pystray = pystray
        self._build = build
        self._template = sys.platform == "darwin"
        self._attention = False
        self._icon = pystray.Icon("statsbadge", icons.load(template=self._template),
                                  title, menu=pystray.Menu(self._items))

    def run(self, setup=None):
        self._icon.run(setup=self._started(setup))

    def stop(self):
        self._icon.stop()

    def update(self):
        self._icon.update_menu()

    def title(self, text):
        self._icon.title = text

    def attention(self, wanted):
        if wanted == self._attention:
            return
        self._attention = wanted
        self._icon.icon = icons.load(attention=wanted, template=self._template)
        self._mark_template()

    def notify(self, message, title=None):
        if not getattr(self._pystray.Icon, "HAS_NOTIFICATION", False):
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def _started(self, setup):
        def ready(icon):
            icon.visible = True
            self._mark_template()
            if setup:
                setup()
        return ready

    def _items(self):
        return [self._convert(entry) for entry in self._build()]

    def _convert(self, entry):
        if entry is SEPARATOR:
            return self._pystray.Menu.SEPARATOR
        action = entry.action
        if entry.submenu is not None:
            action = self._pystray.Menu(*[self._convert(e) for e in entry.submenu])
        checked = None
        if entry.checked is not None:
            checked = lambda _item, value=entry.checked: value  # noqa: E731
        return self._pystray.MenuItem(entry.label, action, checked=checked,
                                      enabled=entry.enabled, default=entry.default)

    def _mark_template(self):
        """AppKit inverts a template image only. pystray builds the NSImage itself."""
        if not self._template:
            return
        try:
            image = self._icon._icon_image
            if image is not None:
                image.setTemplate_(True)
        except Exception:
            pass
