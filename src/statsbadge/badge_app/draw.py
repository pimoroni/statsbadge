"""Drawing the pages.

Vector shapes throughout, so a theme is a colour table. The costs that shape this
(measured on this board, in DEVELOPMENT.md): an anti-aliased shape is about 0.08ms
plus 8us an edge and almost nothing for its fill, a line of live text is about 1ms,
and the same text blitted from a cache is 0.08ms. So shapes are drawn live and every
string that is not new every frame is baked into a sprite once.

The header and footer are drawn where they stand, from raster fills and cached labels.
Only the pip row is baked, being rounded rectangles.

A page that splits into something round and a column of text beside it - the single dial,
the ring stack, an extension's clock face - takes its geometry from `look.DIAL_C`,
`look.DIAL_OUTER` and `look.READOUT_X`, and its rows from `look.readout_rows` and either
`readout` or `column_lines` here. Nothing in a split page should place text on a number of
its own: the pages are paged between, and anything choosing its own margin moves under the
reader when they press a button.
"""

import os
from array import array

import look

FONT = None
_labels = {}
_pip_rows = {}

# Fonts by name, so a sprite cache key can say which one drew it. TEXT is the app's own;
# `icons` is registered by prepare() when the .af is there, and an extension adds its own
# with add_font. Without the name in the key an icon and a letter of the same string would
# collide, and one of them would be drawn in the wrong font.
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
    # TEXT is the role; look.FONT_NAME is which font is filling it, so use_font can name
    # either and a candidate can be tried against the shipped one.
    _fonts[TEXT] = FONT
    _fonts[look.FONT_NAME] = FONT
    screen.font = FONT
    add_font(ICONS, look.ICON_FILE)


def add_font(name, *paths):
    """Register a font under a name, from the first of `paths` that loads.

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
            return True
    return False


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

# Text this size and over is drawn where it stands and never kept. A sprite is blitted at
# 187ns a pixel and glyphs rasterise faster than that once they are large: at 104pt the blit
# is 3.01ms against 1.27ms to draw the text, and the sprite is 130KB. Below it the cache is
# the right way round - 0.08ms against 0.22ms at 11pt, 0.32ms against 0.64ms at 17pt - and a
# page that redraws at frame rate takes eight hits a frame off it.
CACHE_UNDER = 40

# A string is only worth a picture the second time it is asked for. Every reading that moves is
# a new key, and baking each one fills the heap with several hundred images of assorted sizes:
# measured with tools/mem_probe.py, that holds 221 sprites at a time, drops the lot every eighty
# frames, and leaves a largest contiguous free run of 70KB out of 7.9MB free - which the map's own
# 827KB parse would not fit in.
#
# Furniture - a name, a title, a unit - is redrawn every frame and so bakes on the frame after its
# first, which is where the saving is. A value that comes and goes is drawn where it stands, which
# is cheaper than baking it anyway: 297B and 1.23ms live against 4695B and 2.54ms to bake.
_once = set()
# Enough that a string which comes round again inside a few seconds is still remembered, and so
# bakes: a sprite is 100B and 0.26ms to blit, so what repeats is worth one. Keys, not pictures:
# this is about 50KB full.
ONCE_MAX = 512


def label(text_value, size, pen, name=TEXT):
    """A string baked into a sprite, or None if it should be drawn where it stands.

    None for a string too large to be worth keeping, and for one not seen before: see `_once`.
    A caller that needs the sprite - to place something against its width - should ask
    `text_width` and draw with `blit_label`, which handles both sides of that line.
    """
    if size >= CACHE_UNDER:
        return None
    key = (name, text_value, size, pen)
    cached = _labels.get(key)
    if cached is not None:
        return cached
    if key not in _once:
        if len(_once) > ONCE_MAX:
            # Only ever holds keys, so this is a few kilobytes of tuples going, not pictures.
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
        # A ceiling for the furniture of a badge that has been through every page and every
        # theme. Dropped wholesale rather than aged: only what has been asked for twice is in
        # here, so there is little to lose.
        _labels.clear()
    _labels[key] = sprite
    return sprite


def blit_label(text_value, size, pen, x, y, align=0, name=TEXT):
    """Draw a string. align 0 left, 1 centre, 2 right, about x.

    From a sprite where one is worth keeping and live where it is not, which the caller does
    not have to know about. Returns the width drawn, or 0 for a font that is not loaded -
    which is what lets a caller offer an icon and fall back to words without asking first.
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

    Called when the theme changes or the host does, so it has to include anything holding
    colours and not only the sprites: the waterfall's scroll buffer is a second of columns
    painted in the ramp they were drawn with, and it showed the old theme's for a whole
    screen's width after a switch.
    """
    _labels.clear()
    _once.clear()
    _pip_rows.clear()
    _readings.clear()
    _gradients.clear()
    waterfall_reset()


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
        width, _ = screen.measure_text(text_value, font_size=size)
    finally:
        screen.font = was
    return int(width) + 2


# What the app's fonts are built to: a capital stands 81 units of a 128 unit em, and
# tools/make_icon_font.py fits an icon to a box of 100 of the same, sat on the baseline. A
# wide font keeps both ratios, its em being the same em at a finer grid, and the decoder
# scales whichever em to the size asked for - so these are fractions of any size, in any of
# the app's fonts. An icon's box is a quarter taller than a capital, with its ink centred
# half way up it.
CAP_UNITS, ICON_UNITS, EM_UNITS = 81.0, 100.0, 128.0
CAP = CAP_UNITS / EM_UNITS
ICON_BOX = ICON_UNITS / EM_UNITS


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

    One measurement, not one per string: `measure_text` breaks on newlines and returns the
    widest line, so sixteen readings cost 0.2ms where a string at a time was 2.8ms.
    """
    if not texts:
        return 0
    return text_width("\n".join(texts), size, name)


# A column can also be drawn as one bounded `screen.text` call, the native layout placing
# each line - `line_height` is `pitch / size`, the font's natural advance being its size.
# Measured, that loses to the sprite cache: cores 24.2ms against 22.6 and the text page 14.4
# against 10.3, because a live glyph is ~60us and a whole string blitted is 0.2ms. It only
# wins with the cache off.


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


# How many figures a slot will show before it gives up and says how many there are. Three
# is what a load average is: the run queue averaged over one, five and fifteen minutes, and
# the three of them together are the reading - the short window above the long one is load
# climbing. Per-core loads are the other list a field can hold, and sixteen of them do not
# go in a slot at all; three of the sixteen would be a lie, so the slot says what it has
# and the reader can put the field on a bars page instead.
SEVERAL = 3


def _several(values, field):
    if not values:
        return "--"
    if len(values) <= SEVERAL:
        return " ".join(fmt(item, field) for item in values)
    return f"{len(values)} values"


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


# What a value came out as, since a page redraws at frame rate from numbers that change
# once a second. Formatting one is 305us - the float format is 58us of it and each
# isinstance check 23us - against 21us to look it up. Sixteen bars a frame is 4.9ms of
# formatting the same sixteen numbers over and over.
_readings = {}


def reading(value, field):
    """A value with its unit, for a slot that has no room to place one separately.

    `fmt` already carries a prefix where the number was scaled - 12.3G, 50.0M - and
    short_unit adds the base after it, so a rate reads 50.0MB/s and a percentage reads
    9.2%. A reading that is not there gets no unit: there is no such thing as
    "-- percent".
    """
    # Only a number is remembered. It is what costs, and it is the only kind of value that
    # can be a key at all: a field can arrive as a list - core loads, a load average - and a
    # list has no hash.
    if type(value) is float or type(value) is int:
        key = (value, field)
        text = _readings.get(key)
        if text is not None:
            return text
        text = fmt(value, field) + short_unit(field)
        if len(_readings) > 240:
            # A reading per field per poll, so this fills with history nobody will ask for
            # again. Dropped wholesale, like the sprites.
            _readings.clear()
        _readings[key] = text
        return text
    text = fmt(value, field)
    if value is None or isinstance(value, (str, bool, list, tuple)):
        # No unit on a list: the figures carry their own sense - a load average is a queue
        # length and not a percentage of anything - and "16 values%" is nonsense.
        return text
    return text + short_unit(field)


# -- chrome -----------------------------------------------------------------

def background(theme, title, index, total, subtitle=None):
    """The header, the footer and a cleared body, drawn where they stand.

    Raster fills and two cached labels: 2.1ms, against 3.9ms when the bands were baked
    into images and blitted, a blit being 180ns a pixel and a fill 10ns. Only the pip
    row is baked, because a rounded rectangle apiece is 0.19ms and there can be a dozen
    of them.
    """
    screen.pen = theme.bg
    screen.rectangle(rect(0, look.HEADER_H, look.W, look.BODY_H))
    furniture(theme, title, index, total, subtitle)


def furniture(theme, title, index, total, subtitle=None):
    """The header and footer alone, leaving the body as it stands.

    So a page turn can say where it is going before the body gets there: the title and the
    pip are the page you are on, and during a slide - or while a burst of presses settles -
    they should already be the page you pressed for.
    """
    screen.pen = theme.panel
    screen.rectangle(rect(0, 0, look.W, look.HEADER_H))
    screen.rectangle(rect(0, look.H - look.FOOTER_H, look.W, look.FOOTER_H))
    # The chrome takes the second accent where a palette has one, leaving the first for what a
    # reading is drawn in. Where it has none the two are the same colour, which is every theme
    # that was written down before there was a second.
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, look.HEADER_H - 2, look.W, 2))
    blit_label(title.upper(), look.SIZE_TITLE, theme.ink, look.PAD, 4)
    if subtitle:
        blit_label(subtitle, look.SIZE_SMALL, theme.dim, look.W - look.PAD, 10, align=2)
    if total > 1:
        row = _pips(theme, index, total)
        screen.blit(row, vec2((look.W - row.width) // 2,
                              look.H - look.FOOTER_H + look.FOOTER_H // 2 - 2))


# The pips have this much of the width to themselves. A dash shortens as they pack in,
# down to a dot and no further: a mark thinner than it is tall stops reading as a mark.
PIP_ROOM = look.W - look.PAD * 4
PIP_MAX_W, PIP_GAP, PIP_DOT, PIP_TIGHT = 14, 5, 4, 2


def _pips(theme, index, total):
    """The pip row as a sprite, one pip per page and the current one in the accent colour.

    Shortens to fit, and tightens the spacing before it gives up any more length. Enough
    pages to fill the row even as dots is tough luck: it is a badge with six buttons and
    nobody is paging through forty screens.

    Baked, and no wider than the pips themselves: a rounded rectangle is 0.19ms whatever
    its size, so a dozen of them every frame is more than the whole footer is worth. The
    row changes only with the page, so a handful are kept.
    """
    key = (theme.name, index, total)
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

# How the big gauge fills, from the layout. "solid" is the ramp's colour for the reading;
# "ramp" lays the whole ramp round the arc and leaves what the reading has not reached faint,
# so the scale it is climbing shows behind it.
GAUGE_FILL = "solid"
# How faint that is. Per stop, because a gradient brush ignores screen.alpha: measured, a solid
# pen blends at a layer alpha and a gradient does not move.
TRACK_ALPHA = 32
_gradients = {}


def swept_pens(theme, centre, radius, backwards=False):
    """The theme's ramp round a gauge: what fills the sweep, and what sits behind it.

    A conical's stops are fractions of a whole turn, so a 270 degree gauge lays the ramp over
    three quarters of one, and the brush's second point is the direction it starts in -
    DIAL_FROM clockwise from straight up, which is the convention arc() uses, so the two line up
    with no fixup. Unlike a linear gradient it follows the curve, which is what makes the hue
    track the reading.

    `backwards` is for a field whose severity runs the other way, a battery not being in trouble
    at 100%: the colour comes from the angle here and not from a lookup, so the ramp itself has
    to be reversed for the sweep's end to be the reading's own colour.

    Cached: a pair built from OKLCH stops is 3.4ms, where the arc costs the same to draw either
    way.
    """
    key = (theme.name, centre, radius, backwards)
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

    `shape.arc(centre, inner, outer, from, to)` - angles start at the top and run
    clockwise, so look.DIAL_FROM..DIAL_TO is 225..495 and the gap lands at the bottom,
    which is where `under` goes.

    `hot` is where this reading sits on the ramp, for a field where that is not where it
    sits on its own scale: a battery at 100% is not a machine in trouble. It colours the
    sweep and nothing else, the sweep's length being the reading either way.

    `icon` is drawn there instead where the font has it, and `under` is what it falls back
    to. `unit` is a small suffix on the reading, for a gauge whose slot below is already
    spoken for - the single dial puts its unit below instead, having nothing else to put
    there. It is dropped rather than allowed to spill out of a small ring.

    `swept` is a (fill, track) pair from `swept_pens`, for a gauge showing the whole ramp round
    its arc. Without one the sweep is the single ramp colour for the reading and the track is
    the grid.
    """
    value_size = value_size or look.SIZE_HUGE
    label_size = label_size or look.SIZE_LABEL
    middle = vec2(*centre)
    start, end = look.DIAL_FROM, look.DIAL_TO
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    fill, track = swept if swept else (None, None)

    # The track is only the part the sweep does not cover. The two abut rather than one
    # being drawn over the other, which halves the arc a full gauge rasterises, and the join
    # is under the tick below in any case.
    lit = not cold and fraction > 0.001
    sweep = start + (end - start) * fraction if lit else start
    screen.pen = theme.grid if track is None or cold else track
    if end - sweep > 0.5:
        screen.shape(shape.arc(middle, inner, outer, sweep, end))

    if lit:
        # One colour is where this reading sits on the ramp, so the colour *is* the severity;
        # the swept fill is the whole ramp laid round the arc, so the scale shows as well as
        # the reading. Either costs one shape.
        screen.pen = (theme.at(fraction if hot is None else hot) if fill is None else fill)
        screen.shape(shape.arc(middle, inner, outer, start, sweep))

        # A brighter tick at the sweep's end, so the exact value is readable, and it lands
        # on the join between the two arcs.
        screen.pen = theme.ink
        screen.shape(shape.arc(middle, inner - 3, outer + 3, sweep - 1.4, sweep + 1.4))

    ink = theme.dim if cold else theme.ink
    top = centre[1] - value_size * 0.62
    unit_size = max(look.SIZE_SMALL, int(value_size * 0.45))
    reading_w = text_width(value_text, value_size)
    suffix_w = text_width(unit, unit_size) if unit else 0
    if suffix_w and reading_w + suffix_w > inner * 2 - 4:
        # Kept inside the ring rather than allowed over the arc, so a gauge too small for
        # its unit shows the reading and the name under it and nothing else. A scaled
        # figure carries its prefix on the number, so 11.0M still says which 11 it is.
        suffix_w = 0
    left = centre[0] - (reading_w + suffix_w) // 2
    blit_label(value_text, value_size, ink, left, top)
    if suffix_w:
        # Sat on the reading's own baseline, which is where the eye expects a unit. Text
        # puts its baseline `size` below where it is drawn, so the drop is the difference
        # in sizes.
        blit_label(unit, unit_size, theme.dim, left + reading_w,
                   top + value_size - unit_size)
    below = centre[1] + value_size * 0.42
    if icon and blit_icon(icon, label_size + 8, theme.dim, centre[0], below, align=1):
        return
    if under:
        blit_label(under, label_size, theme.dim, centre[0], below, align=1)


def dial(theme, fraction, value_text, unit_text, cold=False, hot=None, backwards=False):
    """The single gauge of a `dial` page, with its readouts beside it.

    The one gauge with a page to itself, and the only one big enough for a ramp round it to be
    read, so this is where the swept fill is offered.
    """
    gauge(theme, look.DIAL_C, look.DIAL_OUTER, look.DIAL_INNER, fraction, value_text,
          unit_text, cold=cold, hot=hot,
          swept=swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, backwards)
          if GAUGE_FILL == "ramp" else None)


def dials(theme, entries):
    """Up to four gauges across the body band, each named under its reading.

    One page kind rather than one per count: how many fields the page carries is the only
    thing that changes, so it picks the layout and nothing else has to be decided.
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

    Every page that draws a gauge and a column draws this row, so nothing moves when you
    page between them. `chip` is the colour of the ring a row belongs to, which is what ties
    the two together on a page where the gauge is not the bar.
    """
    x = look.READOUT_X
    blit_label(name, look.SIZE_SMALL, theme.dim, x, y)
    blit_label(value_text, look.SIZE_VALUE, theme.ink, x, y + 10)
    if chip:
        screen.pen = chip
        screen.rectangle(rect(x + look.READOUT_W - 10, y + 3, 10, 10))
    if note:
        # What a full ring is, for a reading whose scale is not a round number. It takes
        # the bar's place: the ring it belongs to is already the bar.
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


# Between one line of a free-form column and the next, on top of the line's own height.
COLUMN_LEAD = 3


def column_lines(entries, top=None, align=0):
    """A stack of lines down the column beside a gauge, each `(text, size, pen)`.

    For a page whose rows are not readouts - a clock's time, place and date - so that it
    gets the column's left edge and a consistent rhythm without working either out. Empty
    strings are skipped, so a caller can offer a line it may not have.

    Returns the y after the last line, for a page that has more to place by hand.
    """
    y = (look.BODY_TOP + 12) if top is None else top
    x = look.READOUT_X + (look.READOUT_W if align == 2 else 0)
    for text_value, size, pen in entries:
        if not text_value:
            continue
        blit_label(text_value, size, pen, x, y, align=align)
        y += int(size * 1.35) + COLUMN_LEAD
    return y


def bars(theme, values, maximum=100.0, field="pct", fractions=None):
    """A stack of horizontal bars. Raster rectangles: no AA needed on an axis-aligned
    bar, and this is the one page that can have 32 of them.

    `field` is what the values are, so a per-core load reads as a percentage. Without it
    every bar was a bare number.

    `fractions` is where each bar should be drawn to, for a caller sweeping them to their
    readings; without it each bar is drawn at its own value, which is the same thing once a
    sweep has landed.
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
        if fractions is None:
            fraction = max(0.0, min(1.0, value / maximum if maximum else 0.0))
        else:
            fraction = fractions[i]
        y = top + i * slot
        blit_label(names[i], look.SIZE_SMALL, theme.dim, look.PAD, y - 1)
        filled = max(1, int(width * fraction)) if fraction > 0 else 0
        screen.pen = theme.grid
        # From where the fill ends, so the two meet instead of overlapping. Exact: an
        # axis-aligned raster edge is a pixel boundary, not an anti-aliased one.
        screen.rectangle(rect(x + filled, y, width - filled, height))
        if filled:
            screen.pen = theme.at(fraction)
            screen.rectangle(rect(x, y, filled, height))
        blit_label(readings[i], look.SIZE_SMALL, theme.ink,
                   look.W - look.PAD, y - 1, align=2)


# Whether a series is drawn as a curve through its samples or as a polyline between them.
# Set from the layout, so it is one switch for every graph on the badge.
SMOOTH = True
# Points per span between two samples. Two puts a segment about two pixels across on a plot
# of 48 samples in 250, which is where the corners stop reading; four was indistinguishable
# from it against deliberately spiky data and cost 6ms a page.
CURVE_STEPS = 2
# A curve needs height to show. Interpolating a sparkline 22px tall gives back the same
# picture for 1.7ms a series, so a plot shorter than this is drawn straight.
SMOOTH_MIN_H = 40
_weights = {}


def _basis(steps):
    """The Catmull-Rom weights for each fraction of a span, worked out once.

    The four weights depend only on t, so a curve of any length reuses `steps` sets of them
    and each output point costs four multiplies an axis. Evaluating the polynomial per point
    instead cost 265us a point on this board, which is 50ms for one series.
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

    A segment shorter than a pixel buys nothing and costs the same as one that shows, so a
    narrow plot is subdivided less, and a short one not at all: the curve is only visible if
    there is room for it to bend.
    """
    if not SMOOTH or count < 3 or height < SMOOTH_MIN_H:
        return 1
    return max(2, min(CURVE_STEPS, int(width / (count - 1))))


def curve(values, steps=CURVE_STEPS):
    """`values` resampled to a Catmull-Rom curve through them, evenly spaced as they were.

    A graph is a polyline with a corner at every sample, and the corners are what reads as
    jagged. Catmull-Rom passes *through* each sample rather than near it, so the shape is
    smoothed without the reading moving: the peak drawn is still the peak measured.

    Only the values are interpolated, the samples being evenly spaced along the axis - so a
    caller lays the output out the same way it laid out the input, over one more point per
    step. Returned as it came when there is nothing to interpolate or SMOOTH is off.

    A spline overshoots where the data turns sharply, so the output is held within the range
    of the input: inside that the bulge is what makes a curve read as one, but past the
    lowest sample an area fill would run under its own baseline.
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

# How many samples of room a moving plot keeps on its right for the ones still coming in. Held
# still rather than derived from the current offset, which would resize the plot every frame.
WALK_LEAD = 2


def _lay_out(left, top, width, height, values, peak, shift):
    """`values` scaled against `peak` and laid across the box, in the shared float buffer.

    Returns how many floats were written, or 0.

    `shift` is how far the plot has walked left since its last update, in samples: 0 is just
    after one landed. `None` means the plot is not walking at all, which is a different layout
    and not the same as a shift of zero - a moving plot is laid out `WALK_LEAD` samples wider
    than its box and clipped to it, so the samples still to come slide in at the right as the
    oldest leave at the left.

    The plot is smoothed first if it is tall enough to show a curve, then scaled and laid out
    in one pass: `shape.custom` takes a float buffer, so no point is boxed as a vec2 - 2.3ms
    against 3.7 for 191 points, and the same pixels. Scaling here rather than in a list the
    caller passes saves a pass over every sample, which was 14.7us a point and 4.2ms of the
    sparkline page.
    """
    global _points
    count = len(values)
    if count < 2:
        return 0
    steps = curve_steps(width, height, count)
    if steps > 1:
        values = curve([value or 0.0 for value in values], steps)
        count = len(values)
    if len(_points) < (count + 2) * 2:
        _points = array("f", bytes((count + 2) * 8))
    # A sample of the original data, however many points it was interpolated to, so a shift
    # of one moves the plot by one reading whether it is smoothed or not.
    # Points per original sample, so a shift of one moves the plot by one reading whether the
    # series was interpolated or not.
    per_sample = steps if steps > 1 else 1
    # Walking, the samples still to come in are laid *past* the right edge and slide in as the
    # plot moves, so the box stays full. Laid across the width alone the whole plot simply
    # shifts left, and the gap it leaves at the right grows to a sample's width before
    # snapping back - which reads as the plot periodically shrinking.
    lead = per_sample * (WALK_LEAD if WALK_LEAD > 1 else 1)
    # A quarter of the plot at most. Headroom is space the samples are not drawn in, so a
    # badge far enough behind to want more than that gets a shorter walk instead of a plot
    # squeezed into the corner of its own box.
    if lead > count // 4:
        lead = count // 4
    walking = shift is not None
    span = count - 1 - lead if walking and count > lead + 1 else count - 1
    step = width / float(span)
    scale = height / float(peak or 1.0)
    bottom = top + height
    # Past the headroom the plot really is short of data, and the honest thing is to let it
    # move and leave the gap: `graph` draws that region as one rather than pretending the
    # newest reading is now.
    away = shift * step * per_sample if walking else 0.0
    start = left - away
    i = 0
    for index in range(count):
        y = bottom - (values[index] or 0.0) * scale
        _points[i] = start + index * step
        _points[i + 1] = top if y < top else (bottom if y > bottom else y)
        i += 2
    return i


def area(left, top, width, height, values, peak, base=None, shift=None):
    """One filled area from `values` against `peak`, closed along its base. A shape, or None.

    Where the base sits is a caller's business, a sparkline's axis being under its plot
    rather than at the foot of it.
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


# A plot drawn as a line rather than as a fill. Anti-aliased, so it costs its edges: the
# weight is free - 2.0 and 2.5 time the same as 1.5 - but the join is not, a round one being
# an arc at every one of 48 vertices and 3.5ms a page more than a miter. Centred on the
# samples, or the band grows to one side of its own data.
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


# What an axis with no full scale of its own tops out at: one of these times a power of the
# base the reading is formatted in. Stepped rather than fitted to the data, because a scale
# fitted to the window creeps with every sample that arrives or leaves - the plot rescaling
# slightly on each poll, which reads as the whole graph twitching. It also settles the
# gutter, whose width is the label's: a stepped axis shows one of a few strings.
AXIS_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500)


def axis_top(peak, field):
    """The round number an axis tops out at, at or above `peak`.

    In the base the reading is scaled by, so a byte rate steps 1024 at a time and says
    5.0MB/s rather than 4.8: a label the reader can place a sample against is the point of
    having one.
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

    Each series is one `shape.custom` contour: a polyline across the top and back
    along the bottom. One shape is one anti-aliased edge and one setup cost, where a
    line per sample would be dozens.

    A field with a full scale is drawn against it with headroom above; one without - a
    throughput has none of its own - is drawn against the next round number up from the
    busiest sample on the plot.
    """
    field = labels[0][1] if labels else "pct"
    if maximum is None:
        peak = axis_top(max((max(s) for s in series if s), default=1.0), field)
    else:
        peak = max(maximum, 1.0) * 1.15

    peak_text = reading(peak, field)
    # The gutter holds the scale, which is as wide as the scale is: 100% and 9.8MB/s do
    # not need the same room.
    left = look.PAD + column_width((peak_text, "0"), look.SIZE_SMALL) + 4
    top = look.BODY_TOP + 8
    width = look.W - left - look.PAD
    height = look.BODY_H - 26

    screen.pen = theme.grid
    for i in range(5):
        y = top + int(height * i / 4.0)
        screen.hspan(left, y, width)

    # Where the series has run out: the host has not answered for longer than a plot can cover
    # with what it holds. Drawn rather than papered over, because a stalled host and an idle
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
        # The plot is drawn a sample wider than its box while it walks left, so the oldest
        # reading leaves at the gutter rather than over it.
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


# What the two series are drawn at. The first is nearly solid and the second lets it show
# through, so two areas that overlap still read as two - except on a pale page, where a
# translucent area washes out towards it and the second may as well be as solid as the first.
SERIES_ALPHA = (200, 150)
# How far from the page a series has to land, as `difference` measures it, to count as
# visible: black to white is 100, and about 5 is where a difference becomes obvious. Measured
# against every theme, only luminescence falls through, at 13.8, and the next nearest is mono
# at 24.9 - so the threshold sits between them with room either side.
SERIES_FLOOR = 20


def _series_alpha(theme, index):
    return SERIES_ALPHA[0] if index == 0 or theme.pale else SERIES_ALPHA[1]


def _series_colour(theme, index):
    """Colours for the two graph series: the accent, and whichever end of the ramp is
    furthest from it that can actually be seen.

    The two areas overlap and are drawn semi-transparent, so a near miss reads as one
    series and takes the legend with it. Which end is further depends on the theme:
    the default theme's teal accent takes the hot end, mono's near-white the cold one.

    Both ends are tried, furthest first. A theme built out of one hue has the page at one
    end of its own ramp, and an area drawn in that is not there at all - but the other end
    usually is, and taking it beats giving up on the ramp. Measured across the themes,
    trying both leaves none of them needing the fallback, where taking the furthest end
    and no other left four of them either invisible or grey.
    """
    if index == 0:
        return theme.accent
    alpha = _series_alpha(theme, index)
    # A palette's own second colour, where it has one: the theme said what to use here, so
    # nothing has to be worked out from the ramp. It is still checked against the page.
    if theme.accent_b != theme.accent:
        if theme.bg.difference(theme.accent_b.with_alpha(alpha).over(theme.bg)) >= SERIES_FLOOR:
            return theme.accent_b
    cold, hot = theme.at(0.0), theme.at(1.0)
    order = ((cold, hot) if theme.accent.difference(cold) >= theme.accent.difference(hot)
             else (hot, cold))
    for pen in order:
        if theme.bg.difference(pen.with_alpha(alpha).over(theme.bg)) >= SERIES_FLOOR:
            return pen
    # Neither end shows, which takes a palette whose ramp is the page at both ends. The dim
    # colour does not track a reading, so it is the last resort rather than a choice.
    return theme.dim


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
        # Both: a cell has room for the name and for a symbol in the far corner, so the
        # symbol is another way to find the tile rather than the only one. A gauge has
        # room for one or the other and takes the symbol.
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 7, y + 5)
        if icon:
            blit_icon(icon, look.SIZE_VALUE, theme.dim, x + cell_w - 7, y + 4, align=2)
        size = look.SIZE_BIG if rows < 3 else look.SIZE_VALUE
        blit_label(value_text, size, theme.ink, x + 7, y + cell_h // 2 - size // 2 + 2)


# The badge's own page: two columns and a plate under them. The columns are the same width so
# the two halves read as one table, and the plate is where anything too long for a column goes.
VITALS_METERS = 5
VITALS_FACTS = 5
VITALS_NOTE_H = 12
# The bar under a level. Four rather than the readout column's three: this page is a wall of
# them and they are what it is read by, where a readout's bar sits beside a gauge saying the
# same thing.
VITALS_BAR_H = 4


def vitals(theme, meters, facts, notes=()):
    """Levels down the left, figures down the right, and a plate of strings underneath.

    Its own widget because none of the others fit what a badge knows about itself: half of it
    is levels that want a bar and half is strings that want reading, and a name like "littlefs"
    is not a field on any host.
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
        # A level is read off its bar; the figure under the name is for looking closer, and it
        # is a compound one - used of total - where the right hand column's are single numbers.
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

    # Fitted rather than wrapped: these are one string each and the useful end is the front.
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

    For anything written in the colour of a reading. A ramp is built to be seen against the
    page, and a pale palette's cold end lands within 5 counts of its own panel - measured
    across the themes - so a low reading would be written in a colour that is not there. The
    hue is kept where it can be: half way to the ink usually clears it.
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
    cut = text
    while cut and screen.measure_text(cut + "...", font_size=size)[0] > room:
        cut = cut[:-1]
    return (cut + "...") if cut else text


# How long a toast takes to go, and how much of that is left when the fade starts. It holds
# at full strength while it is being read and then leaves; fading the whole time would make
# it look like it was never quite there.
TOAST_FADE_MS = 400


def toast(theme, message, fade=1.0):
    """A short-lived note over the footer, for a command that was sent.

    `fade` is how much of it to draw, 1 solid and 0 gone. The page under it is redrawn on
    every frame of the fade, so the note thins out over the page rather than over a copy of
    itself; the label is drawn from the sprite cache either way, `alpha` being a property of
    the blend and not of the string.
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
    for index, ((name, value_text, fraction, pen, note), y) in enumerate(
            zip(rows, look.readout_rows(len(rows), height))):
        ring_outer = look.DIAL_OUTER - index * (RING_BAND + RING_GAP)
        ring_inner = ring_outer - RING_BAND
        if ring_inner < 8:
            break
        # Track and fill abut, as they do on a single gauge: four rings drawn over their own
        # tracks is twice the arc for the same picture.
        sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * (fraction or 0.0)
        screen.pen = theme.grid
        if look.DIAL_TO - sweep > 0.5:
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   sweep, look.DIAL_TO))
        if fraction:
            screen.pen = pen
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   look.DIAL_FROM, sweep))
        # The legend doubles as the reading, so the rings need no labels on them. The chip
        # is only for a row whose scale note has taken the bar's place: where there is a
        # bar, it is already drawn in this ring's colour.
        readout(theme, y, name, value_text, fraction, note, chip=pen if note else None)


# -- sparklines -------------------------------------------------------------

# How one row is told from the next: a band behind every other row, a hairline between
# them, or nothing but the plots. Set from the layout. Banded by default - six lines on one
# page read as one plot with six traces otherwise. The colours are the theme's own
# `stripe`, which is a step from the page rather than a colour of its own, and `grid`.
ROWS = "zebra"
ROW_NONE = "none"


def sparklines(theme, entries):
    """A row per reading: name, current value, and its history as a small line.

    Six of these fit the body band, which is the point - one page that says what every
    other page says, at the cost of the detail a full graph gives.

    A line rather than a filled area, at 1.2ms a page more: a plot 22px tall filled to its
    axis is a slab of colour on any reading that holds steady, which says the level over
    again where the reading beside it already does, and says nothing about the shape.

    Still between readings, whatever the animation setting says. Six plots this small have
    nowhere to scroll - a sample is 5px - and interpolating them at fixed x is a horizontal
    translation whatever it is called, which reads as a jump rather than as points settling.

    The axis rule under each plot is drawn only when nothing else separates the rows: with a
    band or a hairline there it is a second line saying the same thing, and the row it
    belongs to is no longer in doubt.
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
    # Behind everything, and the whole width of the row: a band that stopped at the plot
    # would leave the name and the reading it belongs to outside it.
    if ROWS == "zebra":
        screen.pen = theme.stripe
        for index in range(1, len(rows), 2):
            screen.rectangle(rect(0, look.BODY_TOP + 2 + index * height, look.W, height))
    elif ROWS == "rules":
        # What a rule is drawn in everywhere else, this being one: the palette's `grid` is
        # the unfilled part of a gauge and a graph's rules.
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

    screen.pen = theme.grid
    for step in (0.5, 1.0):
        web = [point(i, step) for i in range(count)]
        for i in range(count):
            here, then = web[i], web[(i + 1) % count]
            screen.line(here, then, 1)
    for i in range(count):
        screen.line(vec2(*centre), point(i, 1.0), 1)

    filled = [point(i, row[2] or 0.0) for i, row in enumerate(rows)]
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
    """One big reading, which way it is going, and where it has been.

    The arrow and the change are the point: a number on its own does not say whether
    something is climbing.
    """
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
        # Drawn, not written: the text font carries no arrows, and a missing glyph is a
        # gap rather than an error.
        _arrow(theme, x - 46, look.BODY_TOP + 34, delta,
               fraction if hot is None else hot)


    # The history underneath, so the number has somewhere to have come from.
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
