"""Drawing the pages.

Vector shapes throughout, which makes a theme a colour table. Shapes are drawn live and
every string that survives a frame is baked into a sprite; see CACHE_UNDER for where that
stops paying, and DEVELOPMENT.md for the costs behind it.

A page that splits into something round and a column of text beside it - the single dial,
the ring stack, an extension's clock face - takes its geometry from `look.DIAL_C`,
`look.DIAL_OUTER` and `look.READOUT_X`. Its rows come from `look.readout_rows` and either
`readout` or `column_lines` here.
"""

import binascii
import os
from array import array

import look

FONT = None

# Emptied by clear_cache(). Each cache registers where it is defined, so one added later
# is dropped on a theme change and this list stays as it is.
_CLEARS = []


def _cached(empty):
    """Register a container holding colours, or sprites painted in them. Returns it."""
    _CLEARS.append(empty.clear)
    return empty


def clears(reset):
    """Register a function to run on a theme change, as a decorator.

    For state that is not one container. worldmap uses it: its pens are keyed by theme
    name, and two tints of one theme share a name.
    """
    _CLEARS.append(reset)
    return reset


# How many decoded pictures to hold. The same bytes arrive every frame between changes,
# and at most three are on screen.
_pictures = _cached({})
PICTURE_CACHE = 4

_labels = _cached({})
_pip_rows = _cached({})

# Fonts by name, so a sprite cache key can say which one drew it. Without the name an icon
# and a letter of the same string collide and one is drawn in the wrong font. Kept across
# a theme change: a font holds no colour, and loading the text one is 107ms.
TEXT = "text"
ICONS = "icons"
_fonts = {}


def prepare():
    """Load the fonts. The text font is 107ms, so once, and before the first frame."""
    global FONT
    if FONT is None:
        if not add_font(look.FONT_NAME, look.FONT_FILE):
            print(f"draw: no {look.FONT_FILE}, falling back to the firmware's font")
            add_font(look.FONT_NAME, look.FALLBACK_FONT_PATH)
        FONT = _fonts.get(look.FONT_NAME)
    # TEXT is the role, look.FONT_NAME the font filling it, so use_font can name either.
    _fonts[TEXT] = FONT
    _fonts[look.FONT_NAME] = FONT
    screen.font = FONT
    add_font(ICONS, look.ICON_FILE)


def add_font(name, *paths):
    """Register a font under a name, from the first of `paths` that loads.

    A bare filename is looked for in the app directory and then beside this module; see
    _candidates for why the order matters. An extension passes full paths.

    A missing or unloadable file is reported and skipped, so a page needing an icon falls
    back to its words.
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
            return True
    return False


def _candidates(path):
    """Where to look for a font. An absolute path is taken as given; anything else is
    relative to the app, which is `fonts/x.af` for what the app ships.

    A path under /remote is skipped: that is a `mpremote mount` serving the file as text,
    and font.load reads it as UTF-8, fails partway and wedges the REPL. Under mount the
    device copy is found instead, or the fallback. /fonts is writable, so the tools can put
    a font somewhere loadable without an install.
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

    The sprite cache keys on the name, not the font behind it, so changing what TEXT points
    at has to empty it.
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

# Text this size and over is drawn live and never kept: at 104pt a blit is 3.01ms against
# 1.27ms to draw it, for a 130KB sprite. Under it the cache wins, 0.08ms against 0.22ms.
CACHE_UNDER = 40

# Strings seen once, so a second sighting is what bakes one. Every reading that moves is a
# new key, and baking each one fills the heap: 221 sprites at a time, from mem_probe.py.
_once = _cached(set())
# Keys, not pictures: about 50KB full.
ONCE_MAX = 512


def label(text_value, size, pen, name=TEXT):
    """A string baked into a sprite, or None if it should be drawn where it stands.

    None for a string too large to keep, and for one not seen before; see `_once`. To place
    something against a string's width, ask `text_width` and draw with `blit_label`.
    """
    if size >= CACHE_UNDER:
        return None
    key = (name, text_value, size, pen)
    cached = _labels.get(key)
    if cached is not None:
        return cached
    if key not in _once:
        if len(_once) > ONCE_MAX:
            _once.clear()
        _once.add(key)
        return None
    face = _fonts.get(name)
    if face is None:
        return None
    was = screen.font
    screen.font = face
    try:
        width, height = screen.measure_text(text_value, font_size=size)
        width = max(1, int(width + 2))
        height = max(1, int(size * 1.35))
        sprite = image(width, height)
        sprite.font = face
        sprite.pen = brush.erase()
        sprite.rectangle(rect(0, 0, width, height))
        sprite.antialias = image.X4
        sprite.pen = pen
        sprite.text(text_value, vec2(0, 0), size)
    finally:
        screen.font = was
    if len(_labels) > 220:
        # A ceiling for a badge that has been through every page and theme. Dropped wholesale,
        # and only twice-asked-for strings are in here.
        _labels.clear()
    _labels[key] = sprite
    return sprite


def blit_label(text_value, size, pen, x, y, align=0, name=TEXT):
    """Draw a string. align 0 left, 1 centre, 2 right, about x.

    From a sprite where one is worth keeping, live otherwise. Returns the width drawn, or 0
    for a font still to load, so a caller can try an icon and fall back to words.
    """
    face = _fonts.get(name)
    if face is None:
        return 0
    sprite = label(text_value, size, pen, name)
    if sprite is None:
        width = text_width(text_value, size, name)
        if align == 1:
            x -= width // 2
        elif align == 2:
            x -= width
        was = screen.font
        screen.font = face
        try:
            screen.pen = pen
            screen.text(text_value, vec2(int(x), int(y)), size)
        finally:
            screen.font = was
        return width
    if align == 1:
        x -= sprite.width // 2
    elif align == 2:
        x -= sprite.width
    screen.blit(sprite, vec2(int(x), int(y)))
    return sprite.width


def blit_icon(character, size, pen, x, y, align=0):
    """Draw one symbol from the icon font. 0 if there is no icon font."""
    return blit_label(character, size, pen, x, y, align, ICONS)


def clear_cache():
    """Forget everything held from an earlier draw.

    Everything holding colours, not only the sprites: a decoded picture is painted in the
    theme's greys, and the waterfall's scroll buffer is a second of columns painted in the
    ramp they were drawn with.
    """
    for empty in _CLEARS:
        empty()


# -- measuring --------------------------------------------------------------

COLUMN_GAP = 8


def text_width(text_value, size, name=TEXT):
    """How wide a string will be drawn, for a column to be fitted to it.

    Plus the pixel `label` adds, keeping a measurement and its sprite in agreement.
    """
    face = _fonts.get(name)
    if face is None:
        return 0
    was = screen.font
    screen.font = face
    try:
        width, _ = screen.measure_text(text_value, font_size=size)
    finally:
        screen.font = was
    return int(width) + 2


# What the app's fonts are built to: a capital stands 81 units of a 128 unit em, an icon
# fits a box of 100 on the baseline. A wide font keeps both ratios at a finer grid.
CAP_UNITS, ICON_UNITS, EM_UNITS = 81.0, 100.0, 128.0
CAP = CAP_UNITS / EM_UNITS
ICON_BOX = ICON_UNITS / EM_UNITS


def icon_baseline(text_y, text_size, icon_size):
    """Where to draw an icon so it centres on the capitals of text drawn at `text_y`.

    The icon's box stands taller than a capital and its ink sits mid-box, so a shared
    baseline floats the symbol 4.5px above the words at 32 beside 26pt.

    Against the capitals and not the string's extent, or a diacritic moves the symbol and
    16°C sits lower than 16C.
    """
    cap_middle = text_y + text_size * (1.0 - CAP / 2.0)
    return int(cap_middle - icon_size * (1.0 - ICON_BOX / 2.0))


def column_width(texts, size, name=TEXT):
    """How wide a column of these strings has to be.

    Neither column's width is known in advance, so both are measured and the row reflows.
    One measurement, not one per string: `measure_text` breaks on newlines and returns the
    widest line, so sixteen readings cost 0.2ms against 2.8ms a string at a time.
    """
    if not texts:
        return 0
    return text_width("\n".join(texts), size, name)


# A column can also be drawn as one bounded `screen.text` call. Measured, that loses to
# the sprite cache: cores 24.2ms against 22.6, the text page 14.4 against 10.3.


# -- formatting -------------------------------------------------------------

def fmt(value, field):
    """A number as a badge should show it: short, and never wider than its box."""
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return _several(value, field)
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


# How many figures a slot shows before it just says how many there are. Three is a load
# average; sixteen per-core loads belong on a bars page.
SEVERAL = 3


def _several(values, field):
    if not values:
        return "--"
    if len(values) <= SEVERAL:
        return " ".join(fmt(item, field) for item in values)
    return f"{len(values)} values"


def _rate(bps):
    """A throughput, scaled to the largest prefix it fills.

    The prefix is part of the number and `short_unit` supplies the B/s after it, and the
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

    `fmt` leaves the prefix on the number, so this is only the base: 11.4M and MB/s make
    11.4MB/s at any size.
    """
    if field.endswith("_bps"):
        return "B/s"
    if field == "cores" or field == "pct" or field.endswith("_pct"):
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


# What a value came out as. Formatting one is 305us against 21us to look it up, and
# sixteen bars a frame is 4.9ms of formatting the same numbers.
_readings = _cached({})


def reading(value, field):
    """A value with its unit, for a slot that has no room to place one separately.

    `fmt` carries the prefix and short_unit the base, so a rate reads 50.0MB/s. A reading
    that never arrived gets no unit, "-- percent" being meaningless.
    """
    # Numbers only: the expensive kind, and the only hashable one. A field can arrive as a
    # list, core loads or a load average.
    if type(value) is float or type(value) is int:
        key = (value, field)
        text = _readings.get(key)
        if text is not None:
            return text
        text = fmt(value, field) + short_unit(field)
        if len(_readings) > 240:
            # A reading per field per poll, so most of these are stale. Dropped wholesale,
            # as the sprites are.
            _readings.clear()
        _readings[key] = text
        return text
    text = fmt(value, field)
    if value is None or isinstance(value, (str, bool, list, tuple)):
        # A load average is a queue length, not a percentage, and "16 values%" is nonsense.
        return text
    return text + short_unit(field)


# -- chrome -----------------------------------------------------------------

def background(theme, title, index, total, subtitle=None):
    """The header, the footer and a cleared body, drawn where they stand.

    Raster fills and two cached labels: 2.1ms, against 3.9ms when the bands were baked
    into images and blitted.
    """
    screen.pen = theme.bg
    screen.rectangle(rect(0, look.HEADER_H, look.W, look.BODY_H))
    furniture(theme, title, index, total, subtitle)


def furniture(theme, title, index, total, subtitle=None):
    """The header and footer alone, leaving the body as it stands.

    So a page turn can name where it is going before the body gets there.
    """
    screen.pen = theme.panel
    screen.rectangle(rect(0, 0, look.W, look.HEADER_H))
    screen.rectangle(rect(0, look.H - look.FOOTER_H, look.W, look.FOOTER_H))
    # The chrome takes accent_b, leaving the accent for readings.
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, look.HEADER_H - 2, look.W, 2))
    blit_label(title.upper(), look.SIZE_TITLE, theme.ink, look.PAD, 4)
    if subtitle:
        blit_label(subtitle, look.SIZE_SMALL, theme.dim, look.W - look.PAD, 10, align=2)
    if total > 1:
        row = _pips(theme, index, total)
        screen.blit(row, vec2((look.W - row.width) // 2,
                              look.H - look.FOOTER_H + look.FOOTER_H // 2 - 2))


# The pips have this much of the width. A dash shortens as they pack in, down to a dot:
# thinner than it is tall stops reading as a mark.
PIP_ROOM = look.W - look.PAD * 4
PIP_MAX_W, PIP_GAP, PIP_DOT, PIP_TIGHT = 14, 5, 4, 2


def _pips(theme, index, total):
    """The pip row as a sprite, one pip per page and the current one in the accent colour.

    Shortens to fit, and tightens the spacing before it gives up any more length.

    Baked, and no wider than the pips: a rounded rectangle is 0.19ms whatever its size, and
    a dozen a frame is more than the footer is worth.
    """
    key = (theme.key, index, total)
    row = _pip_rows.get(key)
    if row is not None:
        return row

    gap = PIP_GAP
    pip_w = min(PIP_MAX_W, (PIP_ROOM - (total - 1) * gap) // total)
    if pip_w < PIP_DOT:
        gap = PIP_TIGHT
        pip_w = max(PIP_DOT, min(PIP_MAX_W, (PIP_ROOM - (total - 1) * gap) // total))

    span = total * pip_w + (total - 1) * gap
    row = image(span, 4)
    row.antialias = image.X4
    row.pen = brush.erase()
    row.rectangle(rect(0, 0, span, 4))
    for i in range(total):
        row.pen = theme.accent_b if i == index else theme.grid
        row.shape(shape.rounded_rectangle(
            rect(i * (pip_w + gap), 0, pip_w, 4), min(2, pip_w // 2)))
    if len(_pip_rows) > 12:
        _pip_rows.clear()
    _pip_rows[key] = row
    return row


# -- widgets ----------------------------------------------------------------

# How the big gauge fills, from the layout. "solid" is the ramp's colour for the reading.
# "ramp" lays the ramp round the arc and leaves the part past the reading faint.
GAUGE_FILL = "solid"
# How faint that is, per stop: a gradient brush ignores screen.alpha where a solid pen
# blends at it.
TRACK_ALPHA = 32
_gradients = _cached({})


def swept_pens(theme, centre, radius, backwards=False):
    """The theme's ramp round a gauge: what fills the sweep, and what sits behind it.

    A conical's stops are fractions of a turn, so a 270 degree gauge lays the ramp over
    three quarters of one. Its second point is the direction it starts in, DIAL_FROM
    clockwise from straight up, matching arc().

    `backwards` reverses the ramp, for the fields in pages.GOOD_HIGH: the colour comes from
    the angle and not a lookup, so reversing it is what lands the sweep's end on the
    reading's colour.

    Cached, a pair from OKLCH stops being 3.4ms.
    """
    key = (theme.key, centre, radius, backwards)
    pens = _gradients.get(key)
    if pens is None:
        import math

        turn = (look.DIAL_TO - look.DIAL_FROM) / 360.0
        stops = [(pos * turn, pen) for pos, pen in theme.ramp]
        if backwards:
            stops = [(turn - pos, pen) for pos, pen in reversed(stops)]
        angle = math.radians(look.DIAL_FROM)
        towards = (centre[0] + math.sin(angle) * radius,
                   centre[1] - math.cos(angle) * radius)
        pens = _gradients[key] = tuple(
            brush.gradient(brush.CONICAL, centre[0], centre[1], towards[0], towards[1],
                           tuple((pos, pen if alpha == 255 else pen.with_alpha(alpha))
                                 for pos, pen in stops))
            for alpha in (255, TRACK_ALPHA))
    return pens


def gauge(theme, centre, outer, inner, fraction, value_text, under=None,
          value_size=None, label_size=None, cold=False, icon=None, unit=None, hot=None,
          swept=None):
    """One sweep gauge, with a line of text inside it.

    `shape.arc(centre, inner, outer, from, to)` angles start at the top and run clockwise,
    so look.DIAL_FROM..DIAL_TO is 225..495 and the gap lands at the bottom, where `under`
    goes.

    `hot` is where the reading sits on the ramp, which can differ from where it sits on its
    scale. It colours the sweep; the sweep's length is the reading either way.

    `icon` replaces `under` where the font has it. `unit` is a small suffix on the reading,
    for a gauge whose slot below is spoken for, and is dropped before it spills out of a
    small ring.

    `swept` is a (fill, track) pair from `swept_pens`. Without one the sweep is the single
    ramp colour for the reading and the track is the grid.
    """
    value_size = value_size or look.SIZE_HUGE
    label_size = label_size or look.SIZE_LABEL
    middle = vec2(*centre)
    start, end = look.DIAL_FROM, look.DIAL_TO
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    fill, track = swept if swept else (None, None)

    # Track and sweep abut instead of overlapping, which halves the arc a full gauge
    # rasterises. The join lands under the tick drawn below.
    lit = not cold and fraction > 0.001
    sweep = start + (end - start) * fraction if lit else start
    screen.pen = theme.grid if track is None or cold else track
    if end - sweep > 0.5:
        screen.shape(shape.arc(middle, inner, outer, sweep, end))

    if lit:
        # "solid" colours the whole arc by the reading; "ramp" lays the ramp round it so the
        # scale shows too. One shape either way.
        screen.pen = (theme.at(fraction if hot is None else hot) if fill is None else fill)
        screen.shape(shape.arc(middle, inner, outer, start, sweep))

        # A brighter tick at the sweep's end, over the join between the two arcs.
        screen.pen = theme.ink
        screen.shape(shape.arc(middle, inner - 3, outer + 3, sweep - 1.4, sweep + 1.4))

    ink = theme.dim if cold else theme.ink
    top = centre[1] - value_size * 0.62
    unit_size = max(look.SIZE_SMALL, int(value_size * 0.45))
    reading_w = text_width(value_text, value_size)
    suffix_w = text_width(unit, unit_size) if unit else 0
    if suffix_w and reading_w + suffix_w > inner * 2 - 4:
        # Inside the ring, never over the arc. A gauge too small for its unit drops it, and a
        # scaled figure keeps its prefix on the number anyway.
        suffix_w = 0
    left = centre[0] - (reading_w + suffix_w) // 2
    blit_label(value_text, value_size, ink, left, top)
    if suffix_w:
        # On the reading's baseline. Text puts its baseline `size` below where it is drawn, so
        # the drop is the difference in sizes.
        blit_label(unit, unit_size, theme.dim, left + reading_w,
                   top + value_size - unit_size)
    below = centre[1] + value_size * 0.42
    if icon and blit_icon(icon, label_size + 8, theme.dim, centre[0], below, align=1):
        return
    if under:
        blit_label(under, label_size, theme.dim, centre[0], below, align=1)


def dial(theme, fraction, value_text, unit_text, cold=False, hot=None, backwards=False):
    """The single gauge of a `dial` page, with its readouts beside it.

    The one gauge with a page to itself, and the only one large enough to read a ramp off,
    so this is where the swept fill is offered.
    """
    gauge(theme, look.DIAL_C, look.DIAL_OUTER, look.DIAL_INNER, fraction, value_text,
          unit_text, cold=cold, hot=hot,
          swept=swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, backwards)
          if GAUGE_FILL == "ramp" else None)


def dials(theme, entries):
    """Up to four gauges across the body band, each named under its reading.

    One page kind and not one per count. The field count is the only thing that changes,
    so it picks the layout.
    """
    shape_of = look.DIALS.get(len(entries)) or look.DIALS[4]
    for centre, entry in zip(shape_of["centres"], entries):
        name, value_text, fraction, icon, unit, hot = entry
        gauge(theme, centre, shape_of["outer"], shape_of["inner"], fraction, value_text,
              name, shape_of["value"], shape_of["label"], fraction is None, icon, unit,
              hot)


def readout(theme, y, name, value_text, fraction=None, note=None, chip=None, hot=None):
    """One row of the column beside a gauge: a name, the reading under it, and then either
    a bar for the level or a line saying what full is.

    `chip` is the colour of the ring a row belongs to, tying the two together where the
    gauge is not the bar.
    """
    x = look.READOUT_X
    blit_label(name, look.SIZE_SMALL, theme.dim, x, y)
    blit_label(value_text, look.SIZE_VALUE, theme.ink, x, y + 10)
    if chip:
        screen.pen = chip
        screen.rectangle(rect(x + look.READOUT_W - 10, y + 3, 10, 10))
    if note:
        # What a full ring is, where the scale is not a round number. It takes the bar's place.
        blit_label(note, look.SIZE_SMALL, theme.dim, x, y + 29)
    elif fraction is not None:
        width = look.READOUT_W
        fraction = max(0.0, min(1.0, fraction))
        filled = int(width * fraction)
        screen.pen = theme.grid
        screen.rectangle(rect(x + filled, y + 28, width - filled, 3))
        if filled:
            screen.pen = theme.at(fraction if hot is None else hot)
            screen.rectangle(rect(x, y + 28, filled, 3))


COLUMN_LEAD = 3


def column_lines(entries, top=None, align=0):
    """A stack of lines down the column beside a gauge, each `(text, size, pen)`.

    For a page whose rows are not readouts, a clock's time and date say, so it takes the
    column's left edge and rhythm from here. Empty strings are skipped.
    Returns the y after the last line.
    """
    y = (look.BODY_TOP + 12) if top is None else top
    x = look.READOUT_X + (look.READOUT_W if align == 2 else 0)
    for text_value, size, pen in entries:
        if not text_value:
            continue
        blit_label(text_value, size, pen, x, y, align=align)
        y += int(size * 1.35) + COLUMN_LEAD
    return y


def flat(values):
    """A series with its gaps at the axis, or the same list where it has none.

    A None in a ring is a sample the host had no reading for. The ring keeps it so a plot
    can read times off positions, and every widget then draws it at the bottom: there is
    no ink for "nothing here", and a break in a line reads as zero anyway.

    Only the series, and once per draw rather than per point. `graph` is passed the list
    as it came: it looks past the gaps for the axis top, a gap not being a reading of zero.
    """
    if None not in values:
        return values
    return [0.0 if value is None else value for value in values]


def at_axis(value):
    """One reading, where a widget takes a single number and None means it never came."""
    return 0.0 if value is None else value


def bars(theme, values, maximum=100.0, field="pct", fractions=None, names=None):
    """A stack of horizontal bars. Raster rectangles: no AA needed on an axis-aligned
    bar, and this is the one page that can have 32 of them.

    `field` is what the values are, so a per-core load prints as a percentage.
    `fractions` is where each bar should be drawn to, for a caller sweeping them; without
    it each bar is drawn at its value.
    `names` labels the lanes; without it they are numbered.
    """
    if not values:
        return
    values = flat(values)
    count = min(len(values), 16)
    top = look.BODY_TOP + 6
        # Fit the band whatever the core count, with at least a pixel between bars.
    slot = max(6, (look.BODY_H - 12) // count)
    height = max(4, slot - 3)
    names = ([str(names[i]) if i < len(names) else "" for i in range(count)] if names
             else [f"{i}" for i in range(count)])
    readings = [reading(values[i], field) for i in range(count)]
    # Both columns are as wide as their widest entry: a fixed one either leaves a gap or
    # runs the readings into the bars.
    label_w = column_width(names, look.SIZE_SMALL)
    value_w = column_width(readings, look.SIZE_SMALL)
    x = look.PAD + label_w + COLUMN_GAP
    width = max(20, look.W - x - COLUMN_GAP - value_w - look.PAD)

    for i in range(count):
        value = values[i]
        if fractions is None:
            fraction = max(0.0, min(1.0, value / maximum if maximum else 0.0))
        else:
            fraction = fractions[i]
        y = top + i * slot
        blit_label(names[i], look.SIZE_SMALL, theme.dim, look.PAD, y - 1)
        filled = max(1, int(width * fraction)) if fraction > 0 else 0
        screen.pen = theme.grid
        # From where the fill ends, so the two meet exactly: an axis-aligned raster edge is a
        # pixel boundary, not an anti-aliased one.
        screen.rectangle(rect(x + filled, y, width - filled, height))
        if filled:
            screen.pen = theme.at(fraction)
            screen.rectangle(rect(x, y, filled, height))
        blit_label(readings[i], look.SIZE_SMALL, theme.ink,
                   look.W - look.PAD, y - 1, align=2)


# Whether a series is a curve through its samples or a polyline between them, from the
# layout. One switch for every graph on the badge.
SMOOTH = True
# Points per span between two samples. Two puts a segment about two pixels across on a
# plot of 48 samples in 250, where the corners stop showing. Four looked the same against
# spiky data and cost 6ms a page.
CURVE_STEPS = 2
# A curve needs height to show. Interpolating a sparkline 22px tall gives back the same
# picture for 1.7ms a series, and a plot shorter than this is drawn straight.
SMOOTH_MIN_H = 40
_weights = {}


def _basis(steps):
    """The Catmull-Rom weights for each fraction of a span, worked out once.

    The weights depend only on t, so a curve of any length reuses `steps` sets. Evaluating
    the polynomial per point cost 265us a point, or 50ms for one series.
    """
    table = _weights.get(steps)
    if table is None:
        table = []
        for step in range(steps):
            t = step / steps
            t2 = t * t
            t3 = t2 * t
            table.append((0.5 * (-t3 + 2.0 * t2 - t),
                          0.5 * (3.0 * t3 - 5.0 * t2 + 2.0),
                          0.5 * (-3.0 * t3 + 4.0 * t2 + t),
                          0.5 * (t3 - t2)))
        table = tuple(table)
        _weights[steps] = table
    return table


def curve_steps(width, height, count):
    """How finely to subdivide `count` samples across a plot this size. 1 means don't.

    A segment shorter than a pixel costs the same as one that shows. A short plot is not
    subdivided at all, a curve needing height to bend.
    """
    if not SMOOTH or count < 3 or height < SMOOTH_MIN_H:
        return 1
    return max(2, min(CURVE_STEPS, int(width / (count - 1))))


def curve(values, steps=CURVE_STEPS):
    """`values` resampled to a Catmull-Rom curve through them, evenly spaced as they were.

    Catmull-Rom passes through each sample and not near it, so the shape smooths without the
    reading moving and the peak drawn is the peak measured.

    Values only; x is implied by index, so the caller lays the output out as it laid out the
    input, over `steps` times as many points. Returned as it came when there is nothing to
    interpolate or SMOOTH is off.

    Held within the range of the input: past the lowest sample an area fill would run under
    its own baseline.
    """
    if steps < 2 or len(values) < 3:
        return values
    low, high = min(values), max(values)
    table = _basis(steps)
    last = len(values) - 1
    out = []
    for index in range(last):
        a = values[index - 1] if index else values[0]
        b = values[index]
        c = values[index + 1]
        d = values[index + 2] if index + 2 <= last else values[last]
        for w0, w1, w2, w3 in table:
            value = w0 * a + w1 * b + w2 * c + w3 * d
            out.append(low if value < low else (high if value > high else value))
    out.append(values[last])
    return out


_points = array("f", b"")

# Samples of room a moving plot keeps on its right. Fixed: from the current offset it
# would resize the plot every frame.
WALK_LEAD = 2


def _lay_out(left, top, width, height, values, peak, shift):
    """`values` scaled against `peak` and laid across the box, in the shared float buffer.

    Returns how many floats were written, or 0.

    `shift` is how far the plot has walked left since its last update, in samples, 0 being
    just after one landed. `None` is a plot that never walks, a different layout from 0.

    A moving plot is laid out `WALK_LEAD` samples wider than its box and clipped to it, so
    the samples still to come slide in at the right.

    Smoothed first if it is tall enough for a curve, then scaled and laid out in one pass.
    `shape.custom` takes a float buffer, so no point is boxed as a vec2: 2.3ms against 3.7
    for 191 points.
    Scaled here and not in a list the caller passes, which saves a pass at 14.7us a point,
    4.2ms of the sparkline page.
    """
    global _points
    values = flat(values)
    count = len(values)
    if count < 2:
        return 0
    steps = curve_steps(width, height, count)
    if steps > 1:
        values = curve(values, steps)
        count = len(values)
    if len(_points) < (count + 2) * 2:
        _points = array("f", bytes((count + 2) * 8))
    # Points per original sample, so a shift of one moves the plot by one reading whether
    # the series was interpolated or not.
    per_sample = steps if steps > 1 else 1
    # The samples still to come are laid past the right edge and slide in, keeping the box
    # full. Laid across the width alone the plot leaves a growing gap.
    lead = per_sample * (WALK_LEAD if WALK_LEAD > 1 else 1)
    # A quarter of the plot at most, so a badge far enough behind gets a shorter walk rather
    # than a plot squeezed into a corner.
    if lead > count // 4:
        lead = count // 4
    walking = shift is not None
    span = count - 1 - lead if walking and count > lead + 1 else count - 1
    step = width / float(span)
    scale = height / float(peak or 1.0)
    bottom = top + height
    # Past the headroom the plot really is short of data, so it moves and leaves the gap.
    # `graph` draws that region as a gap, and does not pass the newest reading off as now.
    away = shift * step * per_sample if walking else 0.0
    start = left - away
    i = 0
    for index in range(count):
        y = bottom - values[index] * scale
        _points[i] = start + index * step
        _points[i + 1] = top if y < top else (bottom if y > bottom else y)
        i += 2
    return i


def area(left, top, width, height, values, peak, base=None, shift=None):
    """One filled area from `values` against `peak`, closed along its base. A shape, or None.

    Where the base sits is a caller's business, a sparkline's axis being under its plot
    and not at the foot of it.
    """
    i = _lay_out(left, top, width, height, values, peak, shift)
    if not i:
        return None
    if base is None:
        base = top + height
    _points[i] = _points[i - 2]
    _points[i + 1] = base
    _points[i + 2] = _points[0]
    _points[i + 3] = base
    return shape.custom(memoryview(_points)[:i + 4])


# A plot as a line, not a fill. A round join costs 3.5ms a page more than a miter.
# Centred on the samples, or the band grows to one side.
LINE_W = 2.0
LINE_FLAGS = (shape.PATH_OPEN | shape.ALIGN_CENTER | shape.JOIN_MITER | shape.CAP_BUTT)


def line(left, top, width, height, values, peak, weight=LINE_W, shift=None):
    """`values` as a stroked polyline against `peak`. A shape, or None."""
    i = _lay_out(left, top, width, height, values, peak, shift)
    if not i:
        return None
    trace = shape.custom(memoryview(_points)[:i])
    trace.stroke(weight, LINE_FLAGS)
    return trace


# What an axis with no full scale tops out at: one of these times a power of the reading's
# base. Stepped, or the scale creeps with every sample. Also settles the gutter's width.
AXIS_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500)


def axis_top(peak, field):
    """The round number an axis tops out at, at or above `peak`.

    In the base the reading is scaled by, so a byte rate steps 1024 at a time and says
    5.0MB/s and not 4.8. The point of a label is placing a sample against it.
    """
    base = 1024.0 if field.endswith(("_bps", "_mb")) else 10.0
    scale = 1.0
    while scale * AXIS_STEPS[-1] < peak:
        scale *= base
    for step in AXIS_STEPS:
        if scale * step >= peak:
            return scale * step
    return scale * base


def graph(theme, series, labels, maximum=None, shift=None):
    """One or two series over time, as filled areas.

    Each series is one `shape.custom` contour, a polyline across the top and back along the
    bottom: one anti-aliased edge and one setup cost, where a line per sample would be
    dozens.

    A field with a full scale is drawn against it with headroom above. One without is drawn
    against the next round number up from the busiest sample on the plot.
    """
    field = labels[0][1] if labels else "pct"
    if maximum is None:
        # A gap in a ring is a None, so the samples are flattened past them. max() over the
        # series themselves compared None against a float and took the app down.
        peak = axis_top(max((p for s in series for p in s if p is not None),
                            default=1.0), field)
    else:
        peak = max(maximum, 1.0) * 1.15

    peak_text = reading(peak, field)
    # The gutter is as wide as the scale in it: 100% and 9.8MB/s need different room.
    left = look.PAD + column_width((peak_text, "0"), look.SIZE_SMALL) + 4
    top = look.BODY_TOP + 8
    width = look.W - left - look.PAD
    height = look.BODY_H - 26

    screen.pen = theme.grid
    for i in range(5):
        y = top + int(height * i / 4.0)
        screen.hspan(left, y, width)

    # Where the series ran out. Drawn and not papered over: a stalled host and an idle
    # machine are otherwise the same flat line.
    if shift is not None and shift > WALK_LEAD:
        stale = min(width, int((shift - WALK_LEAD) * width / float(len(series[0]) or 1)))
        if stale > 1:
            screen.pen = theme.grid
            for y in range(top, top + height, 4):
                screen.hspan(left + width - stale, y, stale)

    for index, points in enumerate(series):
        if not points or len(points) < 2:
            continue
        filled = area(left, top, width, height, points, peak, shift=shift)
        if filled is None:
            continue
        screen.alpha = _series_alpha(theme, index)
        screen.pen = _series_colour(theme, index)
        was = screen.clip
        # A sample wider than its box while it walks, so the oldest leaves at the gutter.
        screen.clip = rect(left, look.BODY_TOP, width, look.BODY_H)
        screen.shape(filled)
        screen.clip = was
    screen.alpha = 255

    # Scale and legend.
    blit_label(peak_text, look.SIZE_SMALL, theme.dim, look.PAD, top - 4)
    blit_label("0", look.SIZE_SMALL, theme.dim, look.PAD, top + height - 8)
    for index, (name, _field) in enumerate(labels[:2]):
        pen = _series_colour(theme, index)
        x = left + index * 110
        y = look.H - look.FOOTER_H - 14
        screen.pen = pen
        screen.rectangle(rect(x, y + 3, 10, 4))
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 14, y - 2)


# The two series' alphas. On a pale page a translucent area washes out, so both go solid.
SERIES_ALPHA = (200, 150)
# How far from the page a series has to land, `difference` measuring black to white as
# 100. Only luminescence falls through, at 13.8, where the next nearest is mono at 24.9.
SERIES_FLOOR = 20


def _series_alpha(theme, index):
    return SERIES_ALPHA[0] if index == 0 or theme.pale else SERIES_ALPHA[1]


def _series_colour(theme, index):
    """Colours for the two graph series: the accent, and whichever end of the ramp is
    furthest from it that can actually be seen.

    The two areas overlap and are semi-transparent, so a near miss shows as one series.
    Which end is further depends on the theme: the default's teal accent takes the hot end,
    mono's near-white the cold one.
    Both ends are tried, furthest first, since a single-hue theme has the page at one end of
    its ramp. Measured across the themes that leaves none on the fallback, where taking the
    furthest end alone left four invisible or grey.
    """
    if index == 0:
        return theme.accent
    alpha = _series_alpha(theme, index)
    # The palette's second colour, still checked against the page.
    if theme.accent_b != theme.accent:
        if theme.bg.difference(theme.accent_b.with_alpha(alpha).over(theme.bg)) >= SERIES_FLOOR:
            return theme.accent_b
    cold, hot = theme.at(0.0), theme.at(1.0)
    order = ((cold, hot) if theme.accent.difference(cold) >= theme.accent.difference(hot)
             else (hot, cold))
    for pen in order:
        if theme.bg.difference(pen.with_alpha(alpha).over(theme.bg)) >= SERIES_FLOOR:
            return pen
    # A palette whose ramp is the page at both ends. `dim` tracks no reading, so it is last.
    return theme.dim


def grid(theme, entries):
    """Up to six labelled figures in two rows, one panel each."""
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
        name, value_text, fraction, icon, hot = entries[i]
        column = i % columns
        row = i // columns
        x = look.PAD + column * (cell_w + 6)
        y = look.BODY_TOP + 6 + row * (cell_h + 6)
        screen.pen = theme.panel
        screen.shape(shape.rounded_rectangle(rect(x, y, cell_w, cell_h), 5))
        if fraction is not None:
            screen.pen = theme.at(max(0.0, min(1.0, fraction if hot is None else hot)))
            screen.rectangle(rect(x, y + cell_h - 3, int(cell_w * max(0.0, min(1.0, fraction))), 3))
        # A cell has room for the name and a symbol in the far corner; a gauge takes the symbol.
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 7, y + 5)
        if icon:
            blit_icon(icon, look.SIZE_VALUE, theme.dim, x + cell_w - 7, y + 4, align=2)
        size = look.SIZE_BIG if rows < 3 else look.SIZE_VALUE
        blit_label(value_text, size, theme.ink, x + 7, y + cell_h // 2 - size // 2 + 2)


# Two equal columns so the halves read as one table, and a plate under them for anything
# too long for a column.
VITALS_METERS = 5
VITALS_FACTS = 5
VITALS_NOTE_H = 12
# The bar under a level: four, against the readout column's three, this page being read
# by its bars where a readout's sits beside a gauge saying the same thing.
VITALS_BAR_H = 4


def vitals(theme, meters, facts, notes=()):
    """Levels down the left, figures down the right, and a plate of strings underneath.

    Half levels needing a bar and half strings to read, which no other widget here covers.
    """
    column = (look.W - look.PAD * 3) // 2
    right = look.PAD * 2 + column
    top = look.BODY_TOP + 4
    plate_h = len(notes) * VITALS_NOTE_H
    room = look.BODY_H - 8 - plate_h

    pitch = room // max(1, min(len(meters), VITALS_METERS))
    for index, (name, value_text, fraction, hot) in enumerate(meters[:VITALS_METERS]):
        y = top + index * pitch
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD, y)
        # A compound figure, used of total, where the right hand column carries single numbers.
        blit_label(value_text, look.SIZE_LABEL, theme.ink, look.PAD, y + 10)
        if fraction is None:
            continue
        fraction = max(0.0, min(1.0, fraction))
        filled = int(column * fraction)
        bar_y = y + pitch - VITALS_BAR_H - 4
        screen.pen = theme.grid
        screen.rectangle(rect(look.PAD + filled, bar_y, column - filled, VITALS_BAR_H))
        if filled:
            screen.pen = theme.at(fraction if hot is None else hot)
            screen.rectangle(rect(look.PAD, bar_y, filled, VITALS_BAR_H))

    pitch = room // max(1, min(len(facts), VITALS_FACTS))
    for index, (name, value_text) in enumerate(facts[:VITALS_FACTS]):
        y = top + index * pitch
        blit_label(name, look.SIZE_SMALL, theme.dim, right, y + 4)
        blit_label(value_text, look.SIZE_VALUE, theme.ink, right + column, y, align=2)
        screen.pen = theme.grid
        screen.hspan(right, y + pitch - 6, column)

    # Fitted, and never wrapped: these are one string each and the useful end is the front.
    y = look.BODY_TOP + look.BODY_H - plate_h - 2
    for note in notes:
        blit_label(fit(note, look.SIZE_SMALL, look.W - look.PAD * 2), look.SIZE_SMALL,
                   theme.dim, look.PAD, y)
        y += VITALS_NOTE_H


def lines(theme, entries):
    """Labelled lines, for names and versions."""
    y = look.BODY_TOP + 10
    for name, value_text in entries[:7]:
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD, y + 3)
        blit_label(value_text, look.SIZE_VALUE, theme.ink, look.W - look.PAD, y,
                   align=2)
        y += 24
        screen.pen = theme.grid
        screen.hspan(look.PAD, y - 5, look.W - look.PAD * 2)


def flow(text_value, size, pen, box, name=TEXT):
    """A run of text filled into `box`, wrapped, with an ellipsis where it does not fit.

    The firmware flows and truncates: `screen.text` takes a rect and an overflow. Here it
    would be a `measure_text` a word, in Python, on every draw.

    Live and not cached: a post is long, unique and read once, so its sprite would be baked,
    blitted once and dropped.
    """
    face = _fonts.get(name)
    if face is None or not text_value:
        return
    was = screen.font
    screen.font = face
    try:
        screen.pen = pen
        screen.text(text_value, box, size, align=(LEFT, TOP), overflow=ELLIPSES)
    finally:
        screen.font = was


# The naming line and the body under it. How many body lines fit follows from the block
# height, which the firmware settles as it flows.
ITEM_TITLE = look.SIZE_SMALL
ITEM_TEXT = look.SIZE_VALUE
# The strip of counters along the bottom, when a page has any.
COUNT_H = 34


def notification(theme, items, counters):
    """Messages down the page, with a row of counters under them.

    One shape for a post, a mention, a headline and an RSS entry. Who it came from, the
    text, how long ago, and sometimes why it is here.
    Anything numeric on the page is a counter, drawn small along the bottom.
    """
    top, bottom = look.BODY_TOP, look.BODY_TOP + look.BODY_H
    if counters:
        bottom -= COUNT_H
        _counter_row(theme, counters, bottom, look.BODY_TOP + look.BODY_H)
    if not items:
        blit_label("nothing yet", ITEM_TEXT, theme.dim, look.W // 2,
                   (top + bottom) // 2 - 8, align=1)
        return
    height = (bottom - top) // len(items)
    for index, item in enumerate(items):
        _item_block(theme, item, top + index * height, height)
        if index:
            screen.pen = theme.grid
            screen.hspan(look.PAD, top + index * height, look.W - look.PAD * 2)


# The gap beside a picture, and the least width worth drawing one in: a message three to
# a page has 52px of block, and thinner is a smear.
PICTURE_GAP = 8
PICTURE_MIN = 24


def fitted(shown, height):
    """`shown` cropped to `height`, or None where there is not enough room to bother.

    A band from the middle, where the crop that made the picture put what matters. Cropped
    and not scaled: the pixels are palette indices, and halfway between two of them is a
    third colour.
    """
    if shown is None or height >= shown.height:
        return shown
    if height < PICTURE_MIN:
        return None
    return shown.window(rect(0, (shown.height - height) // 2, shown.width, height))


def shades_for(theme, entries):
    """The ramp to write into an indexed image's table of `entries`.

    A table is sized by bit depth, not colour count: 1/2/4/8 bits index 2/4/16/256 entries,
    so eight shades at four bits arrive in a table of sixteen. The largest ramp that fits is
    the picture's, and nothing indexes the entries past it.
    """
    if entries in theme.image:
        return theme.image[entries]
    fits = [count for count in theme.image if count <= entries]
    return theme.image[max(fits)] if fits else None


def picture(theme, data):
    """An indexed image off the wire, in this theme's greys. None if it will not decode.

    The bytes carry indices and a grey ramp; the theme's is assigned over the top, so one
    write recolours every pixel and one picture suits every badge.

    Cached on the bytes: the same message is redrawn every frame until the host sends a
    different one.
    """
    if not data:
        return None
    held = _pictures.get(data)
    if held is not None:
        return held
    try:
        # base64, the frame being JSON, and keyed on the encoded string since that is what
        # arrives.
        img = image.load(binascii.a2b_base64(data))
    except (OSError, ValueError, TypeError):
        return None
    table = img.palette
    if table:
        greys = shades_for(theme, len(table))
        if greys:
            img.palette[0:len(greys)] = greys
    if len(_pictures) >= PICTURE_CACHE:
        _pictures.clear()
    _pictures[data] = img
    return img


def _item_block(theme, item, top, height):
    """One message: who it is from and how long ago, then what it says."""
    room = look.W - look.PAD * 2
    y = top + 6
    left = look.PAD
    shown = fitted(picture(theme, (item or {}).get("image")), height - 8)
    if shown is not None:
        # Down the left, so a picture belongs to the message; one above would start a page.
        screen.blit(shown, look.PAD, top + 4)
        left += shown.width + PICTURE_GAP
        room -= shown.width + PICTURE_GAP
    title = str((item or {}).get("title") or "")
    aged = ago((item or {}).get("age_s"))
    if aged:
        width = blit_label(aged, ITEM_TITLE, theme.dim, look.W - look.PAD, y, align=2)
        room -= width + 8
    note = str((item or {}).get("note") or "")
    if title:
        used = blit_label(fit(title, ITEM_TITLE, room), ITEM_TITLE, theme.accent, left, y)
        if note:
            # Boosted, a reply, a section: dim and beside the name, qualifying the line.
            blit_label(fit(note, ITEM_TITLE, room - used - 6), ITEM_TITLE, theme.dim,
                       left + used + 6, y)
        y += int(ITEM_TITLE * 1.45)
    flow(str((item or {}).get("text") or ""), ITEM_TEXT, theme.ink,
         rect(left, y, look.W - look.PAD - left, top + height - y - 4))


def _counter_row(theme, counters, top, bottom):
    """Up to four labelled figures along the bottom, each in its share of the width."""
    counters = counters[:4]
    width = (look.W - look.PAD * 2) // len(counters)
    screen.pen = theme.grid
    screen.hspan(look.PAD, top, look.W - look.PAD * 2)
    for index, (name, value_text) in enumerate(counters):
        x = look.PAD + index * width + width // 2
        blit_label(value_text, look.SIZE_VALUE, theme.ink, x, top + 5, align=1)
        blit_label(fit(name, look.SIZE_SMALL, width - 4), look.SIZE_SMALL, theme.dim,
                   x, bottom - 13, align=1)


def ago(seconds):
    """"3m ago", for a message. None where there is no age to draw.

    Public because every feed carries one and each of them would otherwise write this out
    again: a post, a mention and a headline are all "how long ago" to a reader.
    """
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def banner(theme, title, message, detail=None):
    """A full-screen notice: connecting, no host, an error.

    The box is sized to its lines and not fixed, since these strings carry
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

    # Trim anything that will not fit, so a long error shows as truncated.
    trimmed = [(fit(text, size, room), size, pen) for text, size, pen in lines]
    widest = max(screen.measure_text(text, font_size=size)[0]
                 for text, size, _ in trimmed)
    box_w = min(look.W - 24, max(200, int(widest) + pad_x * 2))
    x = (look.W - box_w) // 2
    y = (look.H - box_h) // 2

    screen.pen = theme.bg
    screen.rectangle(rect(0, 0, look.W, look.H))
    screen.pen = theme.accent
    screen.shape(shape.rounded_rectangle(rect(x, y, box_w, box_h), 8))
    screen.pen = theme.bg
    screen.shape(shape.rounded_rectangle(rect(x + 2, y + 2, box_w - 4, box_h - 4), 7))

    cursor = y + pad_y
    for (text, size, pen), height in zip(trimmed, heights):
        blit_label(text, size, pen, look.W // 2, cursor, align=1)
        cursor += height + gap


def readable(pen, over, toward):
    """`pen` if it can be seen on `over`, else the same hue stepped toward `toward`.

    A ramp is built to be seen against the page, and a pale palette's cold end lands within
    5 counts of its panel, so a low reading disappears. The hue is kept where it can be,
    half way to the ink usually clearing it.
    """
    for alpha in (255, 128):
        candidate = pen if alpha == 255 else pen.with_alpha(alpha).over(toward)
        if over.difference(candidate) >= SERIES_FLOOR:
            return candidate
    return toward


def fit(text, size, room):
    """Shorten a string until it fits `room` pixels, with an ellipsis if cut.

    Public because an extension drawing what the host sent needs it: a place name off a
    feed is whatever length it is.
    """
    if screen.measure_text(text, font_size=size)[0] <= room:
        return text
    # Halving, and not a character at a time: a string is only ever wider the longer it
    # gets, so the longest prefix that fits takes a handful of measurements. A post cut
    # from 160 characters to 35 is eight against a hundred and twenty five.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if screen.measure_text(text[:middle] + "...", font_size=size)[0] <= room:
            low = middle
        else:
            high = middle - 1
    return (text[:low] + "...") if low else text


# How long a toast lasts, and how much of that is the fade at the end.
TOAST_FADE_MS = 400


def toast(theme, message, fade=1.0):
    """A short-lived note over the footer, for a command that was sent.

    `fade` is how much of it to draw, 1 solid and 0 gone. The page under it is redrawn on
    every frame of the fade, so the note thins out over the page and not a copy of it.
    """
    if fade <= 0.0:
        return
    width = min(look.W - 40, 40 + len(message) * 7)
    x = (look.W - width) // 2
    y = look.H - look.FOOTER_H - 26
    if fade < 1.0:
        screen.alpha = int(255 * fade)
    screen.pen = theme.accent
    screen.shape(shape.rounded_rectangle(rect(x, y, width, 22), 6))
    blit_label(message, look.SIZE_LABEL, theme.bg, look.W // 2, y + 4, align=1)
    screen.alpha = 255


# -- rings ------------------------------------------------------------------

# Thin enough that four fit the dial's radius, keeping this page on a gauge's bounds.
RING_BAND = 14
RING_GAP = 4


def rings(theme, entries):
    """Concentric sweep gauges, outermost first, with a legend down the side.

    One arc per reading. The stack sits where the single dial does, and the legend is that
    page's column of readouts.
    """
    rows = entries[:4]
    height = look.READOUT_NOTE_H if any(entry[4] for entry in rows) else look.READOUT_H
    for index, ((name, value_text, fraction, pen, note), y) in enumerate(
            zip(rows, look.readout_rows(len(rows), height))):
        ring_outer = look.DIAL_OUTER - index * (RING_BAND + RING_GAP)
        ring_inner = ring_outer - RING_BAND
        if ring_inner < 8:
            break
        # Track and fill abut: four rings over their tracks is twice the arc for one picture.
        sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * at_axis(fraction)
        screen.pen = theme.grid
        if look.DIAL_TO - sweep > 0.5:
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   sweep, look.DIAL_TO))
        if fraction:
            screen.pen = pen
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   look.DIAL_FROM, sweep))
        # The legend doubles as the reading, so the rings carry no labels.
        readout(theme, y, name, value_text, fraction, note, chip=pen if note else None)


# -- sparklines -------------------------------------------------------------

# How one row is told from the next. Banded by default: six lines otherwise read as one
# plot with six traces.
ROWS = "zebra"
ROW_NONE = "none"


def sparklines(theme, entries):
    """A row per reading: name, current value, and its history as a small line.

    Six fit the body band. A line and not a filled area, at 1.2ms a page more: filled to its
    axis, a plot 22px tall is a slab of colour on any steady reading.

    Still between readings, whatever the animation setting: a sample is 5px, and
    interpolating at fixed x shows as a jump and not a scroll.

    The axis rule under each plot is drawn only where the rows are otherwise unseparated.
    """
    rows = entries[:6]
    if not rows:
        return
    height = min(30, (look.BODY_H - 8) // max(1, len(rows)))
    # The plot takes what the two text columns leave.
    name_w = column_width([row[0] for row in rows], look.SIZE_LABEL)
    value_w = column_width([row[1] for row in rows], look.SIZE_LABEL)
    plot_x = look.PAD + name_w + COLUMN_GAP
    plot_w = max(40, look.W - plot_x - COLUMN_GAP - value_w - look.PAD)
    # The whole width of the row, or the name and reading fall outside their own band.
    if ROWS == "zebra":
        screen.pen = theme.stripe
        for index in range(1, len(rows), 2):
            screen.rectangle(rect(0, look.BODY_TOP + 2 + index * height, look.W, height))
    elif ROWS == "rules":
        # What a rule is drawn in everywhere else.
        screen.pen = theme.grid
        for index in range(1, len(rows)):
            screen.hspan(look.PAD, look.BODY_TOP + 2 + index * height,
                         look.W - look.PAD * 2)
    for index, (name, value_text, points, peak) in enumerate(rows):
        top = look.BODY_TOP + 6 + index * height
        mid = top + height // 2
        blit_label(name, look.SIZE_LABEL, theme.dim, look.PAD, mid - 7)

        plot_h = height - 8
        if ROWS == ROW_NONE:
            screen.pen = theme.grid
            screen.hspan(plot_x, top + plot_h + 3, plot_w)
        trace = (line(plot_x, top, plot_w, plot_h, points, peak)
                 if points and len(points) > 1 and peak else None)
        if trace is not None:
            screen.pen = theme.accent
            was = screen.clip
            screen.clip = rect(plot_x, look.BODY_TOP, plot_w, look.BODY_H)
            screen.shape(trace)
            screen.clip = was
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
    # An ellipse: a label runs 15px below its anchor and four readings put an axis straight
    # down, so a circle wide enough for the 300px band spills it into the page indicator.
    radius_x, radius_y = 70, 56
    count = len(rows)

    def point(index, fraction):
        # Axes start at twelve and run clockwise, matching the gauges.
        angle = math.radians(index * 360.0 / count - 90.0)
        return vec2(centre[0] + math.cos(angle) * radius_x * fraction,
                    centre[1] + math.sin(angle) * radius_y * fraction)

    screen.pen = theme.grid
    for step in (0.5, 1.0):
        web = [point(i, step) for i in range(count)]
        for i in range(count):
            here, then = web[i], web[(i + 1) % count]
            screen.line(here, then, 1)
    for i in range(count):
        screen.line(vec2(*centre), point(i, 1.0), 1)

    filled = [point(i, at_axis(row[2])) for i, row in enumerate(rows)]
    screen.pen = theme.accent
    screen.alpha = 150
    screen.shape(shape.custom(filled))
    screen.alpha = 255
    for corner in filled:
        screen.shape(shape.circle(corner, 3))

    for i, (name, value_text, _fraction, _pen) in enumerate(rows):
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

def trend(theme, value_text, unit_text, name, delta, points, peak, fraction,
          hot=None, shift=None):
    """One big reading, which way it is going, and where it has been."""
    blit_label(name, look.SIZE_LABEL, theme.dim, look.PAD + 2, look.BODY_TOP + 8)
    reading_w = blit_label(value_text, look.SIZE_HUGE, theme.ink, look.PAD,
                           look.BODY_TOP + 26)
    if unit_text:
        blit_label(unit_text, look.SIZE_BIG, theme.dim,
                   look.PAD + reading_w + 4, look.BODY_TOP + 48)

    if delta is not None:
        x = look.W - look.PAD
        blit_label(f"{abs(delta):.1f}", look.SIZE_VALUE, theme.ink, x, look.BODY_TOP + 30,
                   align=2)
        # Drawn, not written: the text font has no arrows, and a missing glyph is a silent gap.
        _arrow(theme, x - 46, look.BODY_TOP + 34, delta,
               fraction if hot is None else hot)


    top = look.BODY_TOP + 92
    height = look.BODY_H - 100
    left = look.PAD
    width = look.W - look.PAD * 2
    screen.pen = theme.grid
    screen.hspan(left, top + height, width)
    filled = (area(left, top, width, height, points, peak, shift=shift)
              if points and len(points) > 1 and peak else None)
    if filled is not None:
        screen.pen = theme.accent
        screen.alpha = 170
        was = screen.clip
        screen.clip = rect(left, look.BODY_TOP, width, look.BODY_H)
        screen.shape(filled)
        screen.clip = was
        screen.alpha = 255


def _arrow(theme, x, y, delta, fraction):
    """A triangle for the direction, flat where the reading is holding still."""
    half, height = 9, 11
    if delta > 0.05:
        screen.pen = theme.at(fraction) if fraction is not None else theme.ink
        screen.shape(shape.custom([vec2(x, y - height), vec2(x + half, y),
                                   vec2(x - half, y)]))
    elif delta < -0.05:
        screen.pen = theme.dim
        screen.shape(shape.custom([vec2(x, y), vec2(x + half, y - height),
                                   vec2(x - half, y - height)]))
    else:
        screen.pen = theme.dim
        screen.rectangle(rect(x - half, y - height // 2 - 2, half * 2, 4))


# -- waterfall --------------------------------------------------------------

# The scroll buffer, its write cursor, and the lane count it was built for. One column a
# frame, shown as two windowed blits: copying the image onto itself is 11ms against 7ms.
_wf_image = None
_wf_cursor = 0
_wf_lanes = 0

WF_LEFT = look.PAD + 22
WF_TOP = look.BODY_TOP + 6


@clears
def waterfall_reset():
    global _wf_image, _wf_cursor, _wf_lanes
    _wf_image = None
    _wf_cursor = 0
    _wf_lanes = 0


def waterfall(theme, lanes, labels=None):
    """One column per call, scrolling left: a lane per value, coloured by the ramp.

    Time is in frames, not samples: the caller interpolates between polls and this draws
    wherever that got to. The ramp carries the reading, so the lost precision does not show.
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
        _wf_image.pen = theme.bg
        _wf_image.rectangle(rect(0, 0, width, height))
        _wf_cursor = 0
        _wf_lanes = len(lanes)

    lane_h = height / float(len(lanes))
    for index, fraction in enumerate(lanes):
        part = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
        top = int(index * lane_h)
        bottom = int((index + 1) * lane_h)
        _wf_image.pen = theme.at(part)
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
