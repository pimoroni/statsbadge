"""Sampling on a timer, so an HTTP request never waits on a sensor."""

import importlib
import threading
import time

from . import extensions, geocode, model
from .sources import discover

# A frame's other values are groups of readings, so a walk over the groups skips these
# four. web/js/live.js hardcodes the same list, and
# test_a_frame_is_walked_past_its_own_scalars holds them equal.
FRAME_SCALARS = ("v", "t", "seq", "slow_rev")

# The fields worth keeping a ring for.
_GRAPHED = (
    ("cpu", "pct"), ("cpu", "temp"),
    ("mem", "pct"),
    ("gpu", "pct"), ("gpu", "temp"),
    ("net", "up_bps"), ("net", "down_bps"),
    ("disk", "read_bps"), ("disk", "write_bps"),
    ("power", "package_w"),
)

# Fields already carrying a list, kept as a ring of lists so a page can plot a lane per
# element. Rounded and held shorter: twelve-core samples cost twelve times a scalar ring.
_GRAPHED_SERIES = (
    ("cpu", "cores"),
)
SERIES_LEN = 64

# How long a peak left alone takes to halve. Applied against the time between samples,
# since the interval is a setting.
PEAK_HALF_LIFE_S = 600.0
# Keeps a quiet link from scaling a trickle to a full ring.
PEAK_FLOOR = 64 * 1024.0


class Collector:
    def __init__(self, interval=1.0, config=None, history=90, state_dir=None,
                 geocoder=None):
        self.interval = interval
        self._history_at = 0
        self.config = config or {}
        self.sources = discover(self.config)
        self.state_dir = state_dir
        self.geocoder = geocoder or geocode.Geocoder()
        self.extensions = extensions.load(self.config, state_dir, self.geocoder)
        # What each was loaded at, so reload_extensions can tell an upgrade from a
        # restart of the same code. Superseded extensions are listed in `stale`.
        self.versions = extensions.versions()
        self.stale = []
        extensions.set_home(self.sources + self.extensions, self.config)
        self.frame = model.empty_frame()
        self.seq = 0
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._last_sample = None
        self.history_len = history
        self._history = {}
        self._peaks = {}
        self._slow_last = None
        self._slow_rev = 0

    def start(self):
        for source in self.sources + self.extensions:
            try:
                source.start()
            except Exception as exc:
                source.note_fault(exc)
        self.sample_once()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="statsbadge-collector")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for source in self.sources + self.extensions:
            try:
                source.stop()
            except Exception:
                pass

    def reconfigure(self):
        """Hand the host config to the sources, and take up any that can run now."""
        extensions.set_home(self.sources + self.extensions, self.config)
        told = []
        for source in list(self.sources):
            handler = getattr(source, "reconfigure", None)
            if handler is None:
                continue
            try:
                handler(self.config)
            except Exception as exc:
                source.note_fault(exc)
                continue
            told.append(getattr(source, "name", "source"))

        have = {type(source) for source in self.sources}
        fresh = [source for source in discover(self.config) if type(source) not in have]
        running = self._thread is not None and self._thread.is_alive()
        for source in fresh:
            if running:
                try:
                    source.start()
                except Exception as exc:
                    source.note_fault(exc)
            told.append(getattr(source, "name", "source"))
        if fresh:
            # Rebinding leaves a sample already walking the list on the old one.
            self.sources = self.sources + fresh
        return told

    def reload_extensions(self):
        """Pick up whatever is installed now, returning the names loaded."""
        importlib.invalidate_caches()
        now = extensions.versions()
        moved = {name for name, version in now.items()
                 if name in self.versions and version != self.versions[name]}
        refreshed = set(extensions.forget(moved))
        self.stale = sorted(moved - refreshed)

        running = {source.name: source for source in self.extensions}
        retired, kept = [], []
        for source in extensions.load(self.config, self.state_dir, self.geocoder):
            already = running.pop(source.name, None)
            if already is not None and source.name not in refreshed:
                kept.append(already)
                continue
            if already is not None:
                retired.append(already)
            try:
                source.start()
            except Exception as exc:
                source.note_fault(exc)
            kept.append(source)
        self.extensions = kept
        self.versions = now
        for gone in retired:
            try:
                gone.stop()
            except Exception:
                pass
        extensions.set_home(kept, self.config)
        for gone in running.values():
            try:
                gone.stop()
            except Exception:
                pass
        return [source.name for source in kept]

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.sample_once()
            except Exception:
                # The collector thread must never die: the next tick has another go.
                pass

    def sample_once(self):
        now = time.monotonic()
        dt = (now - self._last_sample) if self._last_sample else self.interval
        self._last_sample = now

        frame = model.empty_frame()
        for source in self.sources + self.extensions:
            try:
                source.sample(frame, dt)
            except Exception as exc:
                # The fault stays on the source until it clears it.
                source.note_fault(exc)

        frame["t"] = int(now * 1000)
        with self._lock:
            self.seq += 1
            frame["seq"] = self.seq
            self._push_peaks(frame, dt)
            self._push_slow_rev(frame)
            self.frame = frame
            self._push_history(frame)
        return frame

    def _declared(self):
        """Return the extension groups, at their current values."""
        return extensions.model_groups(self.extensions)

    def _extra(self, want):
        """Return extension fields flagged with `want`, as (group, field) pairs."""
        return tuple(
            (group, field)
            for group, declared in sorted(self._declared().items())
            for field, entry in sorted((declared.get("fields") or {}).items())
            if entry.get(want)
        )

    def slow_groups(self):
        """Return the groups whose readings change far slower than a badge polls."""
        return {group for group, declared in self._declared().items()
                if declared.get("slow")}

    def slow_part(self, frame=None):
        """Return the slow half of a frame: those groups, and the peaks belonging to them."""
        frame = self.frame if frame is None else frame
        slow = self.slow_groups()
        part = {group: frame[group] for group in slow if group in frame}
        peaks = {ref: value for ref, value in (frame.get("peaks") or {}).items()
                 if ref.split(".")[0] in slow}
        if peaks:
            part["peaks"] = peaks
        return part

    def _push_slow_rev(self, frame):
        """Number the slow half, so a badge can recognise one it already has."""
        part = self.slow_part(frame)
        if part != self._slow_last:
            self._slow_last = part
            self._slow_rev += 1
        # In the frame either way: the badge sends it back to name the revision it has.
        frame["slow_rev"] = self._slow_rev

    def _push_peaks(self, frame, dt):
        """Track the high-water mark of each rate, decaying so it follows the machine."""
        peaked = {f"{group}.{field}": PEAK_FLOOR for group, field in _GRAPHED
                  if field.endswith("_bps")}
        for group, declared in self._declared().items():
            for field, entry in (declared.get("fields") or {}).items():
                if entry.get("peak"):
                    # Per field, since the 64KB/s floor below would stop a gauge of
                    # requests a minute ever moving.
                    peaked[f"{group}.{field}"] = float(entry.get("peak_floor") or 1.0)
        decay = 0.5 ** (max(dt, 0.0) / PEAK_HALF_LIFE_S)
        for key, floor in peaked.items():
            group, field = key.split(".", 1)
            value = _dig(frame, group, field)
            if value is None:
                continue
            decayed = self._peaks.get(key, 0.0) * decay
            self._peaks[key] = max(float(value), decayed, floor)
        # Whatever a source put there stands under these. These are scales for other
        # fields, so they go in `peaks`.
        given = dict(frame.get("peaks") or {})
        if self._peaks or given:
            # Three places, or a voltage rounds to one.
            given.update({key: round(value, 3) for key, value in self._peaks.items()})
            frame["peaks"] = given

    def _push_history(self, frame):
        """Keep one point per sample per field, aligned to the sample clock."""
        self._history_at = frame["t"]
        for group, field in _GRAPHED + self._extra("graphed"):
            value = _dig(frame, group, field)
            key = f"{group}.{field}"
            ring = self._history.get(key)
            if ring is None:
                if value is None:
                    # Nothing has ever been read for this field, so no ring of Nones.
                    continue
                ring = self._history[key] = []
            ring.append(None if value is None else round(float(value), 1))
            if len(ring) > self.history_len:
                del ring[0 : len(ring) - self.history_len]

        for group, field in _GRAPHED_SERIES + self._extra("series"):
            values = _dig(frame, group, field)
            if not isinstance(values, list) or not values:
                continue
            key = f"{group}.{field}"
            ring = self._history.get(key)
            if ring is None:
                ring = self._history[key] = []
            ring.append([int(round(float(v or 0.0))) for v in values])
            if len(ring) > SERIES_LEN:
                del ring[0 : len(ring) - SERIES_LEN]

    def latest(self):
        with self._lock:
            return self.frame

    def history(self, keys=None, points=48):
        with self._lock:
            wanted = keys or list(self._history)
            return {
                key: self._history.get(key, [])[-points:]
                for key in wanted
                if key in self._history
            }

    def source_series(self, keys=None, points=48):
        """Return the rings the sources keep themselves, on their own spacing."""
        wanted = set(keys) if keys else None
        found = {}
        for source in self.sources + self.extensions:
            try:
                offered = source.series() or {}
            except Exception as exc:
                source.note_fault(exc)
                continue
            for key, entry in offered.items():
                if wanted is not None and key not in wanted:
                    continue
                given = list(entry.get("points") or ())[-points:]
                found[key] = {
                    "points": given,
                    "every_ms": int(entry.get("every_ms") or 0)
                                or int(self.interval * 1000),
                    "age_ms": max(0, int(entry.get("age_ms") or 0)),
                }
        return found

    def history_at(self, keys=None, points=48, spacing=False):
        """Return the same rings, plus when they were taken."""
        with self._lock:
            wanted = keys or list(self._history)
            series = {
                key: self._history.get(key, [])[-points:]
                for key in wanted
                if key in self._history
            }
            age = 0
            if self._history_at:
                age = max(0, int(time.monotonic() * 1000) - self._history_at)
        reply = {"every_ms": int(self.interval * 1000), "age_ms": age, "series": series}
        if spacing:
            own = self.source_series(keys, points)
            for key, entry in own.items():
                series[key] = entry["points"]
            reply["spacing"] = {key: {"every_ms": entry["every_ms"],
                                      "age_ms": entry["age_ms"]}
                                for key, entry in own.items()}
        return reply

    def capabilities(self):
        """Return which fields this host actually produced, for the config UI to offer."""
        frame = self.latest()
        declared = self._declared()
        available = {}
        for group in list(model.GROUPS) + sorted(declared):
            value = frame.get(group)
            offered = (declared.get(group) or {}).get("fields")
            if isinstance(value, list):
                if value:
                    available[group] = sorted({k for item in value for k in item})
            elif isinstance(value, dict) and value:
                keys = value if offered is None else (
                    key for key in value if key in offered)
                # `<field>_names` labels the lanes of the field beside it, which the
                # badge reads for itself.
                available[group] = sorted(key for key in keys
                                          if not key.endswith("_names"))
        described = model.describe()
        _merge_declared(described, declared)
        return {
            "available": available,
            "sources": [
                {"name": s.name, "provides": list(s.provides),
                 "faults": s.faults, "last_fault": s.last_fault}
                for s in self.sources + self.extensions
            ],
            # Which extension each declared group belongs to; an unlisted group is the host's.
            "group_source": extensions.group_owners(self.extensions),
            # What has a history ring. A graph of anything else draws the live value
            # twice. Collector rings and source-answered rings both count.
            "graphed": [f"{group}.{field}" for group, field in
                        _GRAPHED + self._extra("graphed") + self._extra("history")],
            "series_fields": [f"{group}.{field}"
                              for group, field in _GRAPHED_SERIES + self._extra("series")],
            "interval": self.interval,
            "uptime_s": int(time.time() - self.started_at),
            **described,
        }


def _merge_declared(described, declared):
    """Fold the extensions' groups into the contract the config UI reads."""
    described["declared_fields"] = {
        group: {name: {key: field[key] for key in ("unit", "full_scale", "percent")
                       if field.get(key)}
                for name, field in (entry.get("fields") or {}).items()}
        for group, entry in declared.items()}
    for group, entry in declared.items():
        fields = entry.get("fields") or {}
        described["groups"][group] = sorted(fields)
        described["group_labels"][group] = entry.get("label") or group
        described["field_labels"][group] = {
            name: field.get("label") or name.replace("_", " ").capitalize()
            for name, field in fields.items()
        }
        for name, field in fields.items():
            if field.get("unit"):
                described["units"][name] = field["unit"]
            if field.get("full_scale"):
                described["full_scale"][name] = float(field["full_scale"])
            if field.get("percent") and name not in described["percent_fields"]:
                described["percent_fields"].append(name)
            if field.get("list") and name not in described["list_fields"]:
                described["list_fields"].append(name)
            if field.get("item") and name not in described["item_fields"]:
                described["item_fields"].append(name)
    described["percent_fields"].sort()
    described["list_fields"].sort()
    described["item_fields"].sort()


def _dig(frame, group, field):
    value = frame.get(group)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get(field)
    return None
