"""Drawing the pages.

Vector shapes throughout, so a theme is a colour table. The costs that shape this
(measured on this board, in BADGEWARE.md): an anti-aliased shape is about a quarter
of a millisecond whatever its size, a line of live text is about 1ms, and the same
text blitted from a cache is 0.08ms. So shapes are drawn live and every string that
is not new every frame is baked into a sprite once.

The header and footer change only when the page or the theme does, so they are baked
into two band images per page and blitted over a raster fill of the body.
"""

import look

FONT = None
_labels = {}
_bands = {}


def prepare():
    """Load the font. 107ms, so once, and before the first frame."""
    global FONT
    if FONT is None:
        FONT = font.load(look.FONT_PATH)
    screen.font = FONT


# -- text cache -------------------------------------------------------------

def label(text_value, size, rgb):
    """A string baked into a sprite. Live text is ~1ms a line, a blit is 0.08ms."""
    key = (text_value, size, rgb)
    cached = _labels.get(key)
    if cached is not None:
        return cached
    width, height = screen.measure_text(text_value, font_size=size)
    width = max(1, int(width + 2))
    height = max(1, int(size * 1.35))
    sprite = image(width, height)
    sprite.font = FONT
    sprite.pen = brush.erase()
    sprite.rectangle(rect(0, 0, width, height))
    sprite.antialias = image.X4
    sprite.pen = color.rgb(*rgb)
    sprite.text(text_value, vec2(0, 0), size)
    if len(_labels) > 220:
        # Values churn; the cache is for furniture, so drop it wholesale rather than
        # tracking ages.
        _labels.clear()
    _labels[key] = sprite
    return sprite


def blit_label(text_value, size, rgb, x, y, align=0):
    """Draw a cached string. align 0 left, 1 centre, 2 right, about x."""
    sprite = label(text_value, size, rgb)
    if align == 1:
        x -= sprite.width // 2
    elif align == 2:
        x -= sprite.width
    screen.blit(sprite, vec2(int(x), int(y)))
    return sprite.width


def clear_cache():
    _labels.clear()
    _bands.clear()


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
        return f"{value / 1024.0:.1f}G" if value >= 1024 else f"{value:.0f}M"
    if field in ("uptime_s", "secs_left"):
        return _duration(value)
    if field in ("freq", "clock", "rpm", "procs"):
        return f"{value:.0f}"
    if isinstance(value, float):
        return f"{value:.0f}" if value >= 100 else f"{value:.1f}"
    return str(value)


def _rate(bps):
    if bps >= 1024 * 1024 * 1024:
        return f"{bps / (1024.0 ** 3):.1f}G"
    if bps >= 1024 * 1024:
        return f"{bps / (1024.0 ** 2):.1f}M"
    if bps >= 1024:
        return f"{bps / 1024.0:.0f}K"
    return f"{bps}B"


def _duration(seconds):
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 60}m"


def short_unit(field):
    if field.endswith("_bps"):
        return "/s"
    if field in ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct"):
        return "%"
    if field == "temp":
        return "C"
    if field in ("power", "package_w"):
        return "W"
    if field in ("freq", "clock"):
        return "MHz"
    if field.endswith("_mb"):
        return ""
    return ""


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
        # One pip per page, the current one in the accent colour.
        pip_w, gap = 14, 5
        span = total * pip_w + (total - 1) * gap
        x = (look.W - span) // 2
        y = look.FOOTER_H // 2 - 2
        for i in range(total):
            footer.pen = color.rgb(*(theme.accent if i == index else theme.grid))
            footer.shape(shape.rounded_rectangle(
                rect(x + i * (pip_w + gap), y, pip_w, 4), 2))
    return (header, footer)


# -- widgets ----------------------------------------------------------------

def dial(theme, fraction, value_text, unit_text, cold=False):
    """A sweep gauge.

    `shape.arc(centre, inner, outer, from, to)` - angles start at the top and run
    clockwise, so look.DIAL_FROM..DIAL_TO is 225..495 and the gap lands at the bottom.
    """
    centre = vec2(*look.DIAL_C)
    start, end = look.DIAL_FROM, look.DIAL_TO
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, fraction))

    screen.pen = color.rgb(*theme.grid)
    screen.shape(shape.arc(centre, look.DIAL_INNER, look.DIAL_OUTER, start, end))

    if not cold and fraction > 0.001:
        sweep = start + (end - start) * fraction
        # Solid, in the ramp's colour for this value: a spatial gradient across the
        # arc's box does not follow the curve, so the hue would not track the reading.
        # This way the colour *is* the severity, and it costs one shape.
        hot = theme.at(fraction)
        screen.pen = color.rgb(*hot)
        screen.shape(shape.arc(centre, look.DIAL_INNER, look.DIAL_OUTER, start, sweep))

        # A brighter tick at the sweep's end, so the exact value is readable.
        screen.pen = color.rgb(*theme.ink)
        screen.shape(shape.arc(centre, look.DIAL_INNER - 3, look.DIAL_OUTER + 3,
                               sweep - 1.4, sweep + 1.4))

    ink = theme.dim if cold else theme.ink
    blit_label(value_text, look.SIZE_HUGE, ink,
               look.DIAL_C[0], look.DIAL_C[1] - look.SIZE_HUGE * 0.62, align=1)
    if unit_text:
        blit_label(unit_text, look.SIZE_LABEL, theme.dim,
                   look.DIAL_C[0], look.DIAL_C[1] + look.SIZE_HUGE * 0.42, align=1)


def readout(theme, index, name, value_text, fraction=None):
    """One of the small figures beside a dial, with a thin bar under it."""
    x = look.READOUT_X
    y = look.BODY_TOP + 6 + index * look.READOUT_H
    blit_label(name, look.SIZE_SMALL, theme.dim, x, y)
    blit_label(value_text, look.SIZE_VALUE, theme.ink, x, y + 10)
    if fraction is not None:
        width = look.READOUT_W
        fraction = max(0.0, min(1.0, fraction))
        screen.pen = color.rgb(*theme.grid)
        screen.rectangle(rect(x, y + 28, width, 3))
        if fraction > 0:
            screen.pen = color.rgb(*theme.at(fraction))
            screen.rectangle(rect(x, y + 28, int(width * fraction), 3))


def bars(theme, values, maximum=100.0):
    """A stack of horizontal bars. Raster rectangles: no AA needed on an axis-aligned
    bar, and this is the one page that can have 32 of them."""
    if not values:
        return
    count = min(len(values), 16)
    top = look.BODY_TOP + 6
        # Fit the band whatever the core count, with at least a pixel between bars.
    slot = max(6, (look.BODY_H - 12) // count)
    height = max(4, slot - 3)
    label_w = 26
    x = look.PAD + label_w
    width = look.W - x - look.PAD - 34

    for i in range(count):
        value = values[i] or 0.0
        fraction = max(0.0, min(1.0, value / maximum if maximum else 0.0))
        y = top + i * slot
        blit_label(f"{i}", look.SIZE_SMALL, theme.dim, look.PAD, y - 1)
        screen.pen = color.rgb(*theme.grid)
        screen.rectangle(rect(x, y, width, height))
        if fraction > 0:
            screen.pen = color.rgb(*theme.at(fraction))
            screen.rectangle(rect(x, y, max(1, int(width * fraction)), height))
        blit_label(f"{value:.0f}", look.SIZE_SMALL, theme.ink,
                   look.W - look.PAD, y - 1, align=2)


def graph(theme, series, labels, maximum=None):
    """One or two series over time, as filled areas.

    Each series is one `shape.custom` contour: a polyline across the top and back
    along the bottom. One shape is one anti-aliased edge and one setup cost, where a
    line per sample would be dozens.
    """
    left = look.PAD + 30
    top = look.BODY_TOP + 8
    width = look.W - left - look.PAD
    height = look.BODY_H - 26

    peak = maximum
    if peak is None:
        peak = max((max(s) for s in series if s), default=1.0)
    peak = max(peak, 1.0) * 1.15

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
    blit_label(fmt(peak, labels[0][1] if labels else "pct"), look.SIZE_SMALL,
               theme.dim, look.PAD, top - 4)
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
        name, value_text, fraction = entries[i]
        column = i % columns
        row = i // columns
        x = look.PAD + column * (cell_w + 6)
        y = look.BODY_TOP + 6 + row * (cell_h + 6)
        screen.pen = color.rgb(*theme.panel)
        screen.shape(shape.rounded_rectangle(rect(x, y, cell_w, cell_h), 5))
        if fraction is not None:
            screen.pen = color.rgb(*theme.at(max(0.0, min(1.0, fraction))))
            screen.rectangle(rect(x, y + cell_h - 3, int(cell_w * max(0.0, min(1.0, fraction))), 3))
        blit_label(name, look.SIZE_SMALL, theme.dim, x + 7, y + 5)
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
