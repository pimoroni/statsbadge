"""Sampling on a timer, so an HTTP request never waits on a sensor.

Several sensors are slow - ioreg is a subprocess, LibreHardwareMonitor is another
HTTP request - and a badge polling at 1Hz should not pay for that in its
latency. One background thread samples on an interval and requests serve the last
frame, which also means ten badges cost the same as one.
"""

import importlib
import threading
import time

from . import extensions, model
from .sources import discover

# Everything on a frame besides a group of readings, so anything walking one can step
# over them. app.js keeps a copy and a test holds the two together.
FRAME_SCALARS = ("v", "t", "seq", "slow_rev")


class Collector:
    def __init__(self, interval=1.0, config=None, history=90, state_dir=None):
        self.interval = interval
        # When the newest point in every ring was taken, for a reply to say its age.
        self._history_at = 0
        self.config = config or {}
        self.sources = discover(self.config)
        # A store per extension, for what it works out as against what it is told. Nothing is
        # written until one asks to keep something.
        self.state_dir = state_dir
        self.extensions = extensions.load(self.config, state_dir)
        self.frame = model.empty_frame()
        self.seq = 0
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._last_sample = None
        # A short ring per graphed field, so a page can plot without the badge having watched.
        self.history_len = history
        self._history = {}
        # The busiest each rate has been seen to be, which is the only full scale a throughput
        # has: link speed is not reported on every platform.
        self._peaks = {}
        # The slow half of the frame and how many times it has changed. A badge sends the number
        # back and gets the readings only when it is behind.
        self._slow_last = None
        self._slow_rev = 0

    # -- lifecycle ----------------------------------------------------------

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
        """Hand the host config to the sources, and take up any that can run now.

        Two halves, and the second is the one that bites: `available()` is asked once, at
        startup, so LibreHardwareMonitor answers no while its server is down or on another
        port. A URL typed in the browser then reaches a source that was never built, and
        nothing reads it until statsbadge is started again.
        """
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
            # Rebound rather than appended to: a sample walking the list finishes on the
            # one it started with.
            self.sources = self.sources + fresh
        return told

    def reload_extensions(self):
        """Pick up whatever is installed now. Returns the names loaded.

        entry_points() walks sys.path on every call, so one installed since start is
        visible without a restart. Rebinding the list is atomic, and a sample already
        walking the old one finishes on it.

        An extension that was loaded before is kept as it stands. Building it again would
        throw away what it has fetched and start its clock over.
        """
        importlib.invalidate_caches()
        running = {source.name: source for source in self.extensions}
        kept = []
        for source in extensions.load(self.config, self.state_dir):
            already = running.pop(source.name, None)
            if already is not None:
                kept.append(already)
                continue
            try:
                source.start()
            except Exception as exc:
                source.note_fault(exc)
            kept.append(source)
        self.extensions = kept
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
                # The collector thread must never die: a fault is recorded on its source and the
                # next tick tries again.
                pass

    # -- sampling -----------------------------------------------------------

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

    # -- what there is to keep --------------------------------------------

    def _declared(self):
        """The extension groups, as they stand. Recomputed and not cached, since a source
        that discovers its groups sets them on itself while running."""
        return extensions.model_groups(self.extensions)

    def _extra(self, want):
        """Extension fields flagged with `want`, as (group, field) pairs."""
        return tuple(
            (group, field)
            for group, declared in sorted(self._declared().items())
            for field, entry in sorted((declared.get("fields") or {}).items())
            if entry.get(want)
        )

    def slow_groups(self):
        """The groups whose readings change far slower than a badge polls.

        A domain's traffic is fetched once a minute and sent sixty times, and six of them
        take a frame from 832 bytes to 4.7KB. So a source says which of its groups are like
        that, and `/v1/stats` leaves them out of a frame for a badge that already has them.
        """
        return {group for group, declared in self._declared().items()
                if declared.get("slow")}

    def slow_part(self, frame=None):
        """The slow half of a frame: those groups, and the peaks that belong to them.

        A peak is worked out from the reading, and a slow reading's peak moves only when
        it does. Splitting it out too keeps the fast frame small, a peak being 40 bytes of
        key where six domains have twelve of them.
        """
        frame = self.frame if frame is None else frame
        slow = self.slow_groups()
        part = {group: frame[group] for group in slow if group in frame}
        peaks = {ref: value for ref, value in (frame.get("peaks") or {}).items()
                 if ref.split(".")[0] in slow}
        if peaks:
            part["peaks"] = peaks
        return part

    def _push_slow_rev(self, frame):
        """Number the slow half, letting a badge tell whether it already has this one.

        Compared, and not counted off a clock. A source fetching on its own schedule is
        the only place the moment its readings moved is known, and asking it would be one
        more thing to get wrong. The comparison is a dict of a few dozen numbers.
        """
        part = self.slow_part(frame)
        if part != self._slow_last:
            self._slow_last = part
            self._slow_rev += 1
        # In the frame either way: the badge sends it back to say what it holds.
        frame["slow_rev"] = self._slow_rev

    def _push_peaks(self, frame, dt):
        """Track the high-water mark of each rate, decaying so it follows the machine.

        Without the decay one overnight transfer flattens the gauge for as long as the
        server runs. Without a peak at all a rate is scaled by a guess, and a fixed 100Mbit
        reads as pegged on a gigabit link and as idle on a slow one.
        """
        peaked = {f"{group}.{field}": PEAK_FLOOR for group, field in _GRAPHED
                  if field.endswith("_bps")}
        for group, declared in self._declared().items():
            for field, entry in (declared.get("fields") or {}).items():
                if entry.get("peak"):
                    # A per-field floor: 64KB/s stops a trickle filling a link's gauge, and
                    # would stop a gauge of requests a minute ever moving.
                    peaked[f"{group}.{field}"] = float(entry.get("peak_floor") or 1.0)
        decay = 0.5 ** (max(dt, 0.0) / PEAK_HALF_LIFE_S)
        for key, floor in peaked.items():
            group, field = key.split(".", 1)
            value = _dig(frame, group, field)
            if value is None:
                continue
            decayed = self._peaks.get(key, 0.0) * decay
            self._peaks[key] = max(float(value), decayed, floor)
        # Whatever a source put there stands under these: LibreHardwareMonitor reports
        # how high each rail has been, which is past guessing here. Scale and not a
        # reading, so none of it is offered as a field.
        given = dict(frame.get("peaks") or {})
        if self._peaks or given:
            # Three places, or a voltage rounds to one.
            given.update({key: round(value, 3) for key, value in self._peaks.items()})
            frame["peaks"] = given

    def _push_history(self, frame):
        """One point per sample per field, aligned to the sample clock.

        A field with nothing in it gets None instead of being skipped. A plot reads times
        off the ring's positions, and leaving a sample out of one ring alone draws an
        intermittent field's history compressed and mis-timed. A None is also how a plot
        draws a gap where there was no reading.
        """
        self._history_at = frame["t"]
        for group, field in _GRAPHED + self._extra("graphed"):
            value = _dig(frame, group, field)
            key = f"{group}.{field}"
            ring = self._history.get(key)
            if ring is None:
                if value is None:
                    # Nothing has ever been read for this field; do not start a ring of Nones
                    # for a machine that has no such sensor.
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

    # -- reading ------------------------------------------------------------

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
        """The rings the sources keep themselves, on whatever spacing they are really on.

        A source that fetches its own history answers for it. The collector's interval is
        the rate a sensor is read at, and nothing to do with how often a domain's traffic
        is reported. One that raises is skipped, and does not take the reply with it.
        """
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
        """The same rings, plus when they were taken.

        `every_ms` is the spacing and `age_ms` how old the newest point is, so a plot can place
        every point on a time axis. Ages and not timestamps, so no clocks have to be aligned and
        the only error left is the trip back.

        Both are the collector's. A source answering for its own history is on another
        clock, so `spacing` brings its rings with the pair belonging to each.

        Without it they are left out: served under a spacing not theirs, an older app
        animates an hourly series as a per-second one.
        """
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
        """Which fields this host actually produced, for the config UI to offer.

        Derived from the live frame and not from what a source claims, so a laptop with
        no fan header does not offer a fan page. For a group the model does not define it
        is both: in the frame, and named in the declaration.

        A source may put anything in a group it owns - the quake feed carries the events
        its page draws from - and only what it declared is a reading somebody can point a
        dial at.
        """
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
                # `<field>_names` labels the lanes of the field beside it, which the badge
                # reads for itself. Nothing points a dial at one.
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
            # Which extension each declared group belongs to. What is absent here is this host.
            "group_source": extensions.group_owners(self.extensions),
            # What has a history ring: a graph of anything else draws the live value twice, a
            # flat line whatever the machine is doing. Collector rings and source-answered rings
            # both count.
            "graphed": [f"{group}.{field}" for group, field in
                        _GRAPHED + self._extra("graphed") + self._extra("history")],
            "series_fields": [f"{group}.{field}"
                              for group, field in _GRAPHED_SERIES + self._extra("series")],
            # From the server, which describes every discovered extension and not only those
            # that loaded.
            "interval": self.interval,
            "uptime_s": int(time.time() - self.started_at),
            **described,
        }


# The fields worth keeping a ring for. Anything a page draws as a graph.
_GRAPHED = (
    ("cpu", "pct"), ("cpu", "temp"),
    ("mem", "pct"),
    ("gpu", "pct"), ("gpu", "temp"),
    ("net", "up_bps"), ("net", "down_bps"),
    ("disk", "read_bps"), ("disk", "write_bps"),
    ("power", "package_w"),
)

# Fields already carrying a list, kept as a ring of lists so a page can plot a lane per
# element. Rounded and held shorter: twelve-core samples cost twelve times a scalar ring
# on the wire.
_GRAPHED_SERIES = (
    ("cpu", "cores"),
)
SERIES_LEN = 64

# How long a peak left alone takes to halve, so it follows the machine. Applied against
# the time between samples, which is a setting: a factor per sample would make the peak
# fall twice as fast at half the rate.
PEAK_HALF_LIFE_S = 600.0
# The floor keeps a quiet link from scaling a trickle to a full ring.
PEAK_FLOOR = 64 * 1024.0


def _merge_declared(described, declared):
    """Fold the extensions' groups into the contract the config UI reads.

    The model's tables cover the built-in groups and nothing that arrived with a pip
    install, so what an extension declares is merged in beside them. A picker then names
    its fields and units the way it names everything else, and a gauge is offered the ones
    with a top end.
    """
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
