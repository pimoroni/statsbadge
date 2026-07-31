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

# A page kind the badge knows how to draw, and what it needs.
#   dial    one field as a sweep gauge, plus up to three readouts beside it
#   bars    a list of fields as horizontal bars, good for per-core
#   graph   one or two fields over time, from the server's history ring
#   grid    up to six fields as big numbers
#   text    labelled lines, for names and versions
KINDS = ("dial", "bars", "graph", "grid", "text")

THEMES = ("dark", "mono", "amber", "blueprint", "vapor")

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
    "pages": DEFAULT_PAGES,
    "buttons": {"a": None, "b": None, "c": None},
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

    def replace(self, incoming, extra_kinds=()):
        """Validate and store a whole config from the UI. Returns the new revision."""
        cleaned = validate(incoming, extra_kinds)
        with self._lock:
            cleaned["rev"] = self.data.get("rev", 1) + 1
            cleaned["updated_at"] = int(time.time())
            self.data = cleaned
        self.save()
        return cleaned["rev"]

    def for_badge(self, capabilities=None):
        """The layout as the badge should see it: pruned to fields that exist."""
        data = self.snapshot()
        if capabilities:
            data["pages"] = prune(data.get("pages", []), capabilities)
        return data


def validate(incoming, extra_kinds=()):
    """Reject anything the badge could not draw, and normalise the rest.

    The config UI is the only writer, but it arrives over HTTP so it is checked
    here: a bad `kind` on the badge is a crash dialog in a launcher, not a 400.

    `extra_kinds` are page kinds contributed by installed extensions, which the badge
    only knows how to draw once their module has been pushed to it.
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
    out["caselights"] = bool(incoming.get("caselights", out["caselights"]))
    out["graph_points"] = max(8, min(160, int(incoming.get("graph_points", 48))))

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
        out["pages"].append(_validate_page(page, seen, tuple(extra_kinds)))

    buttons = incoming.get("buttons") or {}
    out["buttons"] = {
        key: (str(buttons[key]) if buttons.get(key) else None)
        for key in ("a", "b", "c")
    }
    return out


def _validate_page(page, seen, extra_kinds=()):
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
    elif kind == "bars":
        field = page.get("field")
        if not _is_ref(field):
            raise ValueError(f"page {page_id} needs a field")
        clean["field"] = field
    elif kind in ("graph", "grid", "text"):
        fields = [f for f in (page.get("fields") or []) if _is_ref(f)]
        if not fields:
            raise ValueError(f"page {page_id} needs at least one field")
        clean["fields"] = fields[: (2 if kind == "graph" else 6)]
    else:
        # An extension kind: keep its fields, since only the badge knows the shape.
        clean["fields"] = [f for f in (page.get("fields") or []) if _is_ref(f)][:8]

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
        if page.get("kind") == "bars":
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
