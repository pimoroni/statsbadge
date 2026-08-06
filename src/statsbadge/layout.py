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

from . import derive, themes

# A page kind the badge knows how to draw, and what it needs.
#   dial    one field as a sweep gauge, plus up to three readouts beside it
#   dials   up to four fields as gauges side by side, each named under its reading
#   bars    a list of fields as horizontal bars, good for per-core
#   graph   one or two fields over time, from the server's history ring
#   grid    up to six fields as big numbers
#   text    labelled lines, for names and versions
#   badge   the badge's own vitals, which need no field and come from no host
KINDS = ("dial", "dials", "bars", "graph", "grid", "text",
         "rings", "spark", "radar", "trend", "waterfall", "notify", "badge")

# How many fields a kind can draw. What is left out is the badge's own layout table.
_FIELD_MAX = {"dials": 4, "graph": 2, "grid": 6, "text": 7,
              "rings": 4, "spark": 6, "radar": 6, "notify": 6}

# How a page turn moves. "over" draws the incoming page over the outgoing one; "deck" moves
# them together, the outgoing page leaving to the left.
SLIDE_STYLES = ("off", "over", "deck")

# How the sparkline page tells one row from the next: a band behind every other row, a
# hairline between them, or nothing.
ROW_STYLES = ("zebra", "rules", "none")

# How the gauge on a dial page fills: one colour, the ramp's for the reading, or the whole ramp
# swept round the arc with what the reading has not reached left faint. Only that gauge, being
# the only one with a page to itself and the only one large enough to read a ramp off.
GAUGE_FILLS = ("solid", "ramp")

# How a derived theme picks its second accent - the colour used sparingly beside the first, which
# is a graph's second series and nothing else. A written-down palette names its own, or gets the
# accent again.
ACCENT_B_RULES = derive.ACCENT_B_RULES

# The names, from the palettes themselves: a theme is data, so adding one is a palette and
# nothing else. The tinted pair are the ones not written down anywhere - a whole palette derived
# from the one accent kept in `tint`, so what is stored is the choice and not its result, and a
# change to how one is derived reaches a badge that already has it.
TINTED = {"tinted-dark": "dark", "tinted-light": "light",
          "tinted-bold-dark": "dark", "tinted-bold-light": "light"}
# Which of them take each hue as far as sRGB allows and keep the ramp in it, as against holding
# every hue at one chroma and sending the ramp to red.
BOLD = ("tinted-bold-dark", "tinted-bold-light")
THEMES = tuple(themes.PALETTES) + tuple(TINTED)

# Themes that were a palette each and are now one of the derived pair with an accent. Measured
# against the derived ones they replace: `red` and tinted bold dark at the same hue differ by 8
# counts in the accent and nothing anywhere else, and each of the five sat within 0.003 of its
# hue's own chroma limit - which is what the bold variant does for all twelve. A stored name
# still resolves, so a badge already showing one carries on showing it.
THEME_ALIASES = {
    "red": ("tinted-bold-dark", 30.0),
    "green": ("tinted-bold-dark", 150.0),
    "cyan": ("tinted-bold-dark", 210.0),
    "amber": ("tinted-bold-dark", 60.0),
    "blueprint": ("tinted-bold-dark", 240.0),
}


def resolve_theme(theme, tint):
    """A theme name and accent, with a retired name mapped onto what replaced it.

    The accent comes from the saturated family, which is where each of those palettes had its
    own: measured, all five sat within 0.003 of their hue's chroma limit.
    """
    aliased = THEME_ALIASES.get(theme)
    if not aliased:
        return theme, tint
    name, hue = aliased
    at = derive.ACCENT_HUES.index(int(hue))
    return name, list(derive.accents("saturated")[at])

# What a picker calls a theme, where that is not its own name title cased. `dark` and `light` are
# the two nothing was designed around, so they are named for what they are.
THEME_LABELS = {"dark": "Default Dark", "light": "Default Light"}
# Where a page stops being dark and starts being light, as OKLCH lightness of the background.
PALE_FROM = 0.5


def theme_records():
    """Every theme with the label and the mode a picker needs.

    The mode is read off the palette rather than named in it: a background is either pale or it
    is not, and a theme that had to declare which could declare it wrong.
    """
    records = []
    for name in THEMES:
        palette = palette_for(name, DEFAULT_CONFIG["tint"])
        lightness = derive.oklch(palette["bg"])[0]
        records.append({
            "name": name,
            "label": THEME_LABELS.get(name),
            "mode": "light" if lightness >= PALE_FROM else "dark",
        })
    return records

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
    # Taken from the offered list rather than written out, so it cannot drift from it.
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
    # Per-extension settings, keyed by extension name. Host-side: the badge never sees
    # these, so a location or a token does not travel to it.
    "settings": {},
}


class Config:
    """The layouts, persisted, each with a revision the badge that draws it can watch.

    One layout per badge, and one for a badge that has not been given its own. The file holds
    the default at the top level and the rest under `badges`, keyed by badge id, so a file
    written before there was more than one badge reads as the default and every badge carries
    on showing what it showed.

    A badge is never sent the table: it names every other badge paired with this host, which
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
            with open(self.path) as handle:
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
            # Retired theme names are resolved here, once, so nothing downstream - a picker, a
            # palette lookup, a badge - has to know they ever existed.
            for block in [merged] + list(merged["badges"].values()):
                name, accent = resolve_theme(block.get("theme"), None)
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
        with open(tmp, "w") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    def snapshot(self):
        """The whole file, table included."""
        with self._lock:
            return copy.deepcopy(self.data)

    def layout_for(self, badge_id=None):
        """The layout one badge is configured with: its own, or the default.

        Extension settings come with it wherever they are stored, so a UI editing any badge
        sees the same ones and hands them back as it found them.
        """
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            data = copy.deepcopy(own if own is not None else self.data)
            data["settings"] = copy.deepcopy(self.data.get("settings") or {})
        data.pop("badges", None)
        return data

    def configured(self):
        """Badge ids with a layout of their own, as against those on the default."""
        with self._lock:
            return sorted(self.data.get("badges") or {})

    def all_pages(self):
        """Every page configured anywhere, deduped by id.

        What a source doing per-page work has to be told about: it is handed the pages of its
        own kinds and keys what it fetches by page id, so it needs every badge's and not one
        badge's.
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
        """The revision of the layout this badge draws, which is what it watches for a change.

        Its own layout's, so saving for one badge does not send every other badge to refetch a
        layout that has not moved.
        """
        with self._lock:
            own = (self.data.get("badges") or {}).get(str(badge_id or ""))
            return (own if own is not None else self.data).get("rev", 1)

    def _next_rev(self):
        """One counter across every layout in the file, so a revision is never reused.

        Taken as the highest anywhere plus one, rather than kept as a key of its own: a badge
        comparing what it holds with what a frame reports must never see a number it has
        already drawn.
        """
        revs = [self.data.get("rev", 1)]
        revs += [block.get("rev", 1)
                 for block in (self.data.get("badges") or {}).values()]
        return max(revs) + 1

    def replace(self, incoming, extra_kinds=(), settings_schema=None,
                page_settings_schema=None, badge_id=None):
        """Validate and store a whole layout from the UI. Returns its new revision.

        With a badge id it becomes that badge's own, whatever it was showing before; without
        one it is the default, which is what a badge with no layout of its own draws.
        """
        cleaned = validate(incoming, extra_kinds, settings_schema, page_settings_schema)
        with self._lock:
            cleaned["rev"] = self._next_rev()
            cleaned["updated_at"] = int(time.time())
            if badge_id:
                # What an extension is told is the host's answer and not a badge's - one place,
                # one API key - so it is kept at the top level however it arrives.
                self.data["settings"] = cleaned.pop("settings", None) or {}
                self.data.setdefault("badges", {})[str(badge_id)] = cleaned
            else:
                # The table belongs to the file, not to the default layout, so replacing the
                # default must not take every badge's layout with it.
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
        what this host knows about rather than what its own copy of the app happened to
        ship with. 213 bytes, on a payload that is only refetched when `rev` moves.
        """
        data = self.layout_for(badge_id)
        data.pop("settings", None)
        if capabilities:
            data["pages"] = prune(data.get("pages", []), capabilities)
            data["labels"] = group_labels(data["pages"], capabilities)
        data["palette"] = palette_for(data.get("theme"), data["tint"],
                                      data.get("accent_b", "same"))
        return data


def group_labels(pages, capabilities):
    """What to call the groups these pages draw, where the badge cannot work it out.

    A badge names a reading after its field - LOAD, TEMP - and falls back to the group where
    one page draws the same field from several of them. That is fine for `cpu` and `gpu`, and
    reads as CF_GADGETOID_COM for a group an extension named after a domain: the badge has
    only the key, and the dots that made it a domain cannot be put back.

    So the groups an extension declared travel with the layout, which is where a name the
    reader chose belongs and is refetched only when `rev` moves. The model's own are left
    out: "Processor" is read at a desk and the badge says CPU at arm's length.
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


def tint_accent(incoming, current):
    """The accent a tinted theme is built from, checked against what is offered.

    Restricted on purpose: every accent on the list has been measured to give a legible theme in
    either mode, so a chosen one cannot produce a page nobody can read. Anything unrecognised
    falls back to what was already stored rather than raising - this arrives from a UI, and a
    theme is not worth refusing a whole config over.
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
    """The palette a theme draws with, derived for the tinted four and looked up for the rest."""
    theme, tint = resolve_theme(theme, tint)
    if theme in TINTED:
        return derive.palette(tuple(tint), TINTED[theme], theme in BOLD, second)
    return themes.PALETTES.get(theme, themes.PALETTES[themes.DEFAULT])


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

    theme, aliased = resolve_theme(incoming.get("theme", out["theme"]), None)
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme!r}")
    out["theme"] = theme
    # A retired name brings its own accent with it: it named a colour, so that is the choice
    # being kept, not whatever tint happened to be stored beside it.
    out["tint"] = tint_accent(aliased or incoming.get("tint"), out["tint"])

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
    # Whether a plot moves between readings: a graph scrolls, a sparkline slides its points
    # along y. A separate choice from a gauge sweeping, and off for the same reason.
    out["plot_animation"] = bool(incoming.get("plot_animation", False))
    # How a page turn moves: not at all, the next page sliding over this one, or the two
    # travelling together like a card off a deck. Off by default, since it is a fifth of a
    # second before what the reader pressed for can be read. A bool is taken as well, from
    # before there was a choice.
    slide = incoming.get("slide", "off")
    if isinstance(slide, bool):
        slide = "over" if slide else "off"
    out["slide"] = slide if slide in SLIDE_STYLES else "off"
    # How the sparkline page separates its rows. Banded by default: six lines on one page
    # read as one plot with six traces otherwise.
    rows = incoming.get("rows", "zebra")
    out["rows"] = rows if rows in ROW_STYLES else "zebra"
    # How the dial page's gauge fills. One colour by default: the reading is one value, and
    # the ramp behind it is worth showing on some machines and clutter on others.
    fill = incoming.get("gauge_fill", "solid")
    out["gauge_fill"] = fill if fill in GAUGE_FILLS else "solid"
    # How a derived theme picks the colour it uses beside the accent. The same colour by
    # default, which is what a palette that names none has always had.
    second = incoming.get("accent_b", "same")
    out["accent_b"] = second if second in ACCENT_B_RULES else "same"
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
    elif kind == "badge":
        # Nothing to configure: the page reads the badge, so there is no field to pick and no
        # host that could fail to answer for it.
        pass
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
        if page.get("kind") == "badge":
            # The badge can always answer for itself, whatever this host can measure.
            kept.append(page)
            continue
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
