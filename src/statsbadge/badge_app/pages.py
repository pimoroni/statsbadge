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

# -- sweeping gauges --------------------------------------------------------

# Whether a gauge sweeps to each new reading or steps to it, from the layout. The reading
# itself steps either way: the number is the measurement, and one redrawn at frame rate
# would bake a sprite a frame.
ANIMATE = False
# Whether a plot moves between readings, which is a separate choice from a gauge sweeping: a
# graph scrolls and a sparkline slides its points along y. Off by default, as sweeping is.
PLOT_ANIMATION = False
# How far back in the series "now" is, in samples, for the frame being drawn: 0 when a reading
# has just landed, 1 when the next is due, and more when the badge polls slower than the host
# samples and several arrive at once. From the age the host sent with the series plus the time
# since it arrived, so it owes nothing to how often this badge polls, when a reply happened to
# arrive, or whether one was missed. A graph turns it into an x offset and a sparkline into a y
# one; both read the same number.
BEHIND = 0.0
# How far apart the points are, as the host reports it; how many of them one poll of this badge
# covers, which is both numbers divided and nothing estimated; and how far behind a plot will
# draw before it gives up and shows the gap instead.
EVERY_MS = 1000
LEAD = 1
BEHIND_MAX = 12.0



def note_spacing(every_ms, interval_ms):
    """How far apart the host's points are, and how many of them a poll of ours covers.

    A badge polling slower than the host samples is handed several at a time, and a plot has to
    keep room on its right for them: exactly `interval / every` of them, both known rather than
    measured. Getting this from observed gaps is what made a plot walk at the wrong pace.
    """
    global EVERY_MS, LEAD
    EVERY_MS = int(every_ms) or 1000
    covered = -(-int(interval_ms) // EVERY_MS)      # rounded up
    LEAD = 1 if covered < 1 else (12 if covered > 12 else covered)


# Rings that are not on the collector's clock, as {"group.field": every_ms}. A source may
# answer for its own history - Cloudflare reports by the hour - and a plot of one cannot be
# walked by a number worked out from a spacing it is not on. Set from the history reply.
SPACING = {}


def note_series_spacing(spacing):
    """Which rings are on a clock of their own, from the history reply's `spacing`."""
    SPACING.clear()
    for key, entry in (spacing or {}).items():
        every = int((entry or {}).get("every_ms") or 0)
        if every:
            SPACING[key] = every


def walkable(refs):
    """Whether a plot of these can be animated at all.

    A plot is translated as a whole - one shift, one set of samples wide - so its series
    have to be on one clock for the movement to mean anything on all of them. They are
    unless a source answered for one, and an hourly ring shifted by a number worked out
    from a second is a plot sliding a year an hour.
    """
    return not any(ref in SPACING for ref in refs)


# The kinds that draw a series, and so want one fetched for them.
PLOTS = ("graph", "spark", "trend")
# The ones that move between readings, which is not all of them: a sparkline is 22px tall and
# a sample of it is 5px, so it is drawn still and only the value beside it changes.
SCROLLS = ("graph", "trend")
# How long a sweep takes. A reading lands once a second, so this is the fraction of that
# second the needle is moving for; long enough to read as motion, short enough that the
# gauge is standing at the measurement most of the time.
SWEEP_MS = 350
_sweeps = {}
# Whether the frame just drawn had a sweep in it, which is how the loop knows to ask for
# another. Set by `_swept` while drawing and cleared by `render`.
moving = False


def sweep_reset():
    """Forget where each gauge stood, so the next reading is drawn where it is.

    A page turn is not a change in the machine, and neither is turning the setting on:
    sweeping from the last page's reading would say something untrue about this one.
    """
    _sweeps.clear()


def _swept(ref, fraction):
    """`fraction`, eased from wherever this gauge already stood.

    Keyed on the field, or on the field and a position where a page draws a row of them.
    One page is drawn at a time and a turn forgets the table, so a gauge only ever meets its
    own history.
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
    # A sweep with the same endpoints is how a gauge is seeded, on the first reading and
    # after a page turn. It has nowhere to go, so it does not earn a frame.
    if not sweep.done and abs(sweep.to - sweep.from_) > 0.001:
        moving = True
    return sweep.now

# What the host calls a group an extension declared, from the layout: {"cf_pinout_xyz":
# "pinout.xyz"}. Only those, since the badge has its own shorter names for the model's own
# and a key like this one cannot be turned back into what it was named after.
LABELS = {}

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


def is_percent(field):
    """Whether a reading is already 0-100, so nothing has to say where full is.

    By suffix as well as by name: the model's own percentages carry `_pct` and it is the
    convention a source adding a group of its own is asked to follow, there being nothing
    else the badge could read it off.
    """
    return field in PERCENT or field.endswith("_pct")

# Fields where a high reading is the good one, so the ramp is read backwards to colour them.
# The ramp runs calm to alarming and almost everything here is a load or a temperature, but a
# battery at 100% is not a machine in trouble.
GOOD_HIGH = ("battery_pct",)


def severity_of(ref, fraction):
    """Where a reading sits on the ramp, which is not always where it sits on its own scale.

    Only the colour: a gauge's sweep and a bar's length are the reading itself.
    """
    if fraction is None:
        return None
    return 1.0 - fraction if ref.split(".")[-1] in GOOD_HIGH else fraction


# A unit on the end of a field name is not part of what the field is called. Every built-in
# carrying one is named in NAMES, so this is for a group that arrived with an extension:
# the reading is drawn with its unit after it, and "BYTES BPS 22KB/s" says it twice.
UNIT_SUFFIXES = ("_bps", "_mb", "_pct")


def name_for(ref):
    if ref in NAMES:
        return NAMES[ref]
    field = ref.split(".")[-1]
    for suffix in UNIT_SUFFIXES:
        if field.endswith(suffix) and len(field) > len(suffix):
            field = field[:-len(suffix)]
            break
    return field.replace("_", " ").upper()


def merge_slow(frame, held):
    """Put the slow half of a frame back into it.

    A group whose readings change far slower than the badge polls - a domain's traffic,
    fetched by the host once a minute - is sent only when it changes, so every frame after
    that one arrives without it. `held` is what came last, grafted back on here.

    `peaks` is the one key merged rather than replaced: a peak scales the reading it belongs
    to, so the slow ones travel with the slow readings while the rest arrive every frame.
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

    A rate is scaled by the busiest the host has seen it, which it sends with the frame:
    throughput has no full scale of its own, and the fixed one this used to use reads as
    pegged on a fast link and as idle on a slow one.
    """
    if value is None or isinstance(value, (str, bool)):
        return None
    field = ref.split(".")[-1]
    if page and page.get("max"):
        top = float(page["max"])
    elif is_percent(field):
        top = 100.0
    else:
        # A peak wherever the host sent one: it tracks a throughput, and whatever else a
        # source asked it to, and it is a better scale than a guess in either case.
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
    return "peak " + draw.reading(peak, ref.split(".")[-1])


def render(page, frame, history, theme, index, total, subtitle=None):
    """Draw one page. Everything before this has already cleared the screen."""
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


# -- the kinds --------------------------------------------------------------

def _dial(page, frame, _history, theme):
    ref = page.get("field", "")
    value = value_of(frame, ref)
    fraction = fraction_of(ref, value, page, frame)
    field = ref.split(".")[-1]
    # The unit slot says what full means where that is not obvious, which for a rate it
    # never is.
    under = scale_note(ref, frame) or draw.short_unit(field)
    draw.dial(theme, fraction, draw.fmt(value, field), under, cold=value is None,
              hot=severity_of(ref, fraction), backwards=field in GOOD_HIGH)
    readouts = page.get("readouts", [])[:3]
    for readout_ref, y in zip(readouts, look.readout_rows(len(readouts))):
        readout_value = value_of(frame, readout_ref)
        readout_field = readout_ref.split(".")[-1]
        readout_fraction = fraction_of(readout_ref, readout_value, None, frame)
        draw.readout(theme, y, name_for(readout_ref),
                     draw.reading(readout_value, readout_field), readout_fraction,
                     hot=severity_of(readout_ref, readout_fraction))


def _bars(page, frame, _history, theme):
    ref = page.get("field", "")
    values = value_of(frame, ref)
    if not isinstance(values, list):
        values = [] if values is None else [values]
    maximum = float(page.get("max") or 100.0)
    draw.bars(theme, values, maximum, ref.split(".")[-1],
              _swept_lanes(ref, values, maximum))


def behind_at(age_ms, since_ms):
    """How far back in the series `now` is, in samples: the age the host sent, plus ours.

    The host says how old the newest point was when it composed the reply, so nothing has to be
    aligned between two clocks and the only error is the trip back. Capped, so a host that has
    stopped answering leaves a plot at the edge of what it can honestly draw.
    """
    behind = (age_ms + since_ms) / float(EVERY_MS or 1000)
    if behind < 0.0:
        return 0.0
    return BEHIND_MAX if behind > BEHIND_MAX else behind


def _walk(refs=()):
    """How far back in the series a graph should draw, or None to draw it where it stands.

    None rather than 0.0 when nothing is animating: a plot that moves needs room for the
    samples still coming in, and one that never will should use the whole of its box. And
    none for a plot whose series are not on the collector's clock, `BEHIND` being counted
    in its samples.
    """
    if not PLOT_ANIMATION or not walkable(refs):
        return None
    return BEHIND


def _swept_lanes(ref, values, maximum):
    """Where each bar of a row should be drawn to, or None to draw them at their readings.

    A lane is a gauge of its own, keyed by its position: sixteen cores are sixteen needles
    that happen to share a field, and one core going quiet says nothing about the next.
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
    labels = [(name_for(ref), ref.split(".")[-1]) for ref in refs]
    field = refs[0].split(".")[-1] if refs else "pct"
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
        field = ref.split(".")[-1]
        fraction = fraction_of(ref, value, None, frame)
        entries.append((name_for(ref), draw.reading(value, field), fraction,
                        icon_for(ref, by_group), severity_of(ref, fraction)))
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
        fraction = fraction_of(ref, value, page, frame)
        entries.append((group.upper() if by_group else name_for(ref),
                        draw.fmt(value, field), fraction,
                        icon_for(ref, by_group),
                        draw.short_unit(field),
                        severity_of(ref, fraction)))
    draw.dials(theme, entries)


def _text(page, frame, _history, theme):
    entries = []
    for ref in page.get("fields", [])[:7]:
        value = value_of(frame, ref)
        entries.append((name_for(ref), draw.fmt(value, ref.split(".")[-1])))
    draw.lines(theme, entries)


def _asked(call, fallback=None):
    """What the badge answers, or a fallback. A firmware that has not got one of these should
    cost the page a row and not the frame."""
    try:
        return call()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return fallback


def _size(value):
    """Bytes as the badge's own scale: MB past a megabyte, KB under it."""
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


# What the badge is, as against how it is doing: the board, the firmware, the clock and the
# uid, none of which can move. Read once, because `import os` and `import machine` are ~40ms a
# call on this firmware and are not cached the way `math` and `gc` are - every one walks
# sys.path again - so they belong nowhere near a frame.
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


# Two of the badge's own readings are dear. Measured on the board: gc.mem_free walks the
# allocation table of 8MB of PSRAM and takes 44ms, and littlefs walks its own metadata to say
# how full it is, 3.7ms; the rest are under half a millisecond each. So the dear ones are taken
# on a timer and the cheap ones every frame. Three seconds, because none of them is something
# you watch move - a page of them redrawn on the poll is already 1Hz.
SLOW_EVERY_MS = 3000
_slow = None
_slow_at = 0
# The heap's size, which is settled at boot: worth one reading ever, and it saves the second
# 44ms call on every refresh after.
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
    """What the badge knows about itself, which is nothing the host has an opinion on.

    The only page whose readings do not come from the frame. It is still redrawn on a poll
    like every other page, because these are numbers rather than motion: a bar creeping as
    memory is used reads the same at one frame a second as at forty-five, and this page would
    otherwise be the app's most expensive one for the sake of a digit.
    """
    battery = _asked(badge.battery_level)
    volts = _asked(badge.battery_voltage)
    light = _asked(badge.light_level)
    slow = _slow_readings()
    held, heap = slow["held"], slow["heap"]
    root_text, root_fraction = _used_of(slow["root"])
    system_text, system_fraction = _used_of(slow["system"])

    meters = [
        # The ramp runs calm to alarming, so a nearly flat battery has to be read backwards -
        # the same inversion pages.GOOD_HIGH makes for the host's own battery field.
        ("BATTERY", f"{battery}%" if battery is not None else "--",
         None if battery is None else battery / 100.0,
         None if battery is None else 1.0 - battery / 100.0),
        ("MEMORY", f"{_size(held)} of {_size(heap)}", (held / heap) if heap else None, None),
        ("FLASH, LITTLEFS", root_text, root_fraction, None),
        ("FLASH, FAT", system_text, system_fraction, None),
        # The fraction the backlight is actually following, not the raw count: the sensor's
        # useful range is the bottom two percent of its scale and look.ambient_fraction is the
        # curve the app reads it through.
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


def group_name(ref):
    """What to call a reading's group.

    The host's name for it where one travelled with the layout, which is how an extension's
    group comes to read as gadgetoid.com and not CF_GADGETOID_COM: the key is all the badge
    has, and the dots that made it a domain cannot be put back. Otherwise the key, in the
    case the rest of the furniture is in.
    """
    group = ref.split(".")[0]
    return LABELS.get(group) or group.upper()


def names_for(refs):
    """Display names that tell these readings apart.

    The field name where that is already unique - LOAD, TEMP - the group where it is
    not, and both where neither is: a page of cpu.pct and gpu.pct would otherwise be
    two rows both called LOAD.
    """
    plain = [name_for(ref) for ref in refs]
    if len(set(plain)) == len(plain):
        return plain
    groups = [group_name(ref) for ref in refs]
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
    elif is_percent(ref.split(".")[-1]):
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
    fraction = fraction_of(ref, value, page, frame)
    draw.trend(theme, draw.fmt(value, field), draw.short_unit(field), name_for(ref),
               delta, points, peak, fraction, hot=severity_of(ref, fraction),
               shift=_walk((ref,)))


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
    "badge": _badge_page,
}

# It interpolates between polls, so it needs a frame whether or not one landed.
ANIMATED.add("waterfall")
