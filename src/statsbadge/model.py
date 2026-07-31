"""The normalised stats a badge can draw.

One shape across every platform. A field a platform cannot answer is `None`, never
zero and never absent, so the badge can tell "idle" from "unknown" and draw "--".
Sources fill in what they know and leave the rest alone.
"""

FRAME_VERSION = 1

# Every group the wire format defines, with the fields a page may ask for. Kept
# explicit because it is also the contract the config UI and the badge read.
GROUPS = {
    "cpu": ("pct", "temp", "freq", "load", "cores", "procs"),
    "mem": ("pct", "used_mb", "total_mb", "swap_pct", "swap_used_mb"),
    "gpu": ("name", "pct", "temp", "mem_pct", "mem_used_mb", "power", "fan_pct", "clock"),
    "net": ("iface", "up_bps", "down_bps", "up_total_mb", "down_total_mb"),
    "disk": ("pct", "used_mb", "total_mb", "read_bps", "write_bps"),
    "power": ("battery_pct", "charging", "secs_left", "package_w"),
    "fans": ("name", "rpm", "pct"),
    "sys": ("host", "os", "arch", "uptime_s", "cpu_name"),
}

# What to call a group and a field in the config UI. Terse enough to fit a dropdown and
# explicit about the unit, because "mem_used_mb" tells a reader neither what it measures
# nor what a useful value looks like. The badge has its own, shorter set in pages.NAMES:
# these are read at a desk, those are read at arm's length on a 320px screen.
GROUP_LABELS = {
    "cpu": "Processor",
    "mem": "Memory",
    "gpu": "Graphics",
    "net": "Network",
    "disk": "Disk",
    "power": "Power",
    "fans": "Fans",
    "sys": "System",
}

# Every label carries its unit, because a group with both a percentage and an absolute
# reading of the same thing needs to say which is which: "Used %" against "Used GB".
FIELD_LABELS = {
    "cpu": {"pct": "Load %", "temp": "Temperature C", "freq": "Clock MHz",
            "load": "Load average", "cores": "Per-core load %",
            "procs": "Processes"},
    "mem": {"pct": "Used %", "used_mb": "Used GB", "total_mb": "Total GB",
            "swap_pct": "Swap %", "swap_used_mb": "Swap GB"},
    "gpu": {"name": "Name", "pct": "Load %", "temp": "Temperature C",
            "mem_pct": "VRAM %", "mem_used_mb": "VRAM GB",
            "power": "Power W", "fan_pct": "Fan %", "clock": "Clock MHz"},
    "net": {"iface": "Interface", "up_bps": "Upload", "down_bps": "Download",
            "up_total_mb": "Sent GB", "down_total_mb": "Received GB"},
    "disk": {"pct": "Used %", "used_mb": "Used GB", "total_mb": "Total GB",
             "read_bps": "Read", "write_bps": "Write"},
    "power": {"battery_pct": "Battery %", "charging": "Charging",
              "secs_left": "Time left", "package_w": "Package W"},
    "fans": {"name": "Name", "rpm": "Speed rpm", "pct": "Speed %"},
    "sys": {"host": "Hostname", "os": "OS", "arch": "Architecture",
            "uptime_s": "Uptime", "cpu_name": "Processor model"},
}


def label(group, field):
    """What the UI calls one field. Falls back to the raw name for anything new."""
    return FIELD_LABELS.get(group, {}).get(field, field.replace("_", " ").capitalize())


# Fields whose natural range is 0-100, so a gauge needs no scale hint.
PERCENT_FIELDS = frozenset(
    ("pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct")
)

# Sensible full-scale values for the rest, used when a page does not override it.
FULL_SCALE = {
    "temp": 100.0,      # degrees C
    "power": 250.0,     # watts, a big GPU
    "package_w": 150.0,
    "rpm": 6000.0,
    "up_bps": 12.5e6,   # 100Mbit
    "down_bps": 12.5e6,
    "read_bps": 500e6,
    "write_bps": 500e6,
    "freq": 6000.0,     # MHz
    "clock": 3000.0,
}

UNITS = {
    "pct": "%", "swap_pct": "%", "mem_pct": "%", "fan_pct": "%", "battery_pct": "%",
    "temp": "C", "power": "W", "package_w": "W", "rpm": "rpm",
    "freq": "MHz", "clock": "MHz",
    "up_bps": "B/s", "down_bps": "B/s", "read_bps": "B/s", "write_bps": "B/s",
    "used_mb": "MB", "total_mb": "MB", "swap_used_mb": "MB", "mem_used_mb": "MB",
    "up_total_mb": "MB", "down_total_mb": "MB",
    "uptime_s": "s", "secs_left": "s",
}


def empty_frame():
    """A frame with every group present and nothing known yet."""
    return {
        "v": FRAME_VERSION,
        "cpu": {},
        "mem": {},
        "gpu": [],      # a list: a machine may have more than one
        "net": {},
        "disk": {},
        "power": {},
        "fans": [],
        "sys": {},
    }


def full_scale(group, field, gpu_hint=None):
    """Full-scale value for a field, so a gauge knows where 100% is."""
    if field in PERCENT_FIELDS:
        return 100.0
    if group == "gpu" and field == "temp":
        return 110.0
    if gpu_hint and field in gpu_hint:
        return gpu_hint[field]
    return FULL_SCALE.get(field, 100.0)


def unit(field):
    return UNITS.get(field, "")


def describe():
    """What this host can actually answer, for the config UI to offer.

    Filled at runtime by the collector, which knows which sources loaded; this is
    only the static half of the contract.
    """
    return {
        "version": FRAME_VERSION,
        "groups": {name: list(fields) for name, fields in GROUPS.items()},
        "percent_fields": sorted(PERCENT_FIELDS),
        "units": dict(UNITS),
        "group_labels": dict(GROUP_LABELS),
        "field_labels": {group: dict(fields)
                         for group, fields in FIELD_LABELS.items()},
    }
