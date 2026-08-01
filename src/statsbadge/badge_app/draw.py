"""Drawing the pages.

Vector shapes throughout, so a theme is a colour table. The costs that shape this
(measured on this board, in BADGEWARE.md): an anti-aliased shape is about a quarter
of a millisecond whatever its size, a line of live text is about 1ms, and the same
text blitted from a cache is 0.08ms. So shapes are drawn live and every string that
is not new every frame is baked into a sprite once.

The header and footer change only when the page or the theme does, so they are baked
into two band images per page and blitted over a raster fill of the body.

A page that splits into something round and a column of text beside it - the single dial,
the ring stack, an extension's clock face - takes its geometry from `look.DIAL_C`,
`look.DIAL_OUTER` and `look.READOUT_X`, and its rows from `look.readout_rows` and either
`readout` or `column_lines` here. Nothing in a split page should place text on a number of
its own: the pages are paged between, and anything choosing its own margin moves under the
reader when they press a button.
"""

import os

import look

FONT = None
_labels = {}
_bands = {}

# Fonts by name, so a sprite cache key can say which one drew it. TEXT is the app's own;
# `icons` is registered by prepare() when the .af is there, and an extension adds its own
# with add_font. Without the name in the key an icon and a letter of the same string would
# collide, and one of them would be drawn in the wrong font.
TEXT = "text"
ICONS = "icons"
DIGITS = "digits"
_fonts = {}

# Units a capital stands in a font built the ordinary way, which is what a size means:
# ../BADGEWARE.md, and tools/make_text_font.py's --cap. A font built taller is finer, a
# point being a signed byte either way, and _font_cap is what lets a caller go on asking
# for the size it wants whichever one it is drawing with.
CAP_UNITS = 81
_font_cap = {}


def prepare():
    """Load the fonts. The text font is 107ms, so once, and before the first frame."""
    global FONT
    if FONT is None:
        if not add_font(look.FONT_NAME, look.FONT_FILE):
            print(f"draw: no {look.FONT_FILE}, falling back to the firmware's font")
            add_font(look.FONT_NAME, look.FALLBACK_FONT_PATH)
        FONT = _fonts.get(look.FONT_NAME)
    # TEXT is the role; look.FONT_NAME is which font is filling it, so use_font can name
    # either and a candidate can be tried against the shipped one.
    _fonts[TEXT] = FONT
    _fonts[look.FONT_NAME] = FONT
    screen.font = FONT
    add_font(ICONS, look.ICON_FILE)
    # Text drawn large wobbles: a point is a signed byte, so at 93pt one unit is 0.8px and
    # every vertex on a curve is snapped that far. The digits are also packed at a finer
    # grid, for the pages that draw a number the height of the band. An install predating
    # the file falls back to the text font, which is the same shapes with the wobble.
    if not add_font(DIGITS, look.DIGITS_FILE, cap=look.DIGITS_CAP):
        _fonts[DIGITS] = FONT
        _font_cap[DIGITS] = CAP_UNITS


def add_font(name, *paths, cap=CAP_UNITS):
    """Register a font under a name, from the first of `paths` that loads.

    `cap` is the units a capital stands in that file, 81 for one built the ordinary way.
    Sizes are quoted in those terms whatever the font, so a caller asks for the size it
    wants and `label` converts: a font packed at a finer grid is a drop-in replacement.

    A bare filename is looked for in the installed app directory and then beside this
    module. That order matters under `mpremote mount`: the mounted copy is served as text,
    so a font loaded from it comes back mangled rather than refused. An extension passes
    full paths, because its own file lives in ext/ and may share a name with the app's.

    A missing file is not an error. An install predating the font has none, and a page that
    wanted an icon falls back to its words. Anything that raises is reported and then
    treated the same way, because a font is not worth a crash dialog.
    """
    if name in _fonts:
        return True
    for path in paths:
        for candidate in _candidates(path):
            try:
                os.stat(candidate)
            except OSError:
                continue
            try:
                _fonts[name] = font.load(candidate)
            except Exception as exc:  # noqa: BLE001  try the next one
                print(f"draw: could not load {candidate}: {exc}")
                continue
            _font_cap[name] = cap
            return True
    return False


def _drawn_size(name, size):
    """The size to ask a font for, so `size` means the same height in every one of them."""
    cap = _font_cap.get(name, CAP_UNITS)
    return size if cap == CAP_UNITS else size * CAP_UNITS / cap


def _candidates(path):
    """Where to look for a font. An absolute path is taken as given; anything else is
    relative to the app, which is `fonts/x.af` for what the app ships.

    A path under /remote is never offered. That is a `mpremote mount`, which serves a file
    as text, so font.load reads it as UTF-8, fails partway and takes the REPL down with it -
    a wedged badge rather than a caught error. Under mount the device copy is what gets
    found, or the fallback, which is why /fonts is on the list: it is writable, so the tools
    can put a font somewhere loadable without an install.
    """
    if path.startswith("/"):
        return (path,)
    here = globals().get("__file__") or ""
    beside = here.rsplit("/", 1)[0] if "/" in here else ""
    found = [look.APP_DIR + "/" + path, "/" + path]
    if beside and not beside.startswith("/remote"):
        found.append(beside + "/" + path)
    found.append(path)
    return [candidate for candidate in found if not candidate.startswith("/remote")]


def has_font(name):
    return name in _fonts


def use_font(name):
    """Draw text with a registered font from here on. True when it is there.

    The sprite cache keys on the name a caller asked for and not on the font behind it, so
    changing what TEXT points at has to empty the cache or the last font's sprites are
    handed back for every string already drawn once.
    """
    global FONT
    face = _fonts.get(name)
    if face is None:
        return False
    _fonts[TEXT] = face
    FONT = face
    screen.font = face
    clear_cache()
    return True


# -- text cache -------------------------------------------------------------

# What the baked strings may hold between them. A screen of furniture is a few tens of KB;
# the ceiling is for the pages that bake something enormous.
LABEL_CACHE_BYTES = 768 * 1024
_label_bytes = 0


def label(text_value, size, rgb, name=TEXT):
    """A string baked into a sprite. Live text is ~1ms a line, a blit is 0.08ms."""
    key = (name, text_value, size, rgb)
    cached = _labels.get(key)
    if cached is not None:
        return cached
    face = _fonts.get(name)
    if face is None:
        return None
    was = screen.font
    screen.font = face
    try:
        drawn = _drawn_size(name, size)
        width, height = screen.measure_text(text_value, font_size=drawn)
        width = max(1, int(width + 2))
        height = max(1, int(size * 1.35))
        sprite = image(width, height)
        sprite.font = face
        sprite.pen = brush.erase()
        sprite.rectangle(rect(0, 0, width, height))
        sprite.antialias = image.X4
        sprite.pen = color.rgb(*rgb)
        # The baseline lands `size` from the top whatever the font's own grid, which is what
        # keeps every placement rule here in one set of terms.
        sprite.text(text_value, vec2(0, size - drawn), drawn)
    finally:
        screen.font = was
    global _label_bytes
    _label_bytes += width * height * 4
    if len(_labels) > 220 or _label_bytes > LABEL_CACHE_BYTES:
        # Values churn; the cache is for furniture, so drop it wholesale rather than
        # tracking ages. Bounded by bytes as well as by count, because a page drawing a
        # number the height of the band bakes 400KB a minute and would otherwise hold every
        # minute it had drawn.
        _labels.clear()
        _label_bytes = 0
    _labels[key] = sprite
    return sprite


def blit_label(text_value, size, rgb, x, y, align=0, name=TEXT):
    """Draw a cached string. align 0 left, 1 centre, 2 right, about x.

    Returns the width drawn, or 0 for a font that is not loaded, which is what lets a
    caller offer an icon and fall back to words without asking first.
    """
    sprite = label(text_value, size, rgb, name)
    if sprite is None:
        return 0
    if align == 1:
        x -= sprite.width // 2
    elif align == 2:
        x -= sprite.width
    screen.blit(sprite, vec2(int(x), int(y)))
    return sprite.width


def blit_icon(character, size, rgb, x, y, align=0):
    """Draw one symbol from the icon font. 0 if there is no icon font."""
    return blit_label(character, size, rgb, x, y, align, ICONS)


def clear_cache():
    global _label_bytes
    _labels.clear()
    _bands.clear()
    _label_bytes = 0


# -- measuring --------------------------------------------------------------

# Between a measured column and whatever sits next to it.
COLUMN_GAP = 8


def text_width(text_value, size, name=TEXT):
    """How wide a string will be drawn, so a column can be fitted to it.

    Plus the pixel `label` adds, so a measurement and the sprite it describes agree.
    """
    face = _fonts.get(name)
    if face is None:
        return 0
    was = screen.font
    screen.font = face
    try:
        width, _ = screen.measure_text(text_value, font_size=_drawn_size(name, size))
    finally:
        screen.font = was
    return int(width) + 2


# Type metrics, as fractions of the size a string is drawn at. A capital stands CAP above
# the baseline (../BADGEWARE.md), and tools/make_icon_font.py fits every icon to a box of
# 100 units sat on the baseline, where a capital is 81 - so an icon's box is a fifth taller
# than a capital and its ink is centred half way up it.
CAP = 0.68
ICON_BOX = CAP * 100.0 / 81.0


def icon_baseline(text_y, text_size, icon_size):
    """Where to draw an icon so it centres on the capitals of text drawn at `text_y`.

    Not on a shared baseline: the icon's box stands taller than a capital and its ink sits
    in the middle of that box, so sharing a baseline leaves the symbol floating above the
    words - 4.5px at 32 beside 26pt.

    Against the capitals and not the string's own extent, because a diacritic or a
    descender would otherwise move the symbol: 16°C would sit lower than 16C for no reason
    the reader can see.
    """
    cap_middle = text_y + text_size * (1.0 - CAP / 2.0)
    return int(cap_middle - icon_size * (1.0 - ICON_BOX / 2.0))


def column_width(texts, size, name=TEXT):
    """How wide a column of these strings has to be.

    A page that puts names down one side and readings down the other cannot know either
    width in advance: the names are whatever the fields are called and a reading is
    whatever its unit makes it. Measured, a row reflows instead of leaving a gap at one
    end and running off the other.
    """
    return max((text_width(text_value, size, name) for text_value in texts), default=0)


# -- formatting -------------------------------------------------------------

def fmt(value, field):
    """A number as a badge should show it: short, and never wider than its box."""
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if field.endswith("_bps"):
        return _rate(value)
    if field.endswith("_mb"):
        return _size(value)
    if field in ("uptime_s", "secs_left"):
        return _duration(value)
    if field in ("freq", "clock", "rpm", "procs"):
        return f"{value:.0f}"
    if isinstance(value, float):
        return f"{value:.0f}" if value >= 100 else f"{value:.1f}"
    return str(value)


def _rate(bps):
    """A throughput, scaled to the largest prefix it fills.

    The prefix is part of the number and `short_unit` supplies the B/s after it, so the
    two together read 512B/s, 800KB/s, 11.4MB/s, 1.2GB/s.
    """
    if bps >= 1024 * 1024 * 1024:
        return f"{bps / (1024.0 ** 3):.1f}G"
    if bps >= 1024 * 1024:
        return f"{bps / (1024.0 ** 2):.1f}M"
    if bps >= 1024:
        return f"{bps / 1024.0:.0f}K"
    return f"{bps:.0f}"


def _size(megabytes):
    """A size, given in megabytes, scaled the same way a rate is. A 2TB disk reads 2.0T
    where the megabyte figure alone would have said 2097152."""
    if megabytes >= 1024 * 1024:
        return f"{megabytes / (1024.0 ** 2):.1f}T"
    if megabytes >= 1024:
        return f"{megabytes / 1024.0:.1f}G"
    return f"{megabytes:.0f}M"


def _duration(seconds):
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 60}m"


def short_unit(field):
    """What follows the number.

    A byte figure is scaled by `fmt`, which leaves the prefix on the number, so the unit
    here is only the base: 11.4M and MB/s make 11.4MB/s, and the same unit serves the
    reading whatever size it has grown to.
    """
    if field.endswith("_bps"):
        return "B/s"
    if field in ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct", "cores"):
        return "%"
    if field == "temp":
        return "°C"
    if field in ("power", "package_w"):
        return "W"
    if field in ("freq", "clock"):
        return "MHz"
    if field.endswith("_mb"):
        return "B"
    return ""


def reading(value, field):
    """A value with its unit, for a slot that has no room to place one separately.

    `fmt` already carries a prefix where the number was scaled - 12.3G, 50.0M - and
    short_unit adds the base after it, so a rate reads 50.0MB/s and a percentage reads
    9.2%. A reading that is not there gets no unit: there is no such thing as
    "-- percent".
    """
    text = fmt(value, field)
    if value is None or isinstance(value, (str, bool)):
        return text
    return text + short_unit(field)


# -- chrome -----------------------------------------------------------------

def background(theme, title, index, total, subtitle=None):
    """The header and footer, baked per page and blitted as two bands.

    Only the two bands are baked, not a whole screen: a full-screen 1:1 blit is 14ms,
    against 0.5ms for a raster fill of the body plus two small blits. Baking bands
    also makes them cheap enough to keep several, so paging back and forth does not
    re-bake - a cold page cost 90ms with a single full-screen slot, which is a visible
    hitch on every button press.
    """
    key = (theme.name, title, index, total, subtitle)
    bands = _bands.get(key)
    if bands is None:
        bands = _bake_bands(theme, title, index, total, subtitle)
        if len(_bands) > 12:
            _bands.clear()
        _bands[key] = bands
    screen.pen = color.rgb(*theme.bg)
    screen.rectangle(rect(0, look.HEADER_H, look.W, look.BODY_H))
    screen.blit(bands[0], vec2(0, 0))
    screen.blit(bands[1], vec2(0, look.H - look.FOOTER_H))


def _bake_bands(theme, title, index, total, subtitle):
    header = image(look.W, look.HEADER_H)
    header.font = FONT
    header.antialias = image.X4
    header.pen = color.rgb(*theme.panel)
    header.rectangle(rect(0, 0, look.W, look.HEADER_H))
    header.pen = color.rgb(*theme.accent)
    header.rectangle(rect(0, look.HEADER_H - 2, look.W, 2))
    header.pen = color.rgb(*theme.ink)
    header.text(title.upper(), vec2(look.PAD, 4), look.SIZE_TITLE)
    if subtitle:
        header.pen = color.rgb(*theme.dim)
        width, _ = header.measure_text(subtitle, font_size=look.SIZE_SMALL)
        header.text(subtitle, vec2(look.W - look.PAD - width, 10), look.SIZE_SMALL)

    footer = image(look.W, look.FOOTER_H)
    footer.antialias = image.X4
    footer.pen = color.rgb(*theme.panel)
    footer.rectangle(rect(0, 0, look.W, look.FOOTER_H))
    if total > 1:
        _pips(footer, theme, index, total)
    return (header, footer)


# The pips have this much of the width to themselves. A dash shortens as they pack in,
# down to a dot and no further: a mark thinner than it is tall stops reading as a mark.
PIP_ROOM = look.W - look.PAD * 4
PIP_MAX_W, PIP_GAP, PIP_DOT, PIP_TIGHT = 14, 5, 4, 2


def _pips(footer, theme, index, total):
    """One pip per page, the current one in the accent colour.

    Shortens to fit, and tightens the spacing before it gives up any more length. Enough
    pages to fill the row even as dots is tough luck: it is a badge with six buttons and
    nobody is paging through forty screens.
    """
    gap = PIP_GAP
    pip_w = min(PIP_MAX_W, (PIP_ROOM - (total - 1) * gap) // total)
    if pip_w < PIP_DOT:
        gap = PIP_TIGHT
        pip_w = max(PIP_DOT, min(PIP_MAX_W, (PIP_ROOM - (total - 1) * gap) // total))

    y = look.FOOTER_H // 2 - 2
    span = total * pip_w + (total - 1) * gap
    x = (look.W - span) // 2
    for i in range(total):
        footer.pen = color.rgb(*(theme.accent if i == index else theme.grid))
        footer.shape(shape.rounded_rectangle(
            rect(x + i * (pip_w + gap), y, pip_w, 4), min(2, pip_w // 2)))


# -- widgets ----------------------------------------------------------------

def gauge(theme, centre, outer, inner, fraction, value_text, under=None,
          value_size=None, label_size=None, cold=False, icon=None, unit=None):
    """One sweep gauge, with a line of text inside it.

    `shape.arc(centre, inner, outer, from, to)` - angles start at the top and run
    clockwise, so look.DIAL_FROM..DIAL_TO is 225..495 and the gap lands at the bottom,
    which is where `under` goes.

    `icon` is drawn there instead where the font has it, and `under` is what it falls back
    to. `unit` is a small suffix on the reading, for a gauge whose slot below is already
    spoken for - the single dial puts its unit below instead, having nothing else to put
    there. It is dropped rather than allowed to spill out of a small ring.
    """
    value_size = value_size or look.SIZE_HUGE
    label_size = label_size or look.SIZE_LABEL
    middle = vec2(*centre)
    start, end = look.DIAL_FROM, look.DIAL_TO
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, fraction))

    screen.pen = color.rgb(*theme.grid)
    screen.shape(shape.arc(middle, inner, outer, start, end))

    if not cold and fraction > 0.001:
        sweep = start + (end - start) * fraction
        # Solid, in the ramp's colour for this value: a spatial gradient across the
        # arc's box does not follow the curve, so the hue would not track the reading.
        # This way the colour *is* the severity, and it costs one shape.
        screen.pen = color.rgb(*theme.at(fraction))
        screen.shape(shape.arc(middle, inner, outer, start, sweep))

        # A brighter tick at the sweep's end, so the exact value is readable.
        screen.pen = color.rgb(*theme.ink)
        screen.shape(shape.arc(middle, inner - 3, outer + 3, sweep - 1.4, sweep + 1.4))

    ink = theme.dim if cold else theme.ink
    top = centre[1] - value_size * 0.62
    unit_size = max(look.SIZE_SMALL, int(value_size * 0.45))
    reading = label(value_text, value_size, ink)
    suffix = label(unit, unit_size, theme.dim) if unit else None
    if suffix and reading.width + suffix.width > inner * 2 - 4:
        # Kept inside the ring rather than allowed over the arc, so a gauge too small for
        # its unit shows the reading and the name under it and nothing else. A scaled
        # figure carries its prefix on the number, so 11.0M still says which 11 it is.
        suffix = None
    width = reading.width + (suffix.width if suffix else 0)
    left = centre[0] - width // 2
    screen.blit(reading, vec2(int(left), int(top)))
    if suffix:
        # Sat on the reading's own baseline, which is where the eye expects a unit. A
        # sprite puts its baseline `size` from the top, so the drop is the size difference
        # and not the difference in sprite heights.
        screen.blit(suffix, vec2(int(left + reading.width),
                                 int(top + value_size - unit_size)))
    below = centre[1] + value_size * 0.42
    if icon and blit_icon(icon, label_size + 8, theme.dim, centre[0], below, align=1):
        return
    if under:
        blit_label(under, label_size, theme.dim, centre[0], below, align=1)


def dial(theme, fraction, value_text, unit_text, cold=False):
    """The single gauge of a `dial` page, with its readouts beside it."""
    gauge(theme, look.DIAL_C, look.DIAL_OUTER, look.DIAL_INNER, fraction, value_text,
          unit_text, cold=cold)


def dials(theme, entries):
    """Up to four gauges across the body band, each named under its reading.

    One page kind rather than one per count: how many fields the page carries is the only
    thing that changes, so it picks the layout and nothing else has to be decided.
    """
    shape_of = look.DIALS.get(len(entries)) or look.DIALS[4]
    for centre, entry in zip(shape_of["centres"], entries):
        name, value_text, fraction, icon, unit = entry
        gauge(theme, centre, shape_of["outer"], shape_of["inner"], fraction, value_text,
              name, shape_of["value"], shape_of["label"], fraction is None, icon, unit)


def readout(theme, y, name, value_text, fraction=None, note=None, chip=None):
    """One row of the column beside a gauge: a name, the reading under it, and then either
    a bar for the level or a line saying what full is.

    Every page that draws a gauge and a column draws this row, so nothing moves when you
    page between them. `chip` is the colour of the ring a row belongs to, which is what ties
    the two together on a page where the gauge is not the bar.
    """
    x = look.READOUT_X
    blit_label(name, look.SIZE_SMALL, theme.dim, x, y)
    blit_label(value_text, look.SIZE_VALUE, theme.ink, x, y + 10)
    if chip:
        screen.pen = color.rgb(*chip)
        screen.rectangle(rect(x + look.READOUT_W - 10, y + 3, 10, 10))
    if note:
        # What a full ring is, for a reading whose scale is not a round number. It takes
        # the bar's place: the ring it belongs to is already the bar.
        blit_label(note, look.SIZE_SMALL, theme.dim, x, y + 29)
    elif fraction is not None:
        width = look.READOUT_W
        fraction = max(0.0, min(1.0, fraction))
        screen.pen = color.rgb(*theme.grid)
        screen.rectangle(rect(x, y + 28, width, 3))
        if fraction > 0:
            screen.pen = color.rgb(*theme.at(fraction))
            screen.rectangle(rect(x, y + 28, int(width * fraction), 3))


# Between one line of a free-form column and the next, on top of the line's own height.
COLUMN_LEAD = 3


def column_lines(entries, top=None, align=0):
    """A stack of lines down the column beside a gauge, each `(text, size, rgb)`.

    For a page whose rows are not readouts - a clock's time, place and date - so that it
    gets the column's left edge and a consistent rhythm without working either out. Empty
    strings are skipped, so a caller can offer a line it may not have.

    Returns the y after the last line, for a page that has more to place by hand.
    """
    y = (look.BODY_TOP + 12) if top is None else top
    x = look.READOUT_X + (look.READOUT_W if align == 2 else 0)
    for text_value, size, rgb in entries:
        if not text_value:
            continue
        blit_label(text_value, size, rgb, x, y, align=align)
        y += int(size * 1.35) + COLUMN_LEAD
    return y


def bars(theme, values, maximum=100.0, field="pct"):
    """A stack of horizontal bars. Raster rectangles: no AA needed on an axis-aligned
    bar, and this is the one page that can have 32 of them.

    `field` is what the values are, so a per-core load reads as a percentage. Without it
    every bar was a bare number.
    """
    if not values:
        return
    count = min(len(values), 16)
    top = look.BODY_TOP + 6
        # Fit the band whatever the core count, with at least a pixel between bars.
    slot = max(6, (look.BODY_H - 12) // count)
    height = max(4, slot - 3)
    names = [f"{i}" for i in range(count)]
    readings = [reading(values[i] or 0.0, field) for i in range(count)]
    # Both columns are as wide as their own widest entry: the index runs to two digits and
    # a reading is whatever its unit makes it, so a fixed column either leaves a gap or
    # runs the readings into the bars.
    label_w = column_width(names, look.SIZE_SMALL)
    value_w = column_width(readings, look.SIZE_SMALL)
    x = look.PAD + label_w + COLUMN_GAP
    width = max(20, look.W - x - COLUMN_GAP - value_w - look.PAD)

    for i in range(count):
        value = values[i] or 0.0
        fraction = max(0.0, min(1.0, value / maximum if maximum else 0.0))
        y = top + i * slot
        blit_label(names[i], look.SIZE_SMALL, theme.dim, look.PAD, y - 1)
        screen.pen = color.rgb(*theme.grid)
        screen.rectangle(rect(x, y, width, height))
        if fraction > 0:
            screen.pen = color.rgb(*theme.at(fraction))
            screen.rectangle(rect(x, y, max(1, int(width * fraction)), height))
        blit_label(readings[i], look.SIZE_SMALL, theme.ink,
                   look.W - look.PAD, y - 1, align=2)


def graph(theme, series, labels, maximum=None):
    """One or two series over time, as filled areas.

    Each series is one `shape.custom` contour: a polyline across the top and back
    along the bottom. One shape is one anti-aliased edge and one setup cost, where a
    line per sample would be dozens.
    """
    peak = maximum
    if peak is None:
        peak = max((max(s) for s in series if s), default=1.0)
    peak = max(peak, 1.0) * 1.15

    field = labels[0][1] if labels else "pct"
    peak_text = reading(peak, field)
    # The gutter holds the scale, which is as wide as the scale is: 100% and 9.8MB/s do
    # not need the same room.
    left = look.PAD + column_width((peak_text, "0"), look.SIZE_SMALL) + 4
    top = look.BODY_TOP + 8
    width = look.W - left - look.PAD
    height = look.BODY_H - 26

    screen.pen = color.rgb(*theme.grid)
    for i in range(5):
        y = top + int(height * i / 4.0)
        screen.hspan(left, y, width)

    for index, points in enumerate(series):
        if not points or len(points) < 2:
            continue
        rgb = _series_colour(theme, index)
        step = width / float(len(points) - 1)
        contour = []
        for i, value in enumerate(points):
            fraction = max(0.0, min(1.0, (value or 0.0) / peak))
            contour.append(vec2(left + i * step, top + height - height * fraction))
        contour.append(vec2(left + width, top + height))
        contour.append(vec2(left, top + height))
        area = shape.custom(contour)
        screen.alpha = 150 if index else 200
        screen.pen = color.rgb(*rgb)
        screen.shape(area)
    screen.alpha = 255

    # Scale and legend.
    blit_label(peak_text, look.SIZE_SMALL, theme.dim, look.PAD, top - 4)
    blit_label("0", look.SIZE_SMALL, theme.dim, look.PAD, top + height - 8)
    for index, (name, _field) in enumerate(labels[:2]):
        rgb = _series_colour(theme, index)
        x = left + index * 110
        y = look.H - look.FOOTER_H - 14
        screen.pen = color.rgb(*rgb)
        screen.rectangle(rect(x, y + 3, 10, 4))
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 14, y - 2)


def _series_colour(theme, index):
    """Colours for the two graph series: the accent, and whichever end of the ramp is
    furthest from it.

    The two areas overlap and are drawn semi-transparent, so a near miss reads as one
    series and takes the legend with it. Which end is further depends on the theme:
    the default theme's teal accent takes the hot end, mono's near-white the cold one.
    """
    if index == 0:
        return theme.accent
    cold, hot = theme.at(0.0), theme.at(1.0)
    return cold if _apart(theme.accent, cold) >= _apart(theme.accent, hot) else hot


def _apart(a, b):
    """How far apart two colours are, as squared RGB distance."""
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def grid(theme, entries):
    """Up to six labelled figures in two rows, each in its own panel."""
    if not entries:
        return
    count = min(len(entries), 6)
    columns = 3 if count > 4 else max(1, min(count, 2)) if count <= 2 else 2
    if count in (3, 4):
        columns = 2
    if count > 4:
        columns = 3
    rows = (count + columns - 1) // columns
    cell_w = (look.W - look.PAD * 2 - (columns - 1) * 6) // columns
    cell_h = (look.BODY_H - 12 - (rows - 1) * 6) // rows

    for i in range(count):
        name, value_text, fraction, icon = entries[i]
        column = i % columns
        row = i // columns
        x = look.PAD + column * (cell_w + 6)
        y = look.BODY_TOP + 6 + row * (cell_h + 6)
        screen.pen = color.rgb(*theme.panel)
        screen.shape(shape.rounded_rectangle(rect(x, y, cell_w, cell_h), 5))
        if fraction is not None:
            screen.pen = color.rgb(*theme.at(max(0.0, min(1.0, fraction))))
            screen.rectangle(rect(x, y + cell_h - 3, int(cell_w * max(0.0, min(1.0, fraction))), 3))
        # Both: a cell has room for the name and for a symbol in the far corner, so the
        # symbol is another way to find the tile rather than the only one. A gauge has
        # room for one or the other and takes the symbol.
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 7, y + 5)
        if icon:
            blit_icon(icon, look.SIZE_VALUE, theme.dim, x + cell_w - 7, y + 4, align=2)
        size = look.SIZE_BIG if rows < 3 else look.SIZE_VALUE
        blit_label(value_text, size, theme.ink, x + 7, y + cell_h // 2 - size // 2 + 2)


def lines(theme, entries):
    """Labelled lines, for names and versions."""
    y = look.BODY_TOP + 10
    for name, value_text in entries[:7]:
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD, y + 3)
        blit_label(value_text, look.SIZE_VALUE, theme.ink, look.W - look.PAD, y,
                   align=2)
        y += 24
        screen.pen = color.rgb(*theme.grid)
        screen.hspan(look.PAD, y - 5, look.W - look.PAD * 2)


def banner(theme, title, message, detail=None):
    """A full-screen notice: connecting, no host, an error.

    The box is sized to its lines rather than fixed, because these strings carry
    whatever the network had to say and a fixed box clips the useful part.
    """
    lines = [(title, look.SIZE_BIG, theme.ink)]
    if message:
        lines.append((message, look.SIZE_VALUE, theme.dim))
    if detail:
        lines.append((detail, look.SIZE_SMALL, theme.dim))

    gap = 5
    pad_x, pad_y = 22, 15
    heights = [int(size * 1.35) for _, size, _ in lines]
    box_h = sum(heights) + gap * (len(lines) - 1) + pad_y * 2
    box_w = look.W - 40
    room = box_w - pad_x * 2

    # Trim anything that will not fit, so a long error reads as truncated instead of
    # running off the edge of the box.
    trimmed = [(_fit(text, size, room), size, rgb) for text, size, rgb in lines]
    widest = max(screen.measure_text(text, font_size=size)[0]
                 for text, size, _ in trimmed)
    box_w = min(look.W - 24, max(200, int(widest) + pad_x * 2))
    x = (look.W - box_w) // 2
    y = (look.H - box_h) // 2

    screen.pen = color.rgb(*theme.bg)
    screen.rectangle(rect(0, 0, look.W, look.H))
    screen.pen = color.rgb(*theme.accent)
    screen.shape(shape.rounded_rectangle(rect(x, y, box_w, box_h), 8))
    screen.pen = color.rgb(*theme.bg)
    screen.shape(shape.rounded_rectangle(rect(x + 2, y + 2, box_w - 4, box_h - 4), 7))

    cursor = y + pad_y
    for (text, size, rgb), height in zip(trimmed, heights):
        blit_label(text, size, rgb, look.W // 2, cursor, align=1)
        cursor += height + gap


def _fit(text, size, room):
    """Shorten a string until it fits `room` pixels, with an ellipsis if cut."""
    if screen.measure_text(text, font_size=size)[0] <= room:
        return text
    cut = text
    while cut and screen.measure_text(cut + "...", font_size=size)[0] > room:
        cut = cut[:-1]
    return (cut + "...") if cut else text


def toast(theme, message):
    """A short-lived note over the footer, for a command that was sent."""
    width = min(look.W - 40, 40 + len(message) * 7)
    x = (look.W - width) // 2
    y = look.H - look.FOOTER_H - 26
    screen.pen = color.rgb(*theme.accent)
    screen.shape(shape.rounded_rectangle(rect(x, y, width, 22), 6))
    blit_label(message, look.SIZE_LABEL, theme.bg, look.W // 2, y + 4, align=1)


# -- rings ------------------------------------------------------------------

# Thin enough that four rings fit the dial's own radius, which is what keeps this page on
# the same bounds as a single gauge.
RING_BAND = 14
RING_GAP = 4


def rings(theme, entries):
    """Concentric sweep gauges, outermost first, with a legend down the side.

    One arc per reading, so four readings cost four shapes: the same trick the single
    gauge uses, at a quarter of the screen each. The stack sits where the single dial does
    and the legend is that page's own column of readouts.
    """
    rows = entries[:4]
    height = look.READOUT_NOTE_H if any(entry[4] for entry in rows) else look.READOUT_H
    for index, ((name, value_text, fraction, rgb, note), y) in enumerate(
            zip(rows, look.readout_rows(len(rows), height))):
        ring_outer = look.DIAL_OUTER - index * (RING_BAND + RING_GAP)
        ring_inner = ring_outer - RING_BAND
        if ring_inner < 8:
            break
        screen.pen = color.rgb(*theme.grid)
        screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                               look.DIAL_FROM, look.DIAL_TO))
        if fraction:
            sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * fraction
            screen.pen = color.rgb(*rgb)
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   look.DIAL_FROM, sweep))
        # The legend doubles as the reading, so the rings need no labels on them. The chip
        # is only for a row whose scale note has taken the bar's place: where there is a
        # bar, it is already drawn in this ring's colour.
        readout(theme, y, name, value_text, fraction, note, chip=rgb if note else None)


# -- sparklines -------------------------------------------------------------

def sparklines(theme, entries):
    """A row per reading: name, current value, and its history as a small area.

    Six of these fit the body band, which is the point - one page that says what every
    other page says, at the cost of the detail a full graph gives.
    """
    rows = entries[:6]
    if not rows:
        return
    height = min(30, (look.BODY_H - 8) // max(1, len(rows)))
    # The plot takes what the two text columns leave, so the names are not followed by a
    # gap and the readings are not sat on the plots.
    name_w = column_width([row[0] for row in rows], look.SIZE_LABEL)
    value_w = column_width([row[1] for row in rows], look.SIZE_LABEL)
    plot_x = look.PAD + name_w + COLUMN_GAP
    plot_w = max(40, look.W - plot_x - COLUMN_GAP - value_w - look.PAD)
    for index, (name, value_text, points, peak) in enumerate(rows):
        top = look.BODY_TOP + 6 + index * height
        mid = top + height // 2
        blit_label(name, look.SIZE_LABEL, theme.dim, look.PAD, mid - 7)

        plot_h = height - 8
        screen.pen = color.rgb(*theme.grid)
        screen.hspan(plot_x, top + plot_h + 3, plot_w)
        if points and len(points) > 1 and peak:
            step = plot_w / float(len(points) - 1)
            contour = []
            for i, value in enumerate(points):
                fraction = max(0.0, min(1.0, (value or 0.0) / peak))
                contour.append(vec2(plot_x + i * step, top + plot_h - plot_h * fraction))
            contour.append(vec2(plot_x + plot_w, top + plot_h + 3))
            contour.append(vec2(plot_x, top + plot_h + 3))
            screen.pen = color.rgb(*theme.accent)
            screen.alpha = 190
            screen.shape(shape.custom(contour))
            screen.alpha = 255
        blit_label(value_text, look.SIZE_LABEL, theme.ink, look.W - look.PAD, mid - 7,
                   align=2)


# -- radar ------------------------------------------------------------------

def radar(theme, entries):
    """A polygon over normalised axes: the shape of the machine's load right now.

    Three axes is the fewest that encloses an area, and past six the labels collide.
    """
    import math

    rows = entries[:6]
    if len(rows) < 3:
        blit_label("radar needs three readings", look.SIZE_VALUE, theme.dim,
                   look.W // 2, look.BODY_MID, align=1)
        return
    centre = (look.W // 2, look.BODY_MID)
    # An ellipse, not a circle: the body band is 300 wide and 190 tall, and a label is two
    # lines that reach 15px below its anchor. Four readings put an axis straight down, so a
    # circle wide enough to use the width spills that label into the page indicator. 56 is
    # what keeps the block inside the band; the width is free to stay larger.
    radius_x, radius_y = 70, 56
    count = len(rows)

    def point(index, fraction):
        # Axes start at twelve and run clockwise, matching the gauges.
        angle = math.radians(index * 360.0 / count - 90.0)
        return vec2(centre[0] + math.cos(angle) * radius_x * fraction,
                    centre[1] + math.sin(angle) * radius_y * fraction)

    screen.pen = color.rgb(*theme.grid)
    for step in (0.5, 1.0):
        web = [point(i, step) for i in range(count)]
        for i in range(count):
            here, then = web[i], web[(i + 1) % count]
            screen.line(here, then, 1)
    for i in range(count):
        screen.line(vec2(*centre), point(i, 1.0), 1)

    filled = [point(i, row[2] or 0.0) for i, row in enumerate(rows)]
    screen.pen = color.rgb(*theme.accent)
    screen.alpha = 150
    screen.shape(shape.custom(filled))
    screen.alpha = 255
    for corner in filled:
        screen.shape(shape.circle(corner, 3))

    for i, (name, value_text, _fraction, _rgb) in enumerate(rows):
        anchor = point(i, 1.34)
        align = 1
        if anchor.x < centre[0] - 20:
            align = 2
        elif anchor.x > centre[0] + 20:
            align = 0
        blit_label(name, look.SIZE_SMALL, theme.dim, anchor.x, anchor.y - 12,
                   align=align)
        blit_label(value_text, look.SIZE_LABEL, theme.ink, anchor.x, anchor.y - 1,
                   align=align)


# -- trend ------------------------------------------------------------------

def trend(theme, value_text, unit_text, name, delta, points, peak, fraction):
    """One big reading, which way it is going, and where it has been.

    The arrow and the change are the point: a number on its own does not say whether
    something is climbing.
    """
    blit_label(name, look.SIZE_LABEL, theme.dim, look.PAD + 2, look.BODY_TOP + 8)
    reading = label(value_text, look.SIZE_HUGE, theme.ink)
    screen.blit(reading, vec2(look.PAD, look.BODY_TOP + 26))
    if unit_text:
        blit_label(unit_text, look.SIZE_BIG, theme.dim,
                   look.PAD + reading.width + 4, look.BODY_TOP + 48)

    if delta is not None:
        x = look.W - look.PAD
        blit_label(f"{abs(delta):.1f}", look.SIZE_VALUE, theme.ink, x, look.BODY_TOP + 30,
                   align=2)
        # Drawn, not written: the text font carries no arrows, and a missing glyph is a
        # gap rather than an error.
        _arrow(theme, x - 46, look.BODY_TOP + 34, delta, fraction)


    # The history underneath, so the number has somewhere to have come from.
    top = look.BODY_TOP + 92
    height = look.BODY_H - 100
    left = look.PAD
    width = look.W - look.PAD * 2
    screen.pen = color.rgb(*theme.grid)
    screen.hspan(left, top + height, width)
    if points and len(points) > 1 and peak:
        step = width / float(len(points) - 1)
        contour = []
        for i, value in enumerate(points):
            part = max(0.0, min(1.0, (value or 0.0) / peak))
            contour.append(vec2(left + i * step, top + height - height * part))
        contour.append(vec2(left + width, top + height))
        contour.append(vec2(left, top + height))
        screen.pen = color.rgb(*theme.accent)
        screen.alpha = 170
        screen.shape(shape.custom(contour))
        screen.alpha = 255


def _arrow(theme, x, y, delta, fraction):
    """A triangle for the direction, flat where the reading is holding still."""
    half, height = 9, 11
    if delta > 0.05:
        screen.pen = color.rgb(*(theme.at(fraction) if fraction is not None
                                 else theme.ink))
        screen.shape(shape.custom([vec2(x, y - height), vec2(x + half, y),
                                   vec2(x - half, y)]))
    elif delta < -0.05:
        screen.pen = color.rgb(*theme.dim)
        screen.shape(shape.custom([vec2(x, y), vec2(x + half, y - height),
                                   vec2(x - half, y - height)]))
    else:
        screen.pen = color.rgb(*theme.dim)
        screen.rectangle(rect(x - half, y - height // 2 - 2, half * 2, 4))


# -- waterfall --------------------------------------------------------------

# The scroll buffer, its write cursor, and the lane count it was built for. One column
# is written per frame and the buffer is shown as two windowed blits, because scrolling
# by copying the image onto itself costs 11ms where two windows cost 7ms.
_wf_image = None
_wf_cursor = 0
_wf_lanes = 0

WF_LEFT = look.PAD + 22
WF_TOP = look.BODY_TOP + 6


def waterfall_reset():
    global _wf_image, _wf_cursor, _wf_lanes
    _wf_image = None
    _wf_cursor = 0
    _wf_lanes = 0


def waterfall(theme, lanes, labels=None):
    """One column per call, scrolling left: a lane per value, coloured by the ramp.

    Time is measured in frames rather than samples, which is what makes it move: the
    caller interpolates between polls and this draws wherever that got to. Precision is
    the thing being traded away, and the ramp carries the reading instead.
    """
    global _wf_image, _wf_cursor, _wf_lanes
    if not lanes:
        blit_label("no per-core readings", look.SIZE_VALUE, theme.dim,
                   look.W // 2, look.BODY_MID, align=1)
        return

    width = look.W - WF_LEFT - look.PAD
    height = look.BODY_H - 14
    if _wf_image is None or _wf_lanes != len(lanes):
        _wf_image = image(width, height)
        _wf_image.pen = color.rgb(*theme.bg)
        _wf_image.rectangle(rect(0, 0, width, height))
        _wf_cursor = 0
        _wf_lanes = len(lanes)

    lane_h = height / float(len(lanes))
    for index, fraction in enumerate(lanes):
        part = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
        top = int(index * lane_h)
        bottom = int((index + 1) * lane_h)
        _wf_image.pen = color.rgb(*theme.at(part))
        # vspan, not a rectangle: one call for the lane's whole run of pixels.
        _wf_image.vspan(_wf_cursor, top, max(1, bottom - top))
    _wf_cursor = (_wf_cursor + 1) % width

    # Oldest column first, so the newest lands at the right hand edge.
    tail = width - _wf_cursor
    screen.blit(_wf_image.window(rect(_wf_cursor, 0, tail, height)),
                vec2(WF_LEFT, WF_TOP))
    if _wf_cursor:
        screen.blit(_wf_image.window(rect(0, 0, _wf_cursor, height)),
                    vec2(WF_LEFT + tail, WF_TOP))

    for index, name in enumerate(labels or ()):
        if index >= len(lanes):
            break
        y = WF_TOP + int((index + 0.5) * lane_h) - 6
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD + 16, y, align=2)
