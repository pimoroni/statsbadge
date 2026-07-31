"""Turning a page descriptor from the host into a drawn page.

A page is data: `{"kind": "dial", "field": "cpu.pct", "readouts": [...]}`. The kinds
here are the vocabulary, so rearranging a display is a config change the badge picks
up on its next poll with nothing installed. An extension that needs to draw something
these cannot ships a module of its own and registers it in `EXTRA`.
"""

import draw
import look

# Extension-supplied renderers, keyed by page kind. An extension's badge module does
# `pages.EXTRA["weather"] = render` at import.
EXTRA = {}

# Kinds that need a frame even when no new data arrived, because something on them
# moves on its own - a sweeping clock hand, an animation. Everything else is redrawn
# only when a poll lands, which is once a second.
ANIMATED = set()

# Nicer names than the raw field, where the raw field reads badly.
NAMES = {
    "cpu.pct": "LOAD", "cpu.temp": "TEMP", "cpu.freq": "CLOCK", "cpu.procs": "PROCS",
    "cpu.load": "LOADAVG",
    "mem.pct": "USED", "mem.used_mb": "USED", "mem.total_mb": "TOTAL",
    "mem.swap_pct": "SWAP", "mem.swap_used_mb": "SWAP",
    "gpu.pct": "LOAD", "gpu.temp": "TEMP", "gpu.power": "POWER",
    "gpu.mem_pct": "VRAM", "gpu.mem_used_mb": "VRAM", "gpu.clock": "CLOCK",
    "gpu.fan_pct": "FAN", "gpu.name": "GPU",
    "net.up_bps": "UP", "net.down_bps": "DOWN", "net.iface": "IFACE",
    "net.up_total_mb": "SENT", "net.down_total_mb": "RECV",
    "disk.pct": "FULL", "disk.read_bps": "READ", "disk.write_bps": "WRITE",
    "disk.used_mb": "USED", "disk.total_mb": "TOTAL",
    "power.battery_pct": "BATTERY", "power.charging": "CHARGING",
    "power.package_w": "PACKAGE", "power.secs_left": "LEFT",
    "sys.host": "HOST", "sys.os": "OS", "sys.cpu_name": "CPU",
    "sys.uptime_s": "UPTIME", "sys.arch": "ARCH",
}

# Full-scale for a field that is not a percentage, so a bar knows where the end is.
SCALE = {
    "temp": 100.0, "power": 250.0, "package_w": 150.0, "rpm": 6000.0,
    "freq": 6000.0, "clock": 3000.0,
    "up_bps": 12.5e6, "down_bps": 12.5e6, "read_bps": 500e6, "write_bps": 500e6,
}

PERCENT = ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct")


def name_for(ref):
    if ref in NAMES:
        return NAMES[ref]
    return ref.split(".")[-1].replace("_", " ").upper()


def value_of(frame, ref):
    """Look up "group.field", taking the first entry of a list group."""
    if not ref or "." not in ref:
        return None
    group, field = ref.split(".", 1)
    holder = frame.get(group)
    if isinstance(holder, list):
        holder = holder[0] if holder else None
    if not isinstance(holder, dict):
        return None
    return holder.get(field)


def fraction_of(ref, value, page=None):
    """Where a value sits on 0-1, for a gauge."""
    if value is None or isinstance(value, (str, bool)):
        return None
    field = ref.split(".")[-1]
    if page and page.get("max"):
        top = float(page["max"])
    elif field in PERCENT:
        top = 100.0
    else:
        top = SCALE.get(field)
        if top is None:
            return None
    try:
        return max(0.0, min(1.0, float(value) / top))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def render(page, frame, history, theme, index, total, subtitle=None):
    """Draw one page. Everything before this has already cleared the screen."""
    draw.background(theme, page.get("title", page.get("id", "")), index, total,
                    subtitle)
    kind = page.get("kind")
    handler = EXTRA.get(kind) or _KINDS.get(kind)
    if handler is None:
        draw.blit_label(f"no renderer for {kind!r}", look.SIZE_VALUE, theme.dim,
                        look.W // 2, look.BODY_MID, align=1)
        return
    handler(page, frame, history, theme)


# -- the kinds --------------------------------------------------------------

def _dial(page, frame, _history, theme):
    ref = page.get("field", "")
    value = value_of(frame, ref)
    fraction = fraction_of(ref, value, page)
    field = ref.split(".")[-1]
    draw.dial(theme, fraction, draw.fmt(value, field), draw.short_unit(field),
              cold=value is None)
    for i, readout_ref in enumerate(page.get("readouts", [])[:3]):
        readout_value = value_of(frame, readout_ref)
        readout_field = readout_ref.split(".")[-1]
        draw.readout(theme, i, name_for(readout_ref),
                     draw.fmt(readout_value, readout_field),
                     fraction_of(readout_ref, readout_value))


def _bars(page, frame, _history, theme):
    ref = page.get("field", "")
    values = value_of(frame, ref)
    if not isinstance(values, list):
        values = [] if values is None else [values]
    maximum = float(page.get("max") or 100.0)
    draw.bars(theme, values, maximum)


def _graph(page, frame, history, theme):
    refs = page.get("fields", [])[:2]
    series = [history.get(ref) or [] for ref in refs]
    # If the host has no ring for this yet, at least plot the live value.
    for i, ref in enumerate(refs):
        if not series[i]:
            value = value_of(frame, ref)
            if value is not None:
                series[i] = [value, value]
    labels = [(name_for(ref), ref.split(".")[-1]) for ref in refs]
    field = refs[0].split(".")[-1] if refs else "pct"
    maximum = float(page["max"]) if page.get("max") else (
        100.0 if field in PERCENT else None)
    draw.graph(theme, series, labels, maximum)


def _grid(page, frame, _history, theme):
    refs = page.get("fields", [])[:6]
    groups = [ref.split(".")[0] for ref in refs]
    by_group = len(set(groups)) == len(groups)
    entries = []
    for ref in refs:
        value = value_of(frame, ref)
        field = ref.split(".")[-1]
        entries.append((name_for(ref), draw.fmt(value, field),
                        fraction_of(ref, value), icon_for(ref, by_group)))
    draw.grid(theme, entries)


# One symbol per group and per field, as characters in icons.af. Built from
# ci/badge-icons.txt, which is the only other place these letters appear. A reading with
# no symbol falls back to its name, so neither map has to be complete.
GROUP_ICONS = {"cpu": "c", "gpu": "g", "mem": "m", "disk": "d", "net": "n",
               "power": "p", "fans": "f", "sys": "y"}
FIELD_ICONS = {
    "pct": "l", "temp": "t", "freq": "s", "clock": "s", "procs": "r",
    "up_bps": "u", "down_bps": "o", "write_bps": "u", "read_bps": "o",
    "battery_pct": "b", "package_w": "p", "power": "p", "rpm": "f",
    "mem_pct": "m", "swap_pct": "e", "fan_pct": "f",
    "used_mb": "a", "total_mb": "a", "mem_used_mb": "a", "swap_used_mb": "a",
    "uptime_s": "h", "secs_left": "h",
}


def icon_for(ref, by_group):
    """The symbol for a reading, or None. `by_group` picks which half of the name it is
    standing in for, the same way the label does."""
    group, _, field = ref.partition(".")
    return GROUP_ICONS.get(group) if by_group else FIELD_ICONS.get(field)


def _dials(page, frame, _history, theme):
    refs = page.get("fields", [])[:4]
    groups = [ref.split(".")[0] for ref in refs]
    # Name each gauge by whatever tells it apart from the others. A page of one reading
    # per subsystem wants CPU and GPU, where NAMES would call both of them LOAD; a page
    # of several readings from one subsystem wants LOAD and TEMP.
    by_group = len(set(groups)) == len(groups)
    entries = []
    for ref, group in zip(refs, groups):
        value = value_of(frame, ref)
        field = ref.split(".")[-1]
        entries.append((group.upper() if by_group else name_for(ref),
                        draw.fmt(value, field),
                        fraction_of(ref, value, page),
                        icon_for(ref, by_group),
                        draw.short_unit(field)))
    draw.dials(theme, entries)


def _text(page, frame, _history, theme):
    entries = []
    for ref in page.get("fields", [])[:7]:
        value = value_of(frame, ref)
        entries.append((name_for(ref), draw.fmt(value, ref.split(".")[-1])))
    draw.lines(theme, entries)


_KINDS = {
    "dial": _dial,
    "dials": _dials,
    "bars": _bars,
    "graph": _graph,
    "grid": _grid,
    "text": _text,
}
