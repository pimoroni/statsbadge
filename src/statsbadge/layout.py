"""What the badge draws: pages, tiles and themes."""

import copy
import json
import threading
import time

from . import derive, state, themes

# Every page the badge can draw, and what each needs.
KINDS = {
    "dial": "one field as a sweep gauge, plus up to three readouts beside it",
    "dials": "up to four fields as gauges side by side, each named under its reading",
    "bars": "a list of fields as horizontal bars, good for per-core",
    "graph": "one or two fields over time, from the server's history ring",
    "grid": "up to six fields as big numbers",
    "text": "labelled lines, for names and versions",
    "rings": "up to four fields as arcs nested inside one another",
    "spark": "up to six fields as small plots, one to a row, each holding still",
    "radar": "up to six fields as the axes of one polygon",
    "trend": "one field over time, with how far it has moved called out",
    "waterfall": "one list field as lanes of colour, a column a frame, scrolling left",
    "notify": "up to six lines of messages and counts",
    "badge": "the badge's own vitals, which need no field and come from no host",
}

# How many fields a page can draw.
_FIELD_MAX = {"dials": 4, "graph": 2, "grid": 6, "text": 7,
              "rings": 4, "spark": 6, "radar": 6, "notify": 6}

# The widest a setting may be, as (low, high). validate() clamps rather than refusing,
# so a config edited by hand still loads.
INTERVAL_MS = (250, 60000)
GRAPH_POINTS = (8, 160)
# Zero is off.
IDLE_ADVANCE_S = (0, 3600)
ADVANCE_EVERY_S = (1, 600)
BRIGHTNESS = (0.05, 1.0)

# "over" draws the incoming page over the outgoing one; "deck" moves them together.
SLIDE_STYLES = ("off", "over", "deck")

# How the sparkline page separates one row from the next.
ROW_STYLES = ("zebra", "rules", "none")

# One colour for the reading, or the ramp swept round the arc.
GAUGE_FILLS = ("solid", "ramp")

# How a derived theme picks its second accent.
ACCENT_B_RULES = derive.ACCENT_B_RULES

# The names, from the file itself; a theme is data.
THEMES = tuple(themes.THEMES)

# Retired names, resolved once at load. Nothing downstream sees a retired name.
resolve_theme = themes.resolve
theme_records = themes.records

# Bindings the badge handles on-device and never sends to the host.
LOCAL_ACTIONS = (
    ("badge.prev", "previous page"),
    ("badge.next", "next page"),
    ("badge.brightness", "brightness"),
)

# A superset of every machine: `prune` drops the pages whose fields the host lacks.
DEFAULT_PAGES = [
    {"id": "cpu", "kind": "dial", "title": "CPU",
     "field": "cpu.pct",
     "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]},
    {"id": "cores", "kind": "bars", "title": "Cores",
     "field": "cpu.cores"},
    {"id": "gpu", "kind": "dial", "title": "GPU",
     "field": "gpu.pct",
     "readouts": ["gpu.temp", "gpu.power", "gpu.mem_pct"]},
    {"id": "mem", "kind": "dial", "title": "Memory",
     "field": "mem.pct",
     "readouts": ["mem.used_mb", "mem.total_mb", "mem.swap_pct"]},
    {"id": "net", "kind": "graph", "title": "Network",
     "fields": ["net.down_bps", "net.up_bps"]},
    {"id": "disk", "kind": "grid", "title": "Disk",
     "fields": ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]},
    {"id": "thermal", "kind": "graph", "title": "Thermals",
     "fields": ["cpu.temp", "gpu.temp"]},
    {"id": "host", "kind": "text", "title": "Host",
     "fields": ["sys.host", "sys.os", "sys.cpu_name", "sys.uptime_s",
                "power.battery_pct"]},
]

DEFAULT_CONFIG = {
    "rev": 1,
    "theme": "dark",
    # The seventh accent derive.accents() offers, not a literal triple.
    "tint": list(derive.accents()[6]),
    "interval_ms": 1000,
    "brightness": 0.8,
    "caselights": True,
    "graph_points": 48,
    "smooth": True,
    "animate": False,
    "plot_animation": False,
    "slide": "off",
    "rows": "zebra",
    "gauge_fill": "solid",
    "accent_b": "same",
    "auto_brightness": False,
    "idle_advance_s": 0,
    "advance_every_s": 10,
    "pages": DEFAULT_PAGES,
    "buttons": {"a": None, "b": None, "c": None},
    # Per extension, keyed by extension name.
    "settings": {},
}


class Config:
    """The layouts, persisted, each with a revision the badge that draws it can watch."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.data["badges"] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        with self._lock:
            merged = copy.deepcopy(DEFAULT_CONFIG)
            merged.update(stored)
            merged["badges"] = {
                str(badge_id): block
                for badge_id, block in (stored.get("badges") or {}).items()
                if isinstance(block, dict)
            }
            for block in [merged] + list(merged["badges"].values()):
                if not block.get("theme"):
                    continue
                name, accent = resolve_theme(block["theme"], None)
                block["theme"] = name
                if accent:
                    block["tint"] = accent
            self.data = merged

    def save(self):
        with self._lock:
            state.write(self.path, json.dumps(self.data, indent=2))

    def set_settings(self, name, block):
        """Store one block of settings, leaving every layout alone."""
        with self._lock:
            settings = self.data.setdefault("settings", {})
            settings[name] = {**(settings.get(name) or {}), **block}
            kept = copy.deepcopy(settings[name])
        self.save()
        return kept

    def snapshot(self):
        """Return the whole file, table included."""
        with self._lock:
            return copy.deepcopy(self.data)

    def layout_for(self, badge_id=None):
        """Return the layout one badge is configured with, or the default."""
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            data = copy.deepcopy(self.data)
            if own is not None:
                data.update(copy.deepcopy(own))
            data["settings"] = copy.deepcopy(self.data.get("settings") or {})
        data.pop("badges", None)
        return data

    def configured(self):
        """Return badge ids with a layout stored, as against those on the default."""
        with self._lock:
            return sorted(self.data.get("badges") or {})

    def all_pages(self):
        """Return every page configured anywhere, deduped by id."""
        with self._lock:
            blocks = [self.data] + list((self.data.get("badges") or {}).values())
            seen, pages = set(), []
            for block in blocks:
                for page in block.get("pages") or ():
                    if page.get("id") in seen:
                        continue
                    seen.add(page.get("id"))
                    pages.append(copy.deepcopy(page))
        return pages

    @property
    def rev(self):
        return self.rev_for(None)

    def rev_for(self, badge_id=None):
        """Return the revision of the layout this badge draws."""
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            return (own if own is not None else self.data).get("rev", 1)

    def _next_rev(self):
        """Return one counter across every layout in the file, so a revision is not reused."""
        revs = [self.data.get("rev", 1)]
        revs += [block.get("rev", 1)
                 for block in (self.data.get("badges") or {}).values()]
        return max(revs) + 1

    def replace(self, incoming, extra_kinds=(), settings_schema=None,
                page_settings_schema=None, badge_id=None):
        """Validate and store a whole layout from the UI, returning its new revision."""
        cleaned = validate(incoming, extra_kinds, settings_schema, page_settings_schema)
        with self._lock:
            cleaned["rev"] = self._next_rev()
            cleaned["updated_at"] = int(time.time())
            if badge_id:
                # Settings are global, so they are lifted out of a badge's block.
                self.data["settings"] = cleaned.pop("settings", None) or {}
                self.data.setdefault("badges", {})[str(badge_id)] = cleaned
            else:
                # Replacing the default keeps the per-badge blocks.
                kept = self.data.get("badges") or {}
                self.data = cleaned
                self.data["badges"] = kept
        self.save()
        return cleaned["rev"]

    def forget(self, badge_id):
        """Drop a badge's layout. True if there was one."""
        with self._lock:
            if str(badge_id) not in (self.data.get("badges") or {}):
                return False
            del self.data["badges"][str(badge_id)]
        self.save()
        return True

    def for_badge(self, capabilities=None, badge_id=None):
        """Return the layout as the badge should see it: pruned to fields that exist."""
        data = self.layout_for(badge_id)
        data.pop("settings", None)
        if capabilities:
            data["pages"] = prune(data.get("pages", []), capabilities)
            data["labels"] = group_labels(data["pages"], capabilities)
            data["units"] = field_units(data["pages"], capabilities)
        data["palette"] = palette_for(data.get("theme"), data["tint"],
                                      data.get("accent_b", "same"))
        return data


def field_units(pages, capabilities):
    """Return what an extension called the units of the fields these pages draw."""
    declared = capabilities.get("units") or {}
    return {field: declared[field] for _group, field in _refs_of(pages)
            if declared.get(field)}


def _refs_of(pages):
    """Return every (group, field) a page draws, from whichever key holds its refs."""
    for page in pages or ():
        refs = list(page.get("fields") or ())
        for key in ("field", "readouts"):
            value = page.get(key)
            refs += value if isinstance(value, list) else ([value] if value else [])
        for ref in refs:
            if isinstance(ref, str) and "." in ref:
                group, _dot, field = ref.partition(".")
                yield group, field


def group_labels(pages, capabilities):
    """Return what to call the groups these pages draw, where the badge cannot work it out."""
    owned = capabilities.get("group_source") or {}
    known = capabilities.get("group_labels") or {}
    labels = {}
    for page in pages or ():
        refs = list(page.get("fields") or ())
        for key in ("field", "readouts"):
            value = page.get(key)
            refs += value if isinstance(value, list) else ([value] if value else [])
        for ref in refs:
            if not isinstance(ref, str) or "." not in ref:
                continue
            group = ref.split(".")[0]
            if group in owned and known.get(group):
                labels[group] = known[group]
    return labels


def _clamped(value, bounds):
    """Return a setting brought inside its bounds."""
    low, high = bounds
    return max(low, min(high, value))


def tint_accent(incoming, current):
    """Return the accent a tinted theme is built from, checked against what is offered."""
    if isinstance(incoming, (list, tuple)) and len(incoming) >= 3:
        try:
            wanted = tuple(max(0, min(255, int(part))) for part in incoming[:3])
        except (TypeError, ValueError):
            return list(current)
        if wanted in derive.offered():
            return list(wanted)
    return list(current)


def palette_for(theme, tint, second="same"):
    """Return the palette a theme draws with: derived from the accent, or looked up."""
    theme, tint = resolve_theme(theme, tint)
    return themes.palette(theme, tint, second)


# Alpha for the first series and the second, copied from badge_app/draw.py.
SERIES_ALPHA = (200, 150)
# How far a series colour has to sit from the background, on derive.apart's 0-100 scale.
SERIES_FLOOR = 20
# A background counts as pale at this sum of its three channels.
PALE_SUM = 384


def series_colours(palette):
    """Return what a graph draws its two series in."""
    background = palette["bg"]
    pale = sum(background) >= PALE_SUM

    def over(pen, alpha):
        return tuple(round(p * alpha / 255.0 + b * (1 - alpha / 255.0))
                     for p, b in zip(pen, background, strict=True))

    alpha = SERIES_ALPHA[0] if pale else SERIES_ALPHA[1]
    accent = palette["accent"]
    second = palette.get("accent_b") or accent
    if tuple(second) != tuple(accent) and derive.apart(background, over(second, alpha)) >= SERIES_FLOOR:
        return [list(accent), list(second)]
    cold, hot = palette["ramp"][0][1], palette["ramp"][-1][1]
    order = ((cold, hot) if derive.apart(accent, cold) >= derive.apart(accent, hot)
             else (hot, cold))
    for pen in order:
        if derive.apart(background, over(pen, alpha)) >= SERIES_FLOOR:
            return [list(accent), list(pen)]
    return [list(accent), list(palette["dim"])]


def _flag(value):
    return bool(value)


def _number(bounds, cast=int):
    # A value that will not cast is a bad request, so the cast is left to raise.
    def rule(value):
        return _clamped(cast(value), bounds)
    return rule


def _one_of(options, fallback):
    def rule(value):
        return value if value in options else fallback
    return rule


DISPLAY_SETTINGS = {
    "interval_ms": _number(INTERVAL_MS),
    "brightness": _number(BRIGHTNESS, float),
    "graph_points": _number(GRAPH_POINTS),
    "idle_advance_s": _number(IDLE_ADVANCE_S),
    "advance_every_s": _number(ADVANCE_EVERY_S),
    "smooth": _flag,
    "animate": _flag,
    "plot_animation": _flag,
    "auto_brightness": _flag,
    "rows": _one_of(ROW_STYLES, "zebra"),
    "gauge_fill": _one_of(GAUGE_FILLS, "solid"),
    "accent_b": _one_of(ACCENT_B_RULES, "same"),
}


def validate(incoming, extra_kinds=(), settings_schema=None,
             page_settings_schema=None):
    """Reject anything the badge could not draw, and normalise the rest."""
    if not isinstance(incoming, dict):
        raise ValueError("config must be an object")

    out = copy.deepcopy(DEFAULT_CONFIG)

    theme, aliased = resolve_theme(incoming.get("theme", out["theme"]), None)
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r}")
    out["theme"] = theme
    # A retired theme name carries the accent it stood for, which wins over any tint sent.
    out["tint"] = tint_accent(aliased or incoming.get("tint"), out["tint"])

    for key, rule in DISPLAY_SETTINGS.items():
        out[key] = rule(incoming.get(key, DEFAULT_CONFIG[key]))

    # A field reference for the lights to follow, or a plain on and off.
    caselights = incoming.get("caselights", out["caselights"])
    out["caselights"] = caselights if _is_ref(caselights) else bool(caselights)

    # `slide` was a bool before it was a choice, and a config saved then still loads.
    slide = incoming.get("slide", DEFAULT_CONFIG["slide"])
    if isinstance(slide, bool):
        slide = "over" if slide else "off"
    out["slide"] = _one_of(SLIDE_STYLES, "off")(slide)

    pages = incoming.get("pages")
    if pages is None:
        pages = DEFAULT_PAGES
    if not isinstance(pages, list) or not pages:
        raise ValueError("pages must be a non-empty list")
    if len(pages) > 24:
        raise ValueError("too many pages (max 24)")

    seen = set()
    out["pages"] = []
    for page in pages:
        out["pages"].append(_validate_page(page, seen, tuple(extra_kinds),
                                           page_settings_schema or {}))

    buttons = incoming.get("buttons") or {}
    out["buttons"] = {
        key: (str(buttons[key]) if buttons.get(key) else None)
        for key in ("a", "b", "c")
    }
    out["settings"] = _validate_settings(incoming.get("settings"), settings_schema)
    return out


def _validate_settings(incoming, schema):
    """Keep the declared keys of each extension, in the declared type."""
    stored = {}
    if not isinstance(incoming, dict):
        return stored
    for name, block in incoming.items():
        if not isinstance(block, dict):
            continue
        declared = (schema or {}).get(name) or ()
        if not any(entry.get("key") for entry in declared):
            stored[name] = {key: value for key, value in block.items()
                            if value is None or isinstance(value, (str, int, float, bool))}
            continue
        kept = coerce_settings(block, declared)
        if kept:
            stored[name] = kept
    return stored


def coerce_settings(block, declared):
    """Return one settings block in the types declared for it, anything undeclared dropped."""
    entries = {entry["key"]: entry for entry in (declared or ()) if entry.get("key")}
    return {key: _coerce_setting(block[key], entry)
            for key, entry in entries.items() if key in block}


def _coerce_setting(value, entry):
    """Return one setting in the declared type, or None where it is not answerable."""
    kind = entry.get("type", "text")
    if kind == "bool":
        return bool(value)
    if kind == "number":
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # A browser does not enforce min and max on a typed value, so clamp here.
        if entry.get("min") is not None:
            number = max(float(entry["min"]), number)
        if entry.get("max") is not None:
            number = min(float(entry["max"]), number)
        return number
    if kind == "choice":
        options = [str(option) for option in entry.get("options", ())]
        text = "" if value is None else str(value)
        return text if text in options else entry.get("default")
    if value is None:
        return None
    return str(value).strip()[:200]


def merge_settings(from_command_line, stored):
    """Return per-extension settings, with the stored ones over anything given on the CLI."""
    merged = {name: dict(block) for name, block in (from_command_line or {}).items()}
    for name, block in (stored or {}).items():
        merged.setdefault(name, {}).update(block)
    return merged


def _validate_page(page, seen, extra_kinds=(), page_settings_schema=None):
    if not isinstance(page, dict):
        raise ValueError("a page must be an object")
    kind = page.get("kind")
    if kind not in KINDS and kind not in extra_kinds:
        raise ValueError(f"unknown page kind: {kind!r}")

    page_id = str(page.get("id") or kind)
    if page_id in seen:
        raise ValueError(f"duplicate page id: {page_id}")
    seen.add(page_id)

    clean = {"id": page_id, "kind": kind, "title": str(page.get("title") or page_id)}

    if kind in ("dial",):
        field = page.get("field")
        if not _is_ref(field):
            raise ValueError(f"page {page_id} needs a field like 'cpu.pct'")
        clean["field"] = field
        readouts = page.get("readouts") or []
        clean["readouts"] = [r for r in readouts if _is_ref(r)][:3]
    elif kind in ("bars", "trend", "waterfall"):
        field = page.get("field")
        if not _is_ref(field):
            raise ValueError(f"page {page_id} needs a field")
        clean["field"] = field
    elif kind == "badge":
        pass
    elif kind in ("dials", "graph", "grid", "text", "rings", "spark", "radar"):
        fields = [f for f in (page.get("fields") or []) if _is_ref(f)]
        if not fields:
            raise ValueError(f"page {page_id} needs at least one field")
        clean["fields"] = fields[:_FIELD_MAX.get(kind, 6)]
    else:
        # An extension's page. Keep its fields; the shape is the badge's business.
        clean["fields"] = [f for f in (page.get("fields") or []) if _is_ref(f)][:8]
        for entry in ((page_settings_schema or {}).get(kind) or ()):
            key = entry.get("key")
            if key:
                clean[key] = _coerce_setting(page.get(key), entry)

    # A max of zero or less is dropped: absent already means the badge scales the page.
    for optional in ("max", "min"):
        try:
            number = float(page[optional]) if page.get(optional) is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None and (optional == "min" or number > 0):
            clean[optional] = number
    if page.get("from_extension"):
        clean["from_extension"] = str(page["from_extension"])
    return clean


def _is_ref(value):
    """Return whether a field reference is "group.field", both non-empty."""
    if not isinstance(value, str) or value.count(".") != 1:
        return False
    group, field = value.split(".")
    return bool(group) and bool(field)


def prune(pages, capabilities):
    """Drop pages whose data this host does not produce."""
    available = capabilities.get("available", {})
    # The pages an installed extension draws. A map page declares no fields, so there is
    # nothing in the host's field list to confirm it by.
    from_extensions = {page.get("kind") for page in capabilities.get("extension_pages", ())}

    def has(ref):
        group, field = ref.split(".")
        return field in available.get(group, ())

    kept = []
    for page in pages:
        if page.get("kind") == "badge":
            # The badge page draws the badge's vitals, whatever this host can measure.
            kept.append(page)
            continue
        if page.get("kind") not in KINDS:
            # An extension page declares its own group, absent from the model's field list.
            fields = [f for f in page.get("fields", []) if has(f)]
            if (fields or page.get("from_extension")
                    or page.get("kind") in from_extensions):
                kept.append(page)
            continue
        if page.get("kind") in ("bars", "trend", "waterfall"):
            if has(page["field"]):
                kept.append(page)
        elif page.get("kind") == "dial":
            if has(page["field"]):
                page = dict(page)
                page["readouts"] = [r for r in page.get("readouts", []) if has(r)]
                kept.append(page)
        else:
            fields = [f for f in page.get("fields", []) if has(f)]
            if fields:
                page = dict(page)
                page["fields"] = fields
                kept.append(page)
    return kept or [p for p in pages if p.get("kind") == "text"] or pages[:1]
