"""What an extension's badge module may use from the app, at badge API `VERSION`.

A module registers its page kind with `pages.register(kind, render, api=VERSION)`, and
`render(page, frame, history, theme)` draws it. Anything else it reaches for in the app's
modules is listed here, and a test holds the app and every known extension to the list.
A change to any of it that is not an addition moves `VERSION`.
"""

VERSION = 1

NAMES = {
    "draw": frozenset({
        "CAP", "LINE_FLAGS", "TEXT", "add_font", "ago", "blit_label", "column_lines",
        "curve", "fit", "has_font", "icon_baseline", "readable", "text_width",
    }),
    "look": frozenset({
        "APP_DIR", "BODY_H", "BODY_TOP", "DIAL_C", "DIAL_OUTER", "PAD", "READOUT_X",
        "SIZE_BIG", "SIZE_LABEL", "SIZE_SMALL", "SIZE_VALUE", "W",
    }),
    "pages": frozenset({"register"}),
    "worldmap": frozenset({"ASPECT", "View", "ready", "shortest"}),
}

# What `render` may read off the theme it is handed.
THEME = frozenset({"accent", "accent_b", "at", "bg", "dim", "grid", "ink", "key", "panel"})
