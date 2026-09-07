"""Talking to pystray, and working out whether there is anything to talk to.

pystray is imported inside functions. It brings in a toolkit, and on Linux it raises at
import where no desktop hosts a tray, so `serve` should never touch it.
"""

import collections
import importlib.util
import os
import sys
import traceback
import uuid

from .. import bundled
from . import icons

# A separator in a menu model. pystray drops runs of them and trims the ends, so a
# builder can emit one after a section that turned out to be empty.
SEPARATOR = object()

Item = collections.namedtuple(
    "Item", "label action checked enabled default submenu",
    defaults=(None, None, True, False, None))

# The notification centre keeps only a weak reference to its delegate, so the one it is
# given has to outlive the call that sets it.
_activation = None

# The alert's own button was clicked, rather than its body. AppKit's
# NSUserNotificationActivationTypeActionButtonClicked, which is not worth a framework
# binding for one integer.
ACTION_BUTTON = 2

# What each posted alert should do, by the key carried in its userInfo. An alert names one
# badge waiting, so several can be out at once and each answers for its own.
_answers = {}


def _activation_delegate():
    """The notification-centre delegate, built once and kept.

    An Objective-C class cannot be registered under the same name twice, and a collected
    delegate would leave the alert's buttons dead.
    """
    global _activation
    if _activation is None:
        from Foundation import NSObject

        class Activated(NSObject):
            def userNotificationCenter_didActivateNotification_(self, centre, note):
                # Nothing may leave here. An exception crossing back into AppKit is
                # rethrown as an NSException and takes the app down with it.
                try:
                    # pyobjc hands userInfo back as a plain dict, so read it as a mapping
                    # rather than through NSDictionary.
                    info = note.userInfo() or {}
                    key = info["statsbadge"] if "statsbadge" in info else None
                    opens, acts = _answers.pop(key, (None, None))
                    handler = acts if note.activationType() == ACTION_BUTTON else opens
                    centre.removeDeliveredNotification_(note)
                    if handler is not None:
                        handler()
                except Exception:
                    traceback.print_exc()

        _activation = Activated.alloc().init()
    return _activation


INSTALL = ("pystray is missing, though statsbadge depends on it. Reinstall:\n"
           "  uv tool install --force statsbadge")

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

    def notify(self, message, title=None, on_activate=None, action=None):
        """Say a badge is waiting, where it can be said properly. Silent where it cannot.

        The icon's attention state and the menu carry the same news, so nothing is lost by
        staying quiet, and an alert nobody can place is worse than none. What pystray does
        with one differs by platform: Windows puts a balloon on the icon itself, macOS
        shells out to `osascript` and has the alert credited to Script Editor, and Linux
        answers with NotImplementedError. Only the first of those is worth posting, and
        macOS goes through `_notify_as_app` above when it is a bundle.
        """
        if self._notify_as_app(message, title, on_activate, action):
            return
        if sys.platform == "darwin":
            return
        if not getattr(self._pystray.Icon, "HAS_NOTIFICATION", False):
            return
        try:
            self._icon.notify(message, title)
        except NotImplementedError:
            pass
        except Exception:
            traceback.print_exc()

    @staticmethod
    def _notify_as_app(message, title, on_activate=None, action=None):
        """Post the alert under this app's own name. True where it went out.

        pystray's macOS backend shells out to `osascript`, and macOS credits the alert to
        Script Editor: it arrives under a name the reader has no reason to trust, and
        opening it opens Script Editor. Only a bundle has an identity to post under, so a
        checkout still goes the long way round.

        NSUserNotification is deprecated, and its replacement is in a framework the bundle
        carries no bindings for. Whatever it does, the osascript path is still below.
        """
        if sys.platform != "darwin" or not bundled():
            return False
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter
            centre = NSUserNotificationCenter.defaultUserNotificationCenter()
            if centre is None:
                return False
            note = NSUserNotification.alloc().init()
            note.setTitle_(title or "statsbadge")
            note.setInformativeText_(message)
            # An alert carries a button whether or not anything is listening, so one with
            # nothing to run loses it rather than keeping a button that does nothing.
            if on_activate or action:
                key = str(uuid.uuid4())
                _answers[key] = (on_activate, action[1] if action else None)
                note.setUserInfo_({"statsbadge": key})
                centre.setDelegate_(_activation_delegate())
            note.setHasActionButton_(bool(action))
            if action:
                note.setActionButtonTitle_(action[0])
            centre.deliverNotification_(note)
            return True
        except Exception:
            return False

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
