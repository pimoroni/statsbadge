"""What the badge draws: pages, tiles and themes.

A page is data, not code, wherever it can be. The badge ships a handful of page
kinds - a dial, a bar stack, a graph, a text readout - and a page says which kind it
is and which fields go in it. That way rearranging a display is a config change the
badge picks up on its next poll, with no install.

`rev` increments on every change. The badge sees it in each stats frame and refetches
the layout only when it moves, so the common case is one small GET a second.
"""

import copy
import json
import os
import threading
import time

from . import themes

# A page kind the badge knows how to draw, and what it needs.
#   dial    one field as a sweep gauge, plus up to three readouts beside it
#   dials   up to four fields as gauges side by side, each named under its reading
#   bars    a list of fields as horizontal bars, good for per-core
#   graph   one or two fields over time, from the server's history ring
#   grid    up to six fields as big numbers
#   text    labelled lines, for names and versions
KINDS = ("dial", "dials", "bars", "graph", "grid", "text",
         "rings", "spark", "radar", "trend", "waterfall")

# How many fields a kind can draw. What is left out is the badge's own layout table.
_FIELD_MAX = {"dials": 4, "graph": 2, "grid": 6, "text": 7,
              "rings": 4, "spark": 6, "radar": 6}

# The names, from the palettes themselves: a theme is data, so adding one is a palette and
# nothing else.
THEMES = tuple(themes.PALETTES)

# Button bindings the badge answers itself, and never sends here: paging and the panel are its
# own business, and a round trip would be slower than the press. Offered to the UI alongside
# the host's commands, which is the only reason this list is on this side at all.
LOCAL_ACTIONS = (
    ("badge.prev", "previous page"),
    ("badge.next", "next page"),
    ("badge.brightness", "brightness"),
)

# What to show on a machine nobody has configured. Only pages whose fields the host
# actually produces survive `prune`, so this is a superset on purpose.
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
    "interval_ms": 1000,
    "brightness": 0.8,
    "caselights": True,
    "graph_points": 48,
    "smooth": True,
    "animate": False,
    "slide": False,
    "auto_brightness": False,
    "idle_advance_s": 0,
    "advance_every_s": 10,
    "pages": DEFAULT_PAGES,
    "buttons": {"a": None, "b": None, "c": None},
    # Per-extension settings, keyed by extension name. Host-side: the badge never sees
    # these, so a location or a token does not travel to it.
    "settings": {},
}


class Config:
    """The layout, persisted, with a revision the badge can watch."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            with open(self.path) as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        with self._lock:
            merged = copy.deepcopy(DEFAULT_CONFIG)
            merged.update(stored)
            self.data = merged

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            payload = json.dumps(self.data, indent=2)
        with open(tmp, "w") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.data)

    @property
    def rev(self):
        with self._lock:
            return self.data.get("rev", 1)

    def replace(self, incoming, extra_kinds=(), settings_schema=None,
                page_settings_schema=None):
        """Validate and store a whole config from the UI. Returns the new revision."""
        cleaned = validate(incoming, extra_kinds, settings_schema, page_settings_schema)
        with self._lock:
            cleaned["rev"] = self.data.get("rev", 1) + 1
            cleaned["updated_at"] = int(time.time())
            self.data = cleaned
        self.save()
        return cleaned["rev"]

    def for_badge(self, capabilities=None):
        """The layout as the badge should see it: pruned to fields that exist.

        The chosen theme travels as its colours and not only its name, so the badge draws
        what this host knows about rather than what its own copy of the app happened to
        ship with. 213 bytes, on a payload that is only refetched when `rev` moves.
        """
        data = self.snapshot()
        data.pop("settings", None)
        if capabilities:
            data["pages"] = prune(data.get("pages", []), capabilities)
        data["palette"] = themes.PALETTES.get(data.get("theme"), themes.PALETTES[themes.DEFAULT])
        return data


def validate(incoming, extra_kinds=(), settings_schema=None,
             page_settings_schema=None):
    """Reject anything the badge could not draw, and normalise the rest.

    The config UI is the only writer, but it arrives over HTTP so it is checked
    here: a bad `kind` on the badge is a crash dialog in a launcher, not a 400.

    `extra_kinds` are page kinds contributed by installed extensions, which the badge
    only knows how to draw once their module has been pushed to it, and
    `page_settings_schema` is what those kinds let a single page be told. Anything a
    kind has not declared is dropped, so a page cannot smuggle keys to the badge.
    """
    if not isinstance(incoming, dict):
        raise ValueError("config must be an object")

    out = copy.deepcopy(DEFAULT_CONFIG)

    theme = incoming.get("theme", out["theme"])
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r}")
    out["theme"] = theme

    interval = int(incoming.get("interval_ms", out["interval_ms"]))
    # Under about 250ms the badge spends its whole frame budget on HTTP.
    out["interval_ms"] = max(250, min(60000, interval))

    brightness = float(incoming.get("brightness", out["brightness"]))
    out["brightness"] = max(0.05, min(1.0, brightness))
    # Off, the theme's own level, or a field reference for the lights to follow.
    caselights = incoming.get("caselights", out["caselights"])
    out["caselights"] = caselights if _is_ref(caselights) else bool(caselights)
    out["graph_points"] = max(8, min(160, int(incoming.get("graph_points", 48))))
    # Whether a graph is a curve through its samples or a polyline between them.
    out["smooth"] = bool(incoming.get("smooth", True))
    # Whether a gauge sweeps to each new reading or steps to it. Off by default: a reading
    # that arrives once a second and moves for a third of it is a choice, and on a noisy
    # field - a throughput that halves between polls - the sweep reads as lag.
    out["animate"] = bool(incoming.get("animate", False))
    # Whether a page turn slides the next page on like a card off a deck. Off by default:
    # it is a quarter of a second before the reader sees what they pressed for.
    out["slide"] = bool(incoming.get("slide", False))
    # Whether the badge takes its brightness down to suit a dim room. Off by default: it is
    # the badge's own sensor and not every board has one.
    out["auto_brightness"] = bool(incoming.get("auto_brightness", False))
    # How long the badge waits for a press before it starts paging on its own, and how long
    # it then holds each page. Zero is off, which is the default: a display that moves while
    # somebody is reading it is a nuisance. An hour is the longest wait worth offering, and a
    # page has to be up for at least a second to be seen at all.
    out["idle_advance_s"] = max(0, min(3600, int(incoming.get("idle_advance_s", 0))))
    out["advance_every_s"] = max(1, min(600, int(incoming.get("advance_every_s", 10))))

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

    None rather than a default, so a field cleared in the UI reads as unset: a source
    asking for a latitude wants to be able to tell "not set" from "the equator".
    """
    kind = entry.get("type", "text")
    if kind == "bool":
        return bool(value)
    if kind == "number":
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
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
    elif kind in ("dials", "graph", "grid", "text", "rings", "spark", "radar"):
        fields = [f for f in (page.get("fields") or []) if _is_ref(f)]
        if not fields:
            raise ValueError(f"page {page_id} needs at least one field")
        clean["fields"] = fields[:_FIELD_MAX.get(kind, 6)]
    else:
        # An extension kind: keep its fields, since only the badge knows the shape.
        clean["fields"] = [f for f in (page.get("fields") or []) if _is_ref(f)][:8]
        for entry in ((page_settings_schema or {}).get(kind) or ()):
            key = entry.get("key")
            if key:
                clean[key] = _coerce_setting(page.get(key), entry)

    for optional in ("max", "min"):
        if page.get(optional) is not None:
            clean[optional] = float(page[optional])
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

    def has(ref):
        group, field = ref.split(".")
        return field in available.get(group, ())

    kept = []
    for page in pages:
        if page.get("kind") not in KINDS:
            # An extension page: it declares its own group, so the model's field list
            # is not the authority on whether the host produces it.
            fields = [f for f in page.get("fields", []) if has(f)]
            if fields or page.get("from_extension"):
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
