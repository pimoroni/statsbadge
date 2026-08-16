"""Turning a page descriptor from the host into a drawn page.

A page is data: `{"kind": "dial", "field": "cpu.pct", "readouts": [...]}`. The kinds
here are the vocabulary, so rearranging a display is a config change the badge picks
up on its next poll with nothing installed. An extension that needs to draw something
these cannot ships a module and registers it in `EXTRA`.
"""

import time

import draw
import look

# Extension-supplied renderers, keyed by page kind. An extension's badge module does
# `pages.EXTRA["weather"] = render` at import.
EXTRA = {}

# Kinds with something that moves unprompted, so they get a frame with no new data.
ANIMATED = set()


# Whether a gauge sweeps to each new reading or steps to it, from the layout. The number
# beside it steps either way, or a sprite is baked every frame.
ANIMATE = False
# Whether a plot moves between readings. Separate from SWEEP, and off by default.
PLOT_ANIMATION = False
# How far back in the series "now" is, in samples: the host's age plus the time since,
# never this badge's poll rate.
BEHIND = 0.0
# The host's spacing, how many points one of our polls covers, and how far behind a plot
# draws before it gives up and shows the gap.
EVERY_MS = 1000
LEAD = 1
BEHIND_MAX = 12.0


def note_spacing(every_ms, interval_ms):
    """How far apart the host's points are, and how many of them a poll of ours covers.

    A badge polling slower than the host samples is handed several at a time, and a plot
    keeps `interval / every` of them in reserve on its right. Both are known, not measured:
    working it out from observed gaps walked a plot at the wrong pace.
    """
    global EVERY_MS, LEAD
    EVERY_MS = int(every_ms) or 1000
    covered = -(-int(interval_ms) // EVERY_MS)      # rounded up
    LEAD = 1 if covered < 1 else (12 if covered > 12 else covered)


# Rings on their own clock, as {"group.field": every_ms}, from the history reply. A source
# answering by the hour cannot be walked at the collector's spacing.
SPACING = {}


def note_series_spacing(spacing):
    """Which rings are on a separate clock, from the history reply's `spacing`."""
    SPACING.clear()
    for key, entry in (spacing or {}).items():
        every = int((entry or {}).get("every_ms") or 0)
        if every:
            SPACING[key] = every


# The kinds that draw a series, and so have one fetched for them.
PLOTS = ("graph", "spark", "trend")
# The ones that move between readings. A spark holds still; see draw.sparklines.
SCROLLS = ("graph", "trend")
# How long a sweep takes, as a fraction of the second between readings.
SWEEP_MS = 350
_sweeps = {}
# Whether the frame just drawn had a sweep in it, so another is owed.
moving = False


def sweep_reset():
    """Forget where each gauge stood, so the next reading is drawn where it is.

    Sweeping from the last page's reading would draw a change the machine never made.
    """
    _sweeps.clear()


def _swept(ref, fraction):
    """`fraction`, eased from wherever this gauge already stood.

    Keyed on the field, plus a position where a page draws a row of them.
    """
    global moving
    if not ANIMATE or fraction is None:
        return fraction
    sweep = _sweeps.get(ref)
    # Eased from where the needle *is*, not from the reading it was heading for, or a
    # second reading mid-sweep would jump the needle forward before carrying on.
    if sweep is None:
        sweep = _sweeps[ref] = tween(fraction, fraction, SWEEP_MS, tween.CUBIC_OUT).start()
    elif abs(sweep.to - fraction) > 0.001:
        sweep = _sweeps[ref] = tween(sweep.now, fraction, SWEEP_MS,
                                     tween.CUBIC_OUT).start()
    # Equal endpoints is a seeded gauge, first reading or after a page turn: nowhere to go.
    if not sweep.done and abs(sweep.to - sweep.from_) > 0.001:
        moving = True
    return sweep.now

# The host's names for an extension's groups, {"cf_pinout_xyz": "pinout.xyz"}. Extension
# groups only: cpu, mem and the rest are named in NAMES below.
LABELS = {}

# Worked out from a ref and then held. Both depend on the ref alone, so only a layout that
# retired a ref makes them worth dropping; names_for is not here because it reads LABELS.
_fields = {}
_names = {}


def field_of(ref):
    """The part after the last dot: what a scale, a unit and a severity are keyed on."""
    field = _fields.get(ref)
    if field is None:
        field = _fields[ref] = ref.split(".")[-1]
    return field


def forget_layout():
    _fields.clear()
    _names.clear()

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

# Full-scale for a field that is not a percentage, which is where a bar ends.
SCALE = {
    "temp": 100.0, "power": 250.0, "package_w": 150.0, "rpm": 6000.0,
    "freq": 6000.0, "clock": 3000.0,
    "up_bps": 12.5e6, "down_bps": 12.5e6, "read_bps": 500e6, "write_bps": 500e6,
    "volts": 1.6,       # a core rail, which sits near 1.1
}

# cores is a list of percentages, which is not obvious from the name: without it a
# per-core page drew bare numbers and scaled its graph from the data.
PERCENT = ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct", "cores")


def is_percent(field):
    """Whether a reading is already 0-100, so nothing has to say where full is.

    By suffix as well as by name: the model's percentages carry `_pct`, and a source adding
    a group is asked to do the same, there being nothing else to read it off.
    """
    return field in PERCENT or field.endswith("_pct")

# Fields where a high reading is the good one, and the ramp is walked backwards. It runs
# calm to alarming, which suits a load or a temperature but inverts a battery.
GOOD_HIGH = ("battery_pct",)


def severity_of(ref, fraction):
    """Where a reading sits on the ramp, which can differ from where it sits on its scale.

    Only the colour. A gauge's sweep and a bar's length are the reading itself.
    """
    if fraction is None:
        return None
    return 1.0 - fraction if field_of(ref) in GOOD_HIGH else fraction


# A trailing unit is stripped for the label, or "BYTES BPS 22KB/s" states it twice. The
# built-ins are all in NAMES, so this is for an extension's fields.
UNIT_SUFFIXES = ("_bps", "_mb", "_pct")

# `cached_pct` is labelled by `cached_pct_names`, where a source sends them.
LANE_NAMES = "_names"


def name_for(ref):
    if ref in NAMES:
        return NAMES[ref]
    held = _names.get(ref)
    if held is not None:
        return held
    field = field_of(ref)
    for suffix in UNIT_SUFFIXES:
        if field.endswith(suffix) and len(field) > len(suffix):
            field = field[:-len(suffix)]
            break
    name = _names[ref] = field.replace("_", " ").upper()
    return name


def merge_slow(frame, held):
    """Put the slow half of a frame back into it.

    A group the host fetches once a minute is sent only when it changes, so every frame
    after arrives without it. `held` is what came last.
    `peaks` is merged rather than replaced: a peak scales the reading it belongs to, so the
    slow ones travel with the slow readings and the rest arrive every frame.
    """
    for key, value in held.items():
        if key == "peaks":
            if frame.get(key) is None:
                frame[key] = dict(value)
            else:
                frame[key].update(value)
        else:
            frame[key] = value
    return frame


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

    A rate is scaled by the busiest the host has seen, which travels with the frame. A fixed
    full scale reads as pegged on a fast link and idle on a slow one.
    """
    if value is None or isinstance(value, (str, bool)):
        return None
    field = field_of(ref)
    if page and page.get("max"):
        top = float(page["max"])
    elif is_percent(field):
        top = 100.0
    else:
        # The host's peak where it sent one: it tracks throughput, and beats a guess here.
        top = peak_of(ref, frame) or SCALE.get(field)
        if top is None:
            return None
    try:
        return _swept(ref, max(0.0, min(1.0, float(value) / top)))
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
    if peak is None:
        return None
    return "peak " + draw.reading(peak, field_of(ref))


def render(page, frame, history, theme, index, total, subtitle=None):
    """Draw one page: the chrome, then the handler for its kind."""
    global moving
    moving = False
    # How much room a moving plot keeps on its right for the samples still coming in.
    draw.WALK_LEAD = LEAD
    draw.background(theme, page.get("title", page.get("id", "")), index, total,
                    subtitle)
    kind = page.get("kind")
    handler = EXTRA.get(kind) or _KINDS.get(kind)
    if handler is None:
        draw.blit_label(f"no renderer for {kind!r}", look.SIZE_VALUE, theme.dim,
                        look.W // 2, look.BODY_MID, align=1)
        return
    handler(page, frame, history, theme)


def _dial(page, frame, _history, theme):
    ref = page.get("field", "")
    value = value_of(frame, ref)
    fraction = fraction_of(ref, value, page, frame)
    field = field_of(ref)
    # The unit slot carries what full scale means, which for a rate is never obvious.
    under = scale_note(ref, frame) or draw.short_unit(field)
    draw.dial(theme, fraction, draw.fmt(value, field), under, cold=value is None,
              hot=severity_of(ref, fraction), backwards=field in GOOD_HIGH)
    readouts = page.get("readouts", [])[:3]
    for readout_ref, y in zip(readouts, look.readout_rows(len(readouts))):
        readout_value = value_of(frame, readout_ref)
        readout_field = field_of(readout_ref)
        readout_fraction = fraction_of(readout_ref, readout_value, None, frame)
        draw.readout(theme, y, name_for(readout_ref),
                     draw.reading(readout_value, readout_field), readout_fraction,
                     hot=severity_of(readout_ref, readout_fraction))


def _bars(page, frame, _history, theme):
    ref = page.get("field", "")
    values = value_of(frame, ref)
    if not isinstance(values, list):
        values = [] if values is None else [values]
    # What the page says, else the full scale the host sent for it, else a percentage.
    maximum = float(page.get("max") or peak_of(ref, frame) or SCALE.get(field_of(ref))
                    or 100.0)
    names = value_of(frame, ref + LANE_NAMES)
    draw.bars(theme, values, maximum, field_of(ref),
              _swept_lanes(ref, values, maximum),
              names if isinstance(names, list) else None)


def behind_at(age_ms, since_ms):
    """How far back in the series `now` is, in samples: the age the host sent, plus ours.

    The host sends the age of its newest point, so no clocks have to be aligned and the only
    error is the trip back. Capped, or a host that stopped answering walks the plot off the
    end of its readings.
    """
    behind = (age_ms + since_ms) / float(EVERY_MS or 1000)
    if behind < 0.0:
        return 0.0
    return BEHIND_MAX if behind > BEHIND_MAX else behind


def _walk(refs=()):
    """How far back in the series a graph should draw, or None to draw it where it stands.

    None, so a still plot uses the whole of its box where a moving one keeps room on the
    right. A ref in SPACING is on its own clock, and BEHIND is counted in collector
    samples, so those are drawn still too.
    """
    if not PLOT_ANIMATION or any(ref in SPACING for ref in refs):
        return None
    return BEHIND


def _swept_lanes(ref, values, maximum):
    """Where each bar of a row should be drawn to, or None to draw them at their readings.

    A lane is a gauge in itself, keyed by position: sixteen cores are sixteen needles that
    happen to share a field.
    """
    if not ANIMATE or not maximum:
        return None
    # Keyed by a tuple and not a formatted string: sixteen lanes a frame is sixteen keys,
    # and building them cost 1ms of the 4 the whole row of sweeps takes.
    return [_swept((ref, index), max(0.0, min(1.0, (value or 0.0) / maximum)))
            for index, value in enumerate(values)]


def _graph(page, frame, history, theme):
    refs = page.get("fields", [])[:2]
    series = [history.get(ref) or [] for ref in refs]
    # If the host has no ring for this yet, at least plot the live value.
    for i, ref in enumerate(refs):
        if not series[i]:
            value = value_of(frame, ref)
            if value is not None:
                series[i] = [value, value]
    # names_for and not name_for: two domains' requests are both REQUESTS by field name,
    # so a key built from the field alone gives both series one label.
    labels = list(zip(names_for(refs), [field_of(ref) for ref in refs]))
    field = field_of(refs[0]) if refs else "pct"
    maximum = float(page["max"]) if page.get("max") else (
        100.0 if is_percent(field) else None)
    draw.graph(theme, series, labels, maximum, shift=_walk(refs))


def _grid(page, frame, _history, theme):
    refs = page.get("fields", [])[:6]
    groups = [ref.split(".")[0] for ref in refs]
    by_group = len(set(groups)) == len(groups)
    entries = []
    for ref in refs:
        value = value_of(frame, ref)
        field = field_of(ref)
        fraction = fraction_of(ref, value, None, frame)
        entries.append((name_for(ref), draw.reading(value, field), fraction,
                        icon_for(ref, by_group), severity_of(ref, fraction)))
    draw.grid(theme, entries)


# One symbol per group and per field, as characters in icons.af, built from
# ci/badge-icons.txt. A reading with no symbol falls back to its name.
GROUP_ICONS = {"cpu": "c", "gpu": "g", "mem": "m", "disk": "d", "net": "n",
               "power": "p", "fans": "f", "sys": "y"}
FIELD_ICONS = {
    "pct": "l", "temp": "t", "freq": "s", "clock": "s", "procs": "r",
    # The arrows invert between a link and a disk: a network is drawn against the machine, so
    # up leaves it; storage is drawn against the disk, so a write goes down into it.
    "up_bps": "u", "down_bps": "o", "write_bps": "o", "read_bps": "u",
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
    # Named by whatever tells them apart: CPU and GPU where NAMES would call both LOAD,
    # LOAD and TEMP where they share a subsystem.
    by_group = len(set(groups)) == len(groups)
    entries = []
    for ref, group in zip(refs, groups):
        value = value_of(frame, ref)
        field = field_of(ref)
        fraction = fraction_of(ref, value, page, frame)
        entries.append((group.upper() if by_group else name_for(ref),
                        draw.fmt(value, field), fraction,
                        icon_for(ref, by_group),
                        draw.short_unit(field),
                        severity_of(ref, fraction)))
    draw.dials(theme, entries)


def _text(page, frame, _history, theme):
    # `reading` and not `fmt`: a row here is a name and a figure, with nowhere to put a unit
    # of its own the way a gauge puts one under the needle. A battery read 86 and not 86%.
    entries = []
    for ref in page.get("fields", [])[:7]:
        value = value_of(frame, ref)
        entries.append((name_for(ref), draw.reading(value, field_of(ref))))
    draw.lines(theme, entries)


def _notify(page, frame, _history, theme):
    """Messages and counters, sorted by what each reading turned out to be.

    One slot list: a message is a dict carrying `text`, anything else is a number. So one
    page kind covers a feed, a mention, a headline and a follower count, in any mixture.
    """
    items, counters = [], []
    for ref in page.get("fields", [])[:6]:
        value = value_of(frame, ref)
        if isinstance(value, dict):
            items.append(value)
        elif value is not None or not items:
            # An empty counter still gets its label, so a page of them says what it is for.
            counters.append((name_for(ref), draw.reading(value, field_of(ref))))
    draw.notification(theme, items[:3], counters)


def _asked(call, fallback=None):
    """What the badge answers, or a fallback. A firmware that has not got one of these should
    cost the page a row and not the frame."""
    try:
        return call()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return fallback


def _size(value):
    """Bytes on the badge's scale: MB past a megabyte, KB under it."""
    if value is None:
        return "--"
    if value >= 1024 * 1024:
        return f"{value / 1048576:.1f}MB"
    return f"{value / 1024:.0f}KB"


def _uptime(ms):
    if ms is None:
        return "--"
    seconds = int(ms / 1000)
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _used_of(volume):
    """(text, fraction) for a filesystem, from badge.disk_free's total, used and free."""
    if not volume or len(volume) < 2:
        return "--", None
    total, used = volume[0], volume[1]
    if not total:
        return "--", None
    return f"{_size(used)} of {_size(total)}", used / total


# The board, firmware, clock and uid, none of which can move. Read once: `import os` and
# `import machine` are ~40ms a call here, walking sys.path every time.
_fixed = None


def _fixed_readings():
    global _fixed
    if _fixed is None:
        import machine
        import os

        uname = _asked(os.uname)
        _fixed = {
            "clock": f"{_asked(machine.freq, 0) // 1000000}MHz",
            "board": getattr(uname, "machine", None) or "unknown board",
            "release": getattr(uname, "release", "?"),
            "uid": _asked(lambda: badge.uid, "?"),
        }
    return _fixed


# gc.mem_free walks 8MB of PSRAM at 44ms and littlefs its metadata at 3.7ms, against under
# half a millisecond for the rest, so those two are on a timer.
SLOW_EVERY_MS = 3000
_slow = None
_slow_at = 0
# Settled at boot, so read once: it saves a second 44ms call on every refresh.
_heap = None


def _slow_readings():
    """Memory and the two filesystems, at most every SLOW_EVERY_MS."""
    global _slow, _slow_at, _heap
    now = time.ticks_ms()
    if _slow is not None and time.ticks_diff(now, _slow_at) < SLOW_EVERY_MS:
        return _slow
    import gc

    free = _asked(gc.mem_free, 0)
    if _heap is None:
        _heap = free + _asked(gc.mem_alloc, 0)
    _slow_at = now
    _slow = {
        "free": free,
        "held": _heap - free,
        "heap": _heap,
        "root": _asked(lambda: badge.disk_free("/")),
        "system": _asked(lambda: badge.disk_free("/system")),
    }
    return _slow


def _badge_page(_page, _frame, _history, theme):
    """The badge's own readings, which the host has no part in.

    The only page whose readings do not come from the frame, and still redrawn once a poll
    like the rest: a memory bar creeping looks the same at one frame a second as at 45.
    """
    battery = _asked(badge.battery_level)
    volts = _asked(badge.battery_voltage)
    light = _asked(badge.light_level)
    slow = _slow_readings()
    held, heap = slow["held"], slow["heap"]
    root_text, root_fraction = _used_of(slow["root"])
    system_text, system_fraction = _used_of(slow["system"])

    meters = [
        # Read backwards, as GOOD_HIGH does for the host's battery field.
        ("BATTERY", f"{battery}%" if battery is not None else "--",
         None if battery is None else battery / 100.0,
         None if battery is None else 1.0 - battery / 100.0),
        ("MEMORY", f"{_size(held)} of {_size(heap)}", (held / heap) if heap else None, None),
        ("FLASH, LITTLEFS", root_text, root_fraction, None),
        ("FLASH, FAT", system_text, system_fraction, None),
        # The fraction the backlight follows, not the raw count. The sensor's useful range is
        # the bottom two percent of its scale; look.ambient_fraction is the curve through it.
        ("AMBIENT LIGHT", "--" if light is None else str(light),
         None if light is None else look.ambient_fraction(light), None),
    ]
    fixed = _fixed_readings()
    facts = [
        ("CLOCK", fixed["clock"]),
        ("BATTERY", f"{volts:.2f}V" if isinstance(volts, float) else "--"),
        ("POWER", "charging" if _asked(badge.is_charging) else
         "usb" if _asked(badge.usb_connected) else "battery"),
        ("UPTIME", _uptime(_asked(lambda: badge.ticks))),
        ("SCREEN", "{} x {}".format(*badge.resolution) if _asked(lambda: badge.resolution)
         else "--"),
    ]
    notes = [fixed["board"], "{}  uid {}".format(fixed["release"], fixed["uid"])]
    draw.vitals(theme, meters, facts, notes)


def names_for(refs):
    """Display names that tell these readings apart.

    The field name where that is already unique - LOAD, TEMP - the group where it is
    not, and both where neither is: a page of cpu.pct and gpu.pct would otherwise be
    two rows both called LOAD.

    A group takes the host's label where one travelled with the layout, so an extension
    draws as gadgetoid.com rather than CF_GADGETOID_COM; the key cannot be turned back
    into a domain here.
    """
    plain = [name_for(ref) for ref in refs]
    if len(set(plain)) == len(plain):
        return plain
    groups = [LABELS.get(group) or group.upper()
              for group in (ref.partition(".")[0] for ref in refs)]
    if len(set(groups)) == len(groups):
        return groups
    return [f"{group} {name}" for group, name in zip(groups, plain)]


def _series_for(ref, frame, history, page=None):
    """A field's history, falling back to the live value so a cold ring still plots."""
    # The ring as it stands, not a copy: nothing here or in draw writes to it.
    points = history.get(ref) or ()
    if not points:
        value = value_of(frame, ref)
        points = [value, value] if value is not None else []
    peak = None
    if page and page.get("max"):
        peak = float(page["max"])
    elif is_percent(field_of(ref)):
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
        field = field_of(ref)
        fraction = fraction_of(ref, value, page, frame)
        # Coloured by its reading, not its position, or the outermost ring always looks calm.
        pen = (theme.at(severity_of(ref, fraction)) if fraction is not None
               else theme.grid)
        entries.append((labels[index], draw.reading(value, field), fraction, pen,
                        scale_note(ref, frame)))
    draw.rings(theme, entries)


def _spark(page, frame, history, theme):
    refs = page.get("fields", [])[:6]
    labels = names_for(refs)
    entries = []
    for index, ref in enumerate(refs):
        points, peak = _series_for(ref, frame, history, page)
        value = value_of(frame, ref)
        entries.append((labels[index], draw.reading(value, field_of(ref)),
                        points, peak))
    draw.sparklines(theme, entries)


def _radar(page, frame, _history, theme):
    refs = page.get("fields", [])[:6]
    labels = names_for(refs)
    entries = []
    for index, ref in enumerate(refs):
        value = value_of(frame, ref)
        entries.append((labels[index], draw.reading(value, field_of(ref)),
                        fraction_of(ref, value, page, frame), theme.accent))
    draw.radar(theme, entries)


def _trend(page, frame, history, theme):
    ref = page.get("field", "")
    field = field_of(ref)
    value = value_of(frame, ref)
    points, peak = _series_for(ref, frame, history, page)
    # Against a few samples back. The last one alone is mostly noise.
    delta = None
    if value is not None and len(points) > 4:
        was = points[-5]
        if was is not None:
            delta = float(value) - float(was)
    fraction = fraction_of(ref, value, page, frame)
    draw.trend(theme, draw.fmt(value, field), draw.short_unit(field), name_for(ref),
               delta, points, peak, fraction, hot=severity_of(ref, fraction),
               shift=_walk((ref,)))


# How far between polls the waterfall has got, so it interpolates and does not step.
_wf_from = ()
_wf_to = ()
_wf_seq = None
_wf_at = 0
# A poll is a second apart; the ease is over slightly less so it settles before the next.
WF_EASE_MS = 850
# Written in place: this kind draws every frame, and these only change with the core count.
# Rebuilding them was 1.7ms and 640 bytes a frame at sixteen lanes.
_wf_lanes = []
_wf_labels = None


def _waterfall(page, frame, history, theme):
    global _wf_from, _wf_to, _wf_seq, _wf_at, _wf_labels
    ref = page.get("field", "cpu.cores")
    values = value_of_list(frame, ref)
    # What the page says, else the full scale the host sent for it, else a percentage.
    maximum = float(page.get("max") or peak_of(ref, frame) or SCALE.get(field_of(ref))
                    or 100.0)

    if values and frame.get("seq") != _wf_seq:
        if not _wf_to:
            # First sight of this page: seed from the host's ring, oldest first.
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
    # Smoothstep, so a lane leaves and arrives gently instead of ramping linearly.
    eased = phase * phase * (3.0 - 2.0 * phase)
    count = len(_wf_to)
    if len(_wf_lanes) != count:
        _wf_lanes[:] = [0.0] * count
        _wf_labels = [str(i) for i in range(count)] if count <= 16 else None
    for index, target in enumerate(_wf_to):
        start = _wf_from[index] if index < len(_wf_from) else target
        _wf_lanes[index] = (start + (target - start) * eased) / maximum
    draw.waterfall(theme, _wf_lanes, _wf_labels)


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
    "notify": _notify,
    "badge": _badge_page,
}

# It interpolates between polls, so it needs a frame whether or not one landed.
ANIMATED.add("waterfall")
