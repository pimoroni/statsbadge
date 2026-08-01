"""Turning a page descriptor from the host into a drawn page.

A page is data: `{"kind": "dial", "field": "cpu.pct", "readouts": [...]}`. The kinds
here are the vocabulary, so rearranging a display is a config change the badge picks
up on its next poll with nothing installed. An extension that needs to draw something
these cannot ships a module of its own and registers it in `EXTRA`.
"""

import time

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

# cores is a list of percentages, which is not obvious from the name: without it a
# per-core page drew bare numbers and scaled its graph from the data.
PERCENT = ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct", "cores")


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


def fraction_of(ref, value, page=None, frame=None):
    """Where a value sits on 0-1, for a gauge.

    A rate is scaled by the busiest the host has seen it, which it sends with the frame:
    throughput has no full scale of its own, and the fixed one this used to use reads as
    pegged on a fast link and as idle on a slow one.
    """
    if value is None or isinstance(value, (str, bool)):
        return None
    field = ref.split(".")[-1]
    if page and page.get("max"):
        top = float(page["max"])
    elif field in PERCENT:
        top = 100.0
    else:
        top = peak_of(ref, frame) if field.endswith("_bps") else None
        top = top or SCALE.get(field)
        if top is None:
            return None
    try:
        return max(0.0, min(1.0, float(value) / top))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def peak_of(ref, frame):
    """What the host has seen this rate reach, or None before it has sent one."""
    if not frame:
        return None
    peak = (frame.get("peaks") or {}).get(ref)
    return float(peak) if peak else None


def scale_note(ref, frame):
    """"peak 11.4M/s", for a gauge whose full scale is that and not a round number."""
    peak = peak_of(ref, frame)
    if peak is None or not ref.split(".")[-1].endswith("_bps"):
        return None
    return "peak " + draw.reading(peak, ref.split(".")[-1])


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
    fraction = fraction_of(ref, value, page, frame)
    field = ref.split(".")[-1]
    # The unit slot says what full means where that is not obvious, which for a rate it
    # never is.
    under = scale_note(ref, frame) or draw.short_unit(field)
    draw.dial(theme, fraction, draw.fmt(value, field), under, cold=value is None)
    readouts = page.get("readouts", [])[:3]
    for i, readout_ref in enumerate(readouts):
        readout_value = value_of(frame, readout_ref)
        readout_field = readout_ref.split(".")[-1]
        draw.readout(theme, i, name_for(readout_ref),
                     draw.reading(readout_value, readout_field),
                     fraction_of(readout_ref, readout_value, None, frame),
                     count=len(readouts))


def _bars(page, frame, _history, theme):
    ref = page.get("field", "")
    values = value_of(frame, ref)
    if not isinstance(values, list):
        values = [] if values is None else [values]
    maximum = float(page.get("max") or 100.0)
    draw.bars(theme, values, maximum, ref.split(".")[-1])


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
        entries.append((name_for(ref), draw.reading(value, field),
                        fraction_of(ref, value, None, frame), icon_for(ref, by_group)))
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
                        fraction_of(ref, value, page, frame),
                        icon_for(ref, by_group),
                        draw.short_unit(field)))
    draw.dials(theme, entries)


def _text(page, frame, _history, theme):
    entries = []
    for ref in page.get("fields", [])[:7]:
        value = value_of(frame, ref)
        entries.append((name_for(ref), draw.fmt(value, ref.split(".")[-1])))
    draw.lines(theme, entries)


def names_for(refs):
    """Display names that tell these readings apart.

    The field name where that is already unique - LOAD, TEMP - the group where it is
    not, and both where neither is: a page of cpu.pct and gpu.pct would otherwise be
    two rows both called LOAD.
    """
    plain = [name_for(ref) for ref in refs]
    if len(set(plain)) == len(plain):
        return plain
    groups = [ref.split(".")[0].upper() for ref in refs]
    if len(set(groups)) == len(groups):
        return groups
    return [f"{group} {name}" for group, name in zip(groups, plain)]


def _series_for(ref, frame, history, page=None):
    """A field's history, falling back to the live value so a cold ring still plots."""
    points = list(history.get(ref) or ())
    if not points:
        value = value_of(frame, ref)
        if value is not None:
            points = [value, value]
    peak = None
    if page and page.get("max"):
        peak = float(page["max"])
    elif ref.split(".")[-1] in PERCENT:
        peak = 100.0
    if peak is None:
        peak = max((p for p in points if p is not None), default=1.0)
    return points, max(float(peak), 1.0)


def _rings(page, frame, _history, theme):
    refs = page.get("fields", [])[:4]
    entries = []
    labels = names_for(refs)
    for index, ref in enumerate(refs):
        value = value_of(frame, ref)
        field = ref.split(".")[-1]
        fraction = fraction_of(ref, value, page, frame)
        # Coloured by its own reading, the way every gauge here is: by position in the
        # stack the outermost ring would always look calm and the innermost alarming.
        rgb = theme.at(fraction) if fraction is not None else theme.grid
        entries.append((labels[index], draw.reading(value, field), fraction, rgb,
                        scale_note(ref, frame)))
    draw.rings(theme, entries)


def _spark(page, frame, history, theme):
    refs = page.get("fields", [])[:6]
    labels = names_for(refs)
    entries = []
    for index, ref in enumerate(refs):
        points, peak = _series_for(ref, frame, history, page)
        value = value_of(frame, ref)
        entries.append((labels[index], draw.reading(value, ref.split(".")[-1]),
                        points, peak))
    draw.sparklines(theme, entries)


def _radar(page, frame, _history, theme):
    refs = page.get("fields", [])[:6]
    labels = names_for(refs)
    entries = []
    for index, ref in enumerate(refs):
        value = value_of(frame, ref)
        entries.append((labels[index], draw.reading(value, ref.split(".")[-1]),
                        fraction_of(ref, value, page, frame), theme.accent))
    draw.radar(theme, entries)


def _trend(page, frame, history, theme):
    ref = page.get("field", "")
    field = ref.split(".")[-1]
    value = value_of(frame, ref)
    points, peak = _series_for(ref, frame, history, page)
    # Against a few samples back rather than the last one, which is mostly noise.
    delta = None
    if value is not None and len(points) > 4:
        was = points[-5]
        if was is not None:
            delta = float(value) - float(was)
    draw.trend(theme, draw.fmt(value, field), draw.short_unit(field), name_for(ref),
               delta, points, peak, fraction_of(ref, value, page, frame))


# How far between polls the waterfall has got, so it can interpolate rather than step.
# Held here because only this side knows when a poll landed.
_wf_from = ()
_wf_to = ()
_wf_seq = None
_wf_at = 0
# A poll is a second apart; the ease is over slightly less so it settles before the next.
WF_EASE_MS = 850


def _waterfall(page, frame, history, theme):
    global _wf_from, _wf_to, _wf_seq, _wf_at
    ref = page.get("field", "cpu.cores")
    values = value_of_list(frame, ref)
    maximum = float(page.get("max") or 100.0)

    if values and frame.get("seq") != _wf_seq:
        if not _wf_to:
            # First sight of this page: seed from the host's ring so it does not start
            # blank, oldest first.
            for past in (history.get(ref) or ())[-24:]:
                if isinstance(past, list) and past:
                    draw.waterfall(theme, [v / maximum for v in past])
            _wf_from = list(values)
        else:
            _wf_from = list(_wf_to)
        _wf_to = list(values)
        _wf_seq = frame.get("seq")
        _wf_at = time.ticks_ms()

    if not _wf_to:
        draw.waterfall(theme, [])
        return

    phase = 1.0
    if _wf_at:
        phase = min(1.0, max(0.0, time.ticks_diff(time.ticks_ms(), _wf_at) / WF_EASE_MS))
    lanes = []
    for index, target in enumerate(_wf_to):
        start = _wf_from[index] if index < len(_wf_from) else target
        # Smoothstep, so a lane leaves and arrives gently instead of ramping linearly.
        eased = phase * phase * (3.0 - 2.0 * phase)
        lanes.append((start + (target - start) * eased) / maximum)
    labels = [str(i) for i in range(len(lanes))] if len(lanes) <= 16 else None
    draw.waterfall(theme, lanes, labels)


def value_of_list(frame, ref):
    """A field that is expected to be a list, as one."""
    group, _, field = ref.partition(".")
    values = (frame.get(group) or {})
    values = values.get(field) if isinstance(values, dict) else None
    return values if isinstance(values, list) else []


_KINDS = {
    "dial": _dial,
    "dials": _dials,
    "bars": _bars,
    "graph": _graph,
    "grid": _grid,
    "text": _text,
    "rings": _rings,
    "spark": _spark,
    "radar": _radar,
    "trend": _trend,
    "waterfall": _waterfall,
}

# It interpolates between polls, so it needs a frame whether or not one landed.
ANIMATED.add("waterfall")
