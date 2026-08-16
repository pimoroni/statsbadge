"""What the badge draws: pages, tiles and themes.

A page is data wherever it can be: the badge ships the kinds, and a page names one and
the fields that go in it. Rearranging a display is then a config change the badge picks
up on its next poll.

`rev` increments on every change. The badge sees it in each stats frame and refetches
the layout only when it moves, so the common case is one small GET a second.
"""

import copy
import json
import os
import threading
import time

from . import derive, themes

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

# The widest a setting may be, as (low, high). validate() clamps to these rather than
# refusing, so a config edited by hand still loads. The sliders in the UI offer a narrower
# range inside them, which someone is likely to want.
#   Under 250ms the badge spends its whole frame budget on HTTP; a minute is the longest
#   gap where a reading still reads as live.
INTERVAL_MS = (250, 60000)
#   Eight points is a plot with a shape; past 160 they are under a pixel apart.
GRAPH_POINTS = (8, 160)
#   Zero is off, and an hour is the longest wait before paging unattended.
IDLE_ADVANCE_S = (0, 3600)
#   How long each page is held once it is paging by itself.
ADVANCE_EVERY_S = (1, 600)
BRIGHTNESS = (0.05, 1.0)

# "over" draws the incoming page over the outgoing one; "deck" moves them together.
SLIDE_STYLES = ("off", "over", "deck")

# How the sparkline page separates one row from the next: a band behind every other row, a
# hairline between them, or nothing.
ROW_STYLES = ("zebra", "rules", "none")

# One colour for the reading, or the ramp swept round the arc. Only the dial's gauge is
# large enough to read a ramp off.
GAUGE_FILLS = ("solid", "ramp")

# How a derived theme picks its second accent. A written-down palette names one, or gets
# the accent again.
ACCENT_B_RULES = derive.ACCENT_B_RULES

# The names, from the file itself; a theme is data. The file also holds which take an
# accent, what each is called, and which half of the picker each belongs in.
THEMES = tuple(themes.THEMES)

# Retired names, resolved once at load. Nothing downstream sees a retired name.
resolve_theme = themes.resolve
theme_records = themes.records

# Bindings the badge handles on-device and never sends to the host. Here so the UI can
# offer them alongside the host's commands.
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
    """The layouts, persisted, each with a revision the badge that draws it can watch.

    One layout per badge, and one for a badge with nothing saved yet. The file holds the
    default at the top level and the rest under `badges`, keyed by badge id, so a single-badge
    file is the default and every badge carries on showing what it showed.

    A badge is never sent the table. It names every other badge paired with this host, which
    is nothing to do with the one asking.
    """

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
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            payload = json.dumps(self.data, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    def set_settings(self, name, block):
        """Store one block of settings, leaving every layout alone.

        Settings are the host's answers and not a badge's, so nothing here moves a
        revision: no badge refetches for a Windows sensor URL.
        """
        with self._lock:
            settings = self.data.setdefault("settings", {})
            settings[name] = {**(settings.get(name) or {}), **block}
            kept = copy.deepcopy(settings[name])
        self.save()
        return kept

    def snapshot(self):
        """The whole file, table included."""
        with self._lock:
            return copy.deepcopy(self.data)

    def layout_for(self, badge_id=None):
        """The layout one badge is configured with, or the default.

        Extension settings come with it wherever they are stored, so a UI editing any badge
        sees the same ones and hands them back as it found them.
        """
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            data = copy.deepcopy(self.data)
            if own is not None:
                data.update(copy.deepcopy(own))
            data["settings"] = copy.deepcopy(self.data.get("settings") or {})
        data.pop("badges", None)
        return data

    def configured(self):
        """Badge ids with a layout stored, as against those on the default."""
        with self._lock:
            return sorted(self.data.get("badges") or {})

    def all_pages(self):
        """Every page configured anywhere, deduped by id.

        What a source doing per-page work has to be told about. It is handed the pages of
        the kinds it draws and keys what it fetches by page id, so it needs every badge's.
        """
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
        """The revision of the layout this badge draws, which it watches for changes.

        The one on its layout, so saving for one badge leaves every other badge holding
        what it already drew.
        """
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            return (own if own is not None else self.data).get("rev", 1)

    def _next_rev(self):
        """One counter across every layout in the file, so a revision is never reused.

        Taken as the highest anywhere plus one, and not kept as a key. A badge comparing
        what it holds with what a frame reports must never see a number it has already
        drawn.
        """
        revs = [self.data.get("rev", 1)]
        revs += [block.get("rev", 1)
                 for block in (self.data.get("badges") or {}).values()]
        return max(revs) + 1

    def replace(self, incoming, extra_kinds=(), settings_schema=None,
                page_settings_schema=None, badge_id=None):
        """Validate and store a whole layout from the UI. Returns its new revision.

        With a badge id it becomes that badge's, whatever it was showing before. Without
        one it is the default, drawn by any badge with nothing saved.
        """
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
        """Drop a badge's layout. True if there was one.

        Called when a pairing is forgotten: the layout would otherwise sit in the file naming a
        badge nothing can reach, and be handed to whatever next held that id.
        """
        with self._lock:
            if str(badge_id) not in (self.data.get("badges") or {}):
                return False
            del self.data["badges"][str(badge_id)]
        self.save()
        return True

    def for_badge(self, capabilities=None, badge_id=None):
        """The layout as the badge should see it: pruned to fields that exist.

        The chosen theme travels as its colours and not only its name, so the badge draws
        this host's palette and not whatever its copy of the app shipped with. 213 bytes,
        on a payload that is only refetched when `rev` moves.
        """
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
    """What an extension called the units of the fields these pages draw.

    A field the badge cannot place gets no unit at all, and a graph of kWh is then a picture
    of a number.

    Everything declared, the model's included: the badge answers for the families it
    rescales itself and reads this for the rest, so a fan's rpm arrives here rather than
    being a bare number on the page.
    """
    declared = capabilities.get("units") or {}
    return {field: declared[field] for _group, field in _refs_of(pages)
            if declared.get(field)}


def _refs_of(pages):
    """Every (group, field) a page draws, from whichever key holds its refs."""
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
    """What to call the groups these pages draw, where the badge cannot work it out.

    The badge falls back to the group key where one page draws a field from several, which is
    fine for `cpu` and comes out CF_GADGETOID_COM for a group named after a domain. The dots
    cannot be recovered from the key.

    So an extension's declared groups travel with the layout. The model's are left out: the
    badge shows CPU at arm's length.
    """
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
    """A setting brought inside its bounds, which are a (low, high) pair up the file."""
    low, high = bounds
    return max(low, min(high, value))


def tint_accent(incoming, current):
    """The accent a tinted theme is built from, checked against what is offered.

    Restricted to a measured list. Every accent on it gives a legible theme in either
    mode, and a chosen one cannot produce a page nobody can read.

    Anything unrecognised falls back to what was stored, and does not raise. This arrives
    from a UI, and a theme is not worth refusing a whole config over.
    """
    if isinstance(incoming, (list, tuple)) and len(incoming) >= 3:
        try:
            wanted = tuple(max(0, min(255, int(part))) for part in incoming[:3])
        except (TypeError, ValueError):
            return list(current)
        if wanted in derive.offered():
            return list(wanted)
    return list(current)


def palette_for(theme, tint, second="same"):
    """The palette a theme draws with: derived from the accent, or looked up."""
    theme, tint = resolve_theme(theme, tint)
    return themes.palette(theme, tint, second)


# Alpha for the first series and the second, copied from badge_app/draw.py.
SERIES_ALPHA = (200, 150)
# How far a series colour has to sit from the background, on derive.apart's 0-100 scale.
SERIES_FLOOR = 20
# A background counts as pale at this sum of its three channels.
PALE_SUM = 384


def series_colours(palette):
    """What a graph draws its two series in.

    Worked out here for the config UI's preview, which then carries no rule of its own.
    `draw._series_colour` is where the behaviour lives; a check holds the two together.
    """
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


def validate(incoming, extra_kinds=(), settings_schema=None,
             page_settings_schema=None):
    """Reject anything the badge could not draw, and normalise the rest.

    The config UI is the only writer, but it arrives over HTTP: a bad `kind` on the badge is
    a crash dialog in a launcher and not a 400.

    `extra_kinds` are the kinds installed extensions contribute, and `page_settings_schema`
    what those let a single page be told. Anything undeclared is dropped, so a page cannot
    smuggle keys to the badge.
    """
    if not isinstance(incoming, dict):
        raise ValueError("config must be an object")

    out = copy.deepcopy(DEFAULT_CONFIG)

    theme, aliased = resolve_theme(incoming.get("theme", out["theme"]), None)
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r}")
    out["theme"] = theme
    # A retired theme name carries the accent it stood for, which wins over any tint sent
    # with it.
    out["tint"] = tint_accent(aliased or incoming.get("tint"), out["tint"])

    interval = int(incoming.get("interval_ms", out["interval_ms"]))
    out["interval_ms"] = _clamped(interval, INTERVAL_MS)

    brightness = float(incoming.get("brightness", out["brightness"]))
    out["brightness"] = _clamped(brightness, BRIGHTNESS)
    # Off, the theme's level, or a field reference for the lights to follow.
    caselights = incoming.get("caselights", out["caselights"])
    out["caselights"] = caselights if _is_ref(caselights) else bool(caselights)
    out["graph_points"] = _clamped(int(incoming.get("graph_points", 48)), GRAPH_POINTS)
    # Whether a graph is a curve through its samples or a polyline between them.
    out["smooth"] = bool(incoming.get("smooth", True))
    # Whether a gauge sweeps to each new reading or steps to it. Off by default: on a noisy
    # field the sweep looks like lag.
    out["animate"] = bool(incoming.get("animate", False))
    # Whether a plot moves between readings. Separate from `animate`, and off for the same
    # reason.
    out["plot_animation"] = bool(incoming.get("plot_animation", False))
    # How a page turn moves. Off by default: it is a fifth of a second before what the reader
    # pressed for can be read. A bool is taken too, from before there was a choice.
    slide = incoming.get("slide", "off")
    if isinstance(slide, bool):
        slide = "over" if slide else "off"
    out["slide"] = slide if slide in SLIDE_STYLES else "off"
    # How the sparkline page separates its rows. Banded by default, or six lines read as one
    # plot with six traces.
    rows = incoming.get("rows", "zebra")
    out["rows"] = rows if rows in ROW_STYLES else "zebra"
    # How the dial page's gauge fills. One colour by default.
    fill = incoming.get("gauge_fill", "solid")
    out["gauge_fill"] = fill if fill in GAUGE_FILLS else "solid"
    # The colour used beside the accent. The same colour by default, as a palette naming
    # none gets.
    second = incoming.get("accent_b", "same")
    out["accent_b"] = second if second in ACCENT_B_RULES else "same"
    # Whether the badge dims to suit the room. Off by default: not every board has the sensor.
    out["auto_brightness"] = bool(incoming.get("auto_brightness", False))
    out["idle_advance_s"] = _clamped(int(incoming.get("idle_advance_s", 0)), IDLE_ADVANCE_S)
    out["advance_every_s"] = _clamped(
        int(incoming.get("advance_every_s", 10)), ADVANCE_EVERY_S)

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
    """Keep the declared keys of each extension, in the declared type.

    An extension with no schema keeps its block as it stands, because uninstalling or
    disabling one must not be what throws away everything it was told.
    """
    stored = {}
    if not isinstance(incoming, dict):
        return stored
    for name, block in incoming.items():
        if not isinstance(block, dict):
            continue
        declared = {entry["key"]: entry
                    for entry in (schema or {}).get(name, ())
                    if entry.get("key")}
        if not declared:
            stored[name] = {key: value for key, value in block.items()
                            if value is None or isinstance(value, (str, int, float, bool))}
            continue
        kept = {}
        for key, entry in declared.items():
            if key in block:
                kept[key] = _coerce_setting(block[key], entry)
        if kept:
            stored[name] = kept
    return stored


def _coerce_setting(value, entry):
    """One setting in the type it was declared as, or None where it is not answerable.

    None and not a default, so a field cleared in the UI comes through unset. A source
    asking for a latitude has to tell "unset" from "the equator".
    """
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
    return str(value)[:200]


def merge_settings(from_command_line, stored):
    """Per-extension settings, with the stored ones over anything given on the CLI.

    The UI is the live editor, so what it saved wins; --extension is for a first run and
    for a host with no browser near it.
    """
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
    """A field reference is "group.field", both non-empty."""
    if not isinstance(value, str) or value.count(".") != 1:
        return False
    group, field = value.split(".")
    return bool(group) and bool(field)


def prune(pages, capabilities):
    """Drop pages whose data this host does not produce.

    A laptop with no discrete GPU should not page through an empty GPU dial, and the
    user should not have to know that to get a sensible default.
    """
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
            # An extension page declares its own group, which is absent from the model's field
            # list.
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
