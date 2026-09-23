"""Drawing the pages."""

import binascii
import os
from array import array

import look

FONT = None

_CLEARS = []


def _cached(empty):
    """Register a container of colours, or sprites painted in them, and return it."""
    _CLEARS.append(empty.clear)
    return empty


def clears(reset):
    """Register a function to run on a theme change, as a decorator."""
    _CLEARS.append(reset)
    return reset


_pictures = _cached({})
PICTURE_CACHE = 4

_labels = _cached({})
_pip_rows = _cached({})

TEXT = "text"
ICONS = "icons"
_fonts = {}


def prepare():
    """Load the fonts."""
    global FONT
    if FONT is None:
        if not add_font(look.FONT_NAME, look.FONT_FILE):
            print(f"draw: no {look.FONT_FILE}, falling back to the firmware's font")
            add_font(look.FONT_NAME, look.FALLBACK_FONT_PATH)
        FONT = _fonts.get(look.FONT_NAME)
    _fonts[TEXT] = FONT
    _fonts[look.FONT_NAME] = FONT
    screen.font = FONT
    add_font(ICONS, look.ICON_FILE)


def add_font(name, *paths):
    """Register a font under a name, from the first of `paths` that loads."""
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
            except Exception as exc:  # noqa: BLE001
                print(f"draw: could not load {candidate}: {exc}")
                continue
            return True
    return False


def _candidates(path):
    """Return the paths to try for a font, skipping /remote.

    font.load reads a /remote path as UTF-8, fails partway and wedges the REPL.
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
    """Draw text with a registered font from here on, returning False if it is missing."""
    global FONT
    face = _fonts.get(name)
    if face is None:
        return False
    _fonts[TEXT] = face
    FONT = face
    screen.font = face
    return True


CACHE_UNDER = 40

_once = _cached(set())
ONCE_MAX = 512


def label(text_value, size, pen, face):
    """Bake a string into a sprite, or return None if it should be drawn where it stands."""
    if size >= CACHE_UNDER:
        return None
    key = (face, text_value, size, pen)
    cached = _labels.get(key)
    if cached is not None:
        return cached
    if key not in _once:
        if len(_once) > ONCE_MAX:
            _once.clear()
        _once.add(key)
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
        _labels.clear()
    _labels[key] = sprite
    return sprite


def blit_label(text_value, size, pen, x, y, align=0, name=TEXT):
    """Draw a string, aligned 0 left, 1 centre, 2 right about x, and return the width."""
    face = _fonts.get(name)
    if face is None:
        return 0
    sprite = label(text_value, size, pen, face)
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
    """Draw one symbol from the icon font."""
    return blit_label(character, size, pen, x, y, align, ICONS)


def clear_cache():
    """Forget everything held from an earlier draw."""
    for empty in _CLEARS:
        empty()


COLUMN_GAP = 8


def text_width(text_value, size, name=TEXT):
    """Return how wide a string will be drawn."""
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


# A capital stands 81 units of a 128 unit em; an icon fits a box of 100 on the baseline.
CAP_UNITS, ICON_UNITS, EM_UNITS = 81.0, 100.0, 128.0
CAP = CAP_UNITS / EM_UNITS
ICON_BOX = ICON_UNITS / EM_UNITS


def icon_baseline(text_y, text_size, icon_size):
    """Return the y to draw an icon at so it centres on the capitals of text at `text_y`."""
    cap_middle = text_y + text_size * (1.0 - CAP / 2.0)
    return int(cap_middle - icon_size * (1.0 - ICON_BOX / 2.0))


def column_width(texts, size, name=TEXT):
    """Return how wide a column of these strings has to be."""
    if not texts:
        return 0
    return text_width("\n".join(texts), size, name)


def fmt(value, field):
    """Format a number as a badge should show it: short, and never wider than its box."""
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


# Figures a slot shows before falling back to a count.
SEVERAL = 3


def _several(values, field):
    if not values:
        return "--"
    if len(values) <= SEVERAL:
        return " ".join(fmt(item, field) for item in values)
    return f"{len(values)} values"


def _rate(bps):
    """Format a throughput, scaled to the largest prefix it fills."""
    if bps >= 1024 * 1024 * 1024:
        return f"{bps / (1024.0 ** 3):.1f}G"
    if bps >= 1024 * 1024:
        return f"{bps / (1024.0 ** 2):.1f}M"
    if bps >= 1024:
        return f"{bps / 1024.0:.0f}K"
    return f"{bps:.0f}"


def _size(megabytes):
    """Format a size given in megabytes, scaled the way a rate is."""
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


# Units by ref, off the layout, for readings this module does not format itself.
UNITS = {}


def use_units(units):
    """Take the units the layout carried."""
    global UNITS
    UNITS = units or {}
    _readings.clear()


def short_unit(ref):
    """Return what follows the number. `fmt` puts a prefix on some, so theirs are its."""
    field = ref.rpartition(".")[2]
    if field.endswith("_bps"):
        return "B/s"
    if field == "cores" or field == "pct" or field.endswith("_pct"):
        return "%"
    if field.endswith("_mb"):
        return "B"
    if field in ("uptime_s", "secs_left"):
        return ""
    return UNITS.get(ref, "")


_readings = _cached({})


def reading(value, ref):
    """Return a value with its unit, for a slot with no room to place one separately."""
    field = ref.rpartition(".")[2]
    # Numbers only: a field can also arrive as a list of core loads.
    if type(value) is float or type(value) is int:
        key = (value, ref)
        text = _readings.get(key)
        if text is not None:
            return text
        text = fmt(value, field) + short_unit(ref)
        if len(_readings) > 240:
            _readings.clear()
        _readings[key] = text
        return text
    text = fmt(value, field)
    if value is None or isinstance(value, (str, bool, list, tuple)):
        # A load average is a queue length, not a percentage.
        return text
    return text + short_unit(ref)


def background(theme, title, index, total, subtitle=None):
    """Draw the header, the footer and a cleared body, each in its fixed place."""
    screen.pen = theme.bg
    screen.rectangle(rect(0, look.HEADER_H, look.W, look.BODY_H))
    furniture(theme, title, index, total, subtitle)


def furniture(theme, title, index, total, subtitle=None):
    """Draw the header and footer alone, leaving the body as it stands."""
    screen.pen = theme.panel
    screen.rectangle(rect(0, 0, look.W, look.HEADER_H))
    screen.rectangle(rect(0, look.H - look.FOOTER_H, look.W, look.FOOTER_H))
    screen.pen = theme.accent_b
    screen.rectangle(rect(0, look.HEADER_H - 2, look.W, 2))
    blit_label(title.upper(), look.SIZE_TITLE, theme.ink, look.PAD, 4)
    if subtitle:
        blit_label(subtitle, look.SIZE_SMALL, theme.dim, look.W - look.PAD, 10, align=2)
    if total > 1:
        row = _pips(theme, index, total)
        screen.blit(row, vec2((look.W - row.width) // 2,
                              look.H - look.FOOTER_H + look.FOOTER_H // 2 - 2))


# A dash shortens as the pips pack in, down to a dot at PIP_TIGHT.
PIP_ROOM = look.W - look.PAD * 4
PIP_MAX_W, PIP_GAP, PIP_DOT, PIP_TIGHT = 14, 5, 4, 2


def _pips(theme, index, total):
    """Bake the pip row, one pip per page and the current one in the accent colour."""
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


# "solid" fills the whole arc with the ramp colour for the reading. "ramp" lays the
# ramp round the arc, leaving the part past the reading at TRACK_ALPHA.
GAUGE_FILL = "solid"
TRACK_ALPHA = 32
_gradients = _cached({})


def swept_pens(theme, centre, radius, backwards=False):
    """Return the (fill, track) pens laying the theme's ramp round a gauge.

    `backwards` reverses the ramp, for the fields in pages.GOOD_HIGH.
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
    """Draw one sweep gauge, with a line of text inside it.

    `hot` is where the reading sits on the ramp, which can differ from where it sits on
    its scale; it colours the sweep, whose length is the reading either way. `swept` is a
    (fill, track) pair from `swept_pens`.
    """
    value_size = value_size or look.SIZE_HUGE
    label_size = label_size or look.SIZE_LABEL
    middle = vec2(*centre)
    start, end = look.DIAL_FROM, look.DIAL_TO
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    fill, track = swept if swept else (None, None)

    # Track and sweep abut rather than overlap; the join lands under the tick below.
    lit = not cold and fraction > 0.001
    sweep = start + (end - start) * fraction if lit else start
    screen.pen = theme.grid if track is None or cold else track
    if end - sweep > 0.5:
        screen.shape(shape.arc(middle, inner, outer, sweep, end))

    if lit:
        screen.pen = (theme.at(fraction if hot is None else hot) if fill is None else fill)
        screen.shape(shape.arc(middle, inner, outer, start, sweep))

        screen.pen = theme.ink
        screen.shape(shape.arc(middle, inner - 3, outer + 3, sweep - 1.4, sweep + 1.4))

    ink = theme.dim if cold else theme.ink
    top = centre[1] - value_size * 0.62
    unit_size = max(look.SIZE_SMALL, int(value_size * 0.45))
    reading_w = text_width(value_text, value_size)
    suffix_w = text_width(unit, unit_size) if unit else 0
    if suffix_w and reading_w + suffix_w > inner * 2 - 4:
        suffix_w = 0
    left = centre[0] - (reading_w + suffix_w) // 2
    blit_label(value_text, value_size, ink, left, top)
    if suffix_w:
        # Text puts its baseline `size` below where it is drawn, so the drop is the
        # difference in sizes.
        blit_label(unit, unit_size, theme.dim, left + reading_w,
                   top + value_size - unit_size)
    below = centre[1] + value_size * 0.42
    if icon and blit_icon(icon, label_size + 8, theme.dim, centre[0], below, align=1):
        return
    if under:
        blit_label(under, label_size, theme.dim, centre[0], below, align=1)


def dial(theme, fraction, value_text, unit_text, cold=False, hot=None, backwards=False):
    """Draw the single gauge of a `dial` page, with its readouts beside it."""
    gauge(theme, look.DIAL_C, look.DIAL_OUTER, look.DIAL_INNER, fraction, value_text,
          unit_text, cold=cold, hot=hot,
          swept=swept_pens(theme, look.DIAL_C, look.DIAL_OUTER, backwards)
          if GAUGE_FILL == "ramp" else None)


def dials(theme, entries):
    """Draw up to four gauges across the body band, each named under its reading."""
    shape_of = look.DIALS.get(len(entries)) or look.DIALS[4]
    for centre, entry in zip(shape_of["centres"], entries):
        name, value_text, fraction, icon, unit, hot = entry
        gauge(theme, centre, shape_of["outer"], shape_of["inner"], fraction, value_text,
              name, shape_of["value"], shape_of["label"], fraction is None, icon, unit,
              hot)


def readout(theme, y, name, value_text, fraction=None, note=None, chip=None, hot=None):
    """Draw one row of the column beside a gauge."""
    x = look.READOUT_X
    blit_label(name, look.SIZE_SMALL, theme.dim, x, y)
    blit_label(value_text, look.SIZE_VALUE, theme.ink, x, y + 10)
    if chip:
        screen.pen = chip
        screen.rectangle(rect(x + look.READOUT_W - 10, y + 3, 10, 10))
    if note:
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
    """Draw a stack of `(text, size, pen)` lines down the column beside a gauge."""
    y = (look.BODY_TOP + 12) if top is None else top
    x = look.READOUT_X + (look.READOUT_W if align == 2 else 0)
    for text_value, size, pen in entries:
        if not text_value:
            continue
        blit_label(text_value, size, pen, x, y, align=align)
        y += int(size * 1.35) + COLUMN_LEAD
    return y


def flat(values):
    """Return a series with its gaps dropped to the axis."""
    if None not in values:
        return values
    return [0.0 if value is None else value for value in values]


def at_axis(value):
    """Draw one reading, where None means it never came."""
    return 0.0 if value is None else value


def bars(theme, values, maximum=100.0, ref="cpu.cores", fractions=None, names=None):
    """Draw a stack of horizontal bars."""
    if not values:
        return
    values = flat(values)
    count = min(len(values), 16)
    top = look.BODY_TOP + 6
    slot = max(6, (look.BODY_H - 12) // count)
    height = max(4, slot - 3)
    names = ([str(names[i]) if i < len(names) else "" for i in range(count)] if names
             else [f"{i}" for i in range(count)])
    readings = [reading(values[i], ref) for i in range(count)]
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
        # From where the fill ends, so the two meet on a pixel boundary.
        screen.rectangle(rect(x + filled, y, width - filled, height))
        if filled:
            screen.pen = theme.at(fraction)
            screen.rectangle(rect(x, y, filled, height))
        blit_label(readings[i], look.SIZE_SMALL, theme.ink,
                   look.W - look.PAD, y - 1, align=2)


SMOOTH = True
# Points per span between two samples.
CURVE_STEPS = 2
# Below this height interpolation gives back the same picture, so a plot is drawn straight.
SMOOTH_MIN_H = 40
_weights = {}


def _basis(steps):
    """Return the Catmull-Rom weights for each fraction of a span."""
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
    """Return how finely to subdivide `count` samples across a plot, 1 for not at all."""
    if not SMOOTH or count < 3 or height < SMOOTH_MIN_H:
        return 1
    return max(2, min(CURVE_STEPS, int(width / (count - 1))))


def curve(values, steps=CURVE_STEPS):
    """Resample `values` to a Catmull-Rom curve through them, evenly spaced as they were."""
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

# Samples of room a moving plot keeps on its right. Fixed, or it resizes every frame.
WALK_LEAD = 2

# Below this a step spans most of the plot and one reading slides the picture off the side.
WALK_MIN = 8


def _lay_out(left, top, width, height, values, peak, shift):
    """Scale `values` against `peak` into the shared buffer, returning the floats written.

    `shift` is how far the plot has walked left since its last update, in samples, 0
    being just after one landed. `None` is a plot that never walks.
    """
    global _points
    values = flat(values)
    samples = len(values)
    if samples < 2:
        return 0
    count = samples
    steps = curve_steps(width, height, count)
    if steps > 1:
        count = (samples - 1) * steps + 1
    if len(_points) < (count + 2) * 2:
        _points = array("f", bytes((count + 2) * 8))
    per_sample = steps if steps > 1 else 1
    # The samples still to come are laid past the right edge and slide in, keeping the
    # box full.
    lead = per_sample * (WALK_LEAD if WALK_LEAD > 1 else 1)
    if lead > count // 4:
        lead = count // 4
    walking = shift is not None and samples >= WALK_MIN
    span = count - 1 - lead if walking and count > lead + 1 else count - 1
    step = width / float(span)
    scale = height / float(peak or 1.0)
    bottom = top + height
    # Past the headroom the plot is short of data, so `graph` draws that region as a gap.
    away = shift * step * per_sample if walking else 0.0
    start = left - away
    i = 0
    if steps > 1:
        low, high = min(values), max(values)
        table = _basis(steps)
        last = samples - 1
        point = 0
        for index in range(last):
            a = values[index - 1] if index else values[0]
            b = values[index]
            c = values[index + 1]
            d = values[index + 2] if index + 2 <= last else values[last]
            for w0, w1, w2, w3 in table:
                value = w0 * a + w1 * b + w2 * c + w3 * d
                value = low if value < low else (high if value > high else value)
                y = bottom - value * scale
                _points[i] = start + point * step
                _points[i + 1] = top if y < top else (bottom if y > bottom else y)
                i += 2
                point += 1
        y = bottom - values[last] * scale
        _points[i] = start + point * step
        _points[i + 1] = top if y < top else (bottom if y > bottom else y)
        return i + 2
    for index in range(count):
        y = bottom - values[index] * scale
        _points[i] = start + index * step
        _points[i + 1] = top if y < top else (bottom if y > bottom else y)
        i += 2
    return i


def area(left, top, width, height, values, peak, base=None, shift=None):
    """Return one filled area from `values` against `peak`, closed along its base."""
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


# Centred on the samples, or the band grows to one side.
LINE_W = 2.0
LINE_FLAGS = (shape.PATH_OPEN | shape.ALIGN_CENTER | shape.JOIN_MITER | shape.CAP_BUTT)


def line(left, top, width, height, values, peak, weight=LINE_W, shift=None):
    """Return `values` as a stroked polyline against `peak`."""
    i = _lay_out(left, top, width, height, values, peak, shift)
    if not i:
        return None
    trace = shape.custom(memoryview(_points)[:i])
    trace.stroke(weight, LINE_FLAGS)
    return trace


# An axis with no full scale tops out at one of these times a power of the reading's base.
AXIS_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500)


def axis_top(peak, field):
    """Return the round number an axis tops out at, at or above `peak`."""
    base = 1024.0 if field.endswith(("_bps", "_mb")) else 10.0
    scale = 1.0
    while scale * AXIS_STEPS[-1] < peak:
        scale *= base
    for step in AXIS_STEPS:
        if scale * step >= peak:
            return scale * step
    return scale * base


def graph(theme, series, labels, maximum=None, shift=None):
    """Draw one or two series over time, as filled areas."""
    ref = labels[0][1] if labels else "cpu.pct"
    field = ref.rpartition(".")[2]
    if maximum is None:
        # Flattened past the gaps: max() over the series compares None against a float.
        peak = axis_top(max((p for s in series for p in s if p is not None),
                            default=1.0), field)
    else:
        peak = max(maximum, 1.0) * 1.15

    peak_text = reading(peak, ref)
    left = look.PAD + column_width((peak_text, "0"), look.SIZE_SMALL) + 4
    top = look.BODY_TOP + 8
    width = look.W - left - look.PAD
    height = look.BODY_H - 26

    screen.pen = theme.grid
    for i in range(5):
        y = top + int(height * i / 4.0)
        screen.hspan(left, y, width)

    # Where the series ran out, drawn rather than papered over: a stalled host and an
    # idle machine are otherwise the same flat line.
    if shift is not None and shift > WALK_LEAD and series and len(series[0]) >= WALK_MIN:
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
        screen.clip = rect(left, look.BODY_TOP, width, look.BODY_H)
        screen.shape(filled)
        screen.clip = was
    screen.alpha = 255

    blit_label(peak_text, look.SIZE_SMALL, theme.dim, look.PAD, top - 4)
    blit_label("0", look.SIZE_SMALL, theme.dim, look.PAD, top + height - 8)
    for index, (name, _ref) in enumerate(labels[:2]):
        pen = _series_colour(theme, index)
        x = left + index * 110
        y = look.H - look.FOOTER_H - 14
        screen.pen = pen
        screen.rectangle(rect(x, y + 3, 10, 4))
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 14, y - 2)


def _series_alpha(theme, index):
    return theme.series_alpha[index]


def _series_colour(theme, index):
    return theme.series[index]


def grid(theme, entries):
    """Draw up to six labelled figures in two rows, one panel each."""
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
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 7, y + 5)
        if icon:
            blit_icon(icon, look.SIZE_VALUE, theme.dim, x + cell_w - 7, y + 4, align=2)
        size = look.SIZE_BIG if rows < 3 else look.SIZE_VALUE
        blit_label(value_text, size, theme.ink, x + 7, y + cell_h // 2 - size // 2 + 2)


VITALS_METERS = 5
VITALS_FACTS = 5
VITALS_NOTE_H = 12
VITALS_BAR_H = 4


def vitals(theme, meters, facts, notes=()):
    """Draw levels down the left, figures down the right, and a plate of strings under."""
    column = (look.W - look.PAD * 3) // 2
    right = look.PAD * 2 + column
    top = look.BODY_TOP + 4
    plate_h = len(notes) * VITALS_NOTE_H
    room = look.BODY_H - 8 - plate_h

    pitch = room // max(1, min(len(meters), VITALS_METERS))
    for index, (name, value_text, fraction, hot) in enumerate(meters[:VITALS_METERS]):
        y = top + index * pitch
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD, y)
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

    y = look.BODY_TOP + look.BODY_H - plate_h - 2
    for note in notes:
        blit_label(fit(note, look.SIZE_SMALL, look.W - look.PAD * 2), look.SIZE_SMALL,
                   theme.dim, look.PAD, y)
        y += VITALS_NOTE_H


def lines(theme, entries):
    """Draw labelled lines, for names and versions."""
    y = look.BODY_TOP + 10
    for name, value_text in entries[:7]:
        blit_label(name, look.SIZE_SMALL, theme.dim, look.PAD, y + 3)
        blit_label(value_text, look.SIZE_VALUE, theme.ink, look.W - look.PAD, y,
                   align=2)
        y += 24
        screen.pen = theme.grid
        screen.hspan(look.PAD, y - 5, look.W - look.PAD * 2)


def flow(text_value, size, pen, box, name=TEXT):
    """Draw text filled into `box`, wrapped, with an ellipsis where it does not fit."""
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


ITEM_TITLE = look.SIZE_SMALL
ITEM_TEXT = look.SIZE_VALUE
COUNT_H = 34


def notification(theme, items, counters):
    """Draw messages down the page, with a row of counters under them."""
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


PICTURE_GAP = 8
PICTURE_MIN = 24


def fitted(shown, height):
    """Return `shown` cropped to `height`, or None where there is not enough room."""
    if shown is None or height >= shown.height:
        return shown
    if height < PICTURE_MIN:
        return None
    return shown.window(rect(0, (shown.height - height) // 2, shown.width, height))


def shades_for(theme, entries):
    """Return the ramp to write into an indexed image's table of `entries`."""
    if entries in theme.image:
        return theme.image[entries]
    fits = [count for count in theme.image if count <= entries]
    return theme.image[max(fits)] if fits else None


def picture(theme, data):
    """Decode an indexed image off the wire into this theme's greys, or None."""
    if not data:
        return None
    held = _pictures.get(data)
    if held is not None:
        return held
    try:
        # Keyed on the encoded string, that being what arrives.
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
    """Draw one message: who it is from, how long ago, then the body."""
    room = look.W - look.PAD * 2
    y = top + 6
    left = look.PAD
    shown = fitted(picture(theme, (item or {}).get("image")), height - 8)
    if shown is not None:
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
            blit_label(fit(note, ITEM_TITLE, room - used - 6), ITEM_TITLE, theme.dim,
                       left + used + 6, y)
        y += int(ITEM_TITLE * 1.45)
    flow(str((item or {}).get("text") or ""), ITEM_TEXT, theme.ink,
         rect(left, y, look.W - look.PAD - left, top + height - y - 4))


def _counter_row(theme, counters, top, bottom):
    """Draw up to four labelled figures along the bottom, each in its share of the width."""
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
    """Return "3m ago" for a message, or None where there is no age to draw."""
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
    """Draw a full-screen notice, sized to its lines."""
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


# How far from what it sits on a pen must land, `difference` measuring black to white as 100.
READABLE_FLOOR = 20


def readable(pen, over, toward):
    """Return `pen` if it can be seen on `over`, else the same hue stepped toward `toward`."""
    for alpha in (255, 128):
        candidate = pen if alpha == 255 else pen.with_alpha(alpha).over(toward)
        if over.difference(candidate) >= READABLE_FLOOR:
            return candidate
    return toward


def fit(text, size, room):
    """Shorten a string until it fits `room` pixels, with an ellipsis if cut."""
    if screen.measure_text(text, font_size=size)[0] <= room:
        return text
    # Halved, not walked: width grows monotonically with length, so the longest prefix
    # that fits takes a handful of measurements.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if screen.measure_text(text[:middle] + "...", font_size=size)[0] <= room:
            low = middle
        else:
            high = middle - 1
    return (text[:low] + "...") if low else text


TOAST_FADE_MS = 400


def toast(theme, message, fade=1.0):
    """Draw a short-lived note over the footer, `fade` being 1 solid and 0 gone."""
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


RING_BAND = 14
RING_GAP = 4


def rings(theme, entries):
    """Draw concentric sweep gauges, outermost first, with a legend down the side."""
    rows = entries[:4]
    height = look.READOUT_NOTE_H if any(entry[4] for entry in rows) else look.READOUT_H
    for index, ((name, value_text, fraction, pen, note), y) in enumerate(
            zip(rows, look.readout_rows(len(rows), height))):
        ring_outer = look.DIAL_OUTER - index * (RING_BAND + RING_GAP)
        ring_inner = ring_outer - RING_BAND
        if ring_inner < 8:
            break
        sweep = look.DIAL_FROM + (look.DIAL_TO - look.DIAL_FROM) * at_axis(fraction)
        screen.pen = theme.grid
        if look.DIAL_TO - sweep > 0.5:
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   sweep, look.DIAL_TO))
        if fraction:
            screen.pen = pen
            screen.shape(shape.arc(vec2(*look.DIAL_C), ring_inner, ring_outer,
                                   look.DIAL_FROM, sweep))
        readout(theme, y, name, value_text, fraction, note, chip=pen if note else None)


ROWS = "zebra"
ROW_NONE = "none"


def sparklines(theme, entries):
    """Draw a row per reading: name, current value, and its history as a small line."""
    rows = entries[:6]
    if not rows:
        return
    height = min(30, (look.BODY_H - 8) // max(1, len(rows)))
    name_w = column_width([row[0] for row in rows], look.SIZE_LABEL)
    value_w = column_width([row[1] for row in rows], look.SIZE_LABEL)
    plot_x = look.PAD + name_w + COLUMN_GAP
    plot_w = max(40, look.W - plot_x - COLUMN_GAP - value_w - look.PAD)
    if ROWS == "zebra":
        screen.pen = theme.stripe
        for index in range(1, len(rows), 2):
            screen.rectangle(rect(0, look.BODY_TOP + 2 + index * height, look.W, height))
    elif ROWS == "rules":
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


def radar(theme, entries):
    """Draw a polygon over normalised axes."""
    import math

    rows = entries[:6]
    if len(rows) < 3:
        blit_label("radar needs three readings", look.SIZE_VALUE, theme.dim,
                   look.W // 2, look.BODY_MID, align=1)
        return
    centre = (look.W // 2, look.BODY_MID)
    # An ellipse: a circle wide enough for the 300px band spills the bottom label into
    # the page indicator.
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


def trend(theme, value_text, unit_text, name, delta, points, peak, fraction,
          hot=None, shift=None, field=""):
    """Draw one big reading, which way it is going, and where it has been."""
    blit_label(name, look.SIZE_LABEL, theme.dim, look.PAD + 2, look.BODY_TOP + 8)
    reading_w = blit_label(value_text, look.SIZE_HUGE, theme.ink, look.PAD,
                           look.BODY_TOP + 26)
    if unit_text:
        blit_label(unit_text, look.SIZE_BIG, theme.dim,
                   look.PAD + reading_w + 4, look.BODY_TOP + 48)

    if delta is not None:
        x = look.W - look.PAD
        blit_label(fmt(abs(delta), field), look.SIZE_VALUE, theme.ink, x,
                   look.BODY_TOP + 30, align=2)
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
    """Draw a triangle for the direction, flat where the reading is holding still."""
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


# One column a frame, shown as two windowed blits rather than copying the image onto itself.
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
    """Draw one column per call, scrolling left: a lane per value, coloured by the ramp."""
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
