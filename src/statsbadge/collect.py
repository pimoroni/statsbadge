"""Sampling on a timer, so an HTTP request never waits on a sensor.

Several sensors are slow - ioreg is a subprocess, LibreHardwareMonitor is another
HTTP request - and a badge polling at 1Hz should not pay for that in its own
latency. One background thread samples on an interval and requests serve the last
frame, which also means ten badges cost the same as one.
"""

import threading
import time

from . import extensions, model
from .sources import discover


class Collector:
    def __init__(self, interval=1.0, config=None, history=90, state_dir=None):
        self.interval = interval
        # When the newest point in every ring was taken, so a reply can say how old it is.
        self._history_at = 0
        self.config = config or {}
        self.sources = discover(self.config)
        # Each extension gets a store of its own under here, for what it works out as against
        # what it is told. Nothing is written until one asks for something to be kept.
        self.extensions = extensions.load(self.config, state_dir)
        self.frame = model.empty_frame()
        self.seq = 0
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._last_sample = None
        # A short ring per graphed field, so a page can draw a sparkline without the
        # badge having to have been watching.
        self.history_len = history
        self._history = {}
        # The busiest each rate has been seen to be, which is the only full scale a
        # throughput has: nothing states what a full one would be, and the link speed is
        # not reported on every platform.
        self._peaks = {}
        # The slow half of the frame as it last stood, and how many times it has changed.
        # A badge sends the number back and is sent the readings only when it is behind.
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

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.sample_once()
            except Exception:
                # The collector thread must never die; a bad source is recorded on
                # that source and the next tick tries again.
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
                # A source that lets an exception out of `sample` is not handling something
                # it knows about, so this one stays until the source clears it itself. What
                # a source expects to fail - a subprocess, a fetch - it notes and clears.
                source.note_fault(exc)

        frame["t"] = int(now * 1000)
        with self._lock:
            self.seq += 1
            frame["seq"] = self.seq
            self._push_peaks(frame)
            self._push_slow_rev(frame)
            self.frame = frame
            self._push_history(frame)
        return frame

    # -- what there is to keep --------------------------------------------

    def _declared(self):
        """The extension groups, as they stand. Recomputed rather than cached: a source
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

        A peak is worked out from the reading, so a slow reading's peak moves only when it
        does. Splitting it out too is what keeps the fast frame small: a peak is 40 bytes
        of key and six domains have twelve of them.
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
        """Number the slow half, so a badge can tell whether it already has this one.

        Compared rather than counted off a clock: a source fetching on its own schedule is
        the only thing that knows when its readings moved, and asking it would be one more
        thing for it to get wrong. The comparison is a dict of a few dozen numbers.
        """
        part = self.slow_part(frame)
        if part != self._slow_last:
            self._slow_last = part
            self._slow_rev += 1
        # In the frame either way: it is what the badge sends back to say what it holds,
        # and a badge with no slow groups at all still has to see it hold still.
        frame["slow_rev"] = self._slow_rev

    def _push_peaks(self, frame):
        """Track the high-water mark of each rate, decaying so it follows the machine.

        Without the decay one overnight transfer flattens the gauge for as long as the
        server runs. Without a peak at all a rate has to be scaled by a guess: the 100Mbit
        this used to assume reads as pegged on a gigabit link and as idle on a slow one.
        """
        peaked = {f"{group}.{field}": PEAK_FLOOR for group, field in _GRAPHED
                  if field.endswith("_bps")}
        for group, declared in self._declared().items():
            for field, entry in (declared.get("fields") or {}).items():
                if entry.get("peak"):
                    # A rate the model does not define has a floor of its own: 64KB/s is
                    # what stops a trickle filling a link's gauge, and would stop a gauge
                    # of requests a minute ever moving.
                    peaked[f"{group}.{field}"] = float(entry.get("peak_floor") or 1.0)
        for key, floor in peaked.items():
            group, field = key.split(".", 1)
            value = _dig(frame, group, field)
            if value is None:
                continue
            decayed = self._peaks.get(key, 0.0) * PEAK_DECAY
            self._peaks[key] = max(float(value), decayed, floor)
        if self._peaks:
            # Not a model group, so it is never offered as a field; it is scale, not a
            # reading.
            frame["peaks"] = {key: round(value) for key, value in self._peaks.items()}

    def _push_history(self, frame):
        """One point per sample per field, aligned to the sample clock.

        A field with nothing in it gets None rather than being skipped: the ring's positions
        are what a plot reads times off, so leaving a sample out of one ring and not the others
        would draw an intermittent field's history compressed and mis-timed. A None is also
        what a plot needs to draw a gap where there was no reading.
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

        A source that fetches its own history answers for it: the collector's interval is
        the rate a sensor is read at, and nothing to do with how often a domain's traffic
        is reported. One that raises is skipped rather than taking the reply with it.
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

        `every_ms` is the spacing of the positions and `age_ms` how old the newest is, so a
        plot can place every point on a time axis without knowing anything about this host's
        clock or about how often the badge asked. Ages rather than timestamps: nothing has to
        be aligned between two machines, and the only error left is the trip back.

        Those two are the collector's own and cover every ring it keeps. A source answering
        for its own history is on a different clock, so with `spacing` its rings come too,
        each with the pair that belongs to it. Without it they are left out rather than
        served under a spacing that is not theirs: an app that cannot read the difference
        would animate an hourly series as though it arrived every second.
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

        Derived from the live frame, not from what a source claims, so a laptop with
        no fan header does not offer a fan page.
        """
        frame = self.latest()
        declared = self._declared()
        available = {}
        for group in list(model.GROUPS) + sorted(declared):
            value = frame.get(group)
            if isinstance(value, list):
                if value:
                    available[group] = sorted({k for item in value for k in item})
            elif isinstance(value, dict) and value:
                available[group] = sorted(value)
        described = model.describe()
        _merge_declared(described, declared)
        return {
            "available": available,
            "sources": [
                {"name": s.name, "provides": list(s.provides),
                 "faults": s.faults, "last_fault": s.last_fault}
                for s in self.sources + self.extensions
            ],
            # Which extension each declared group belongs to, so a picker can head them
            # with it. Only the declared ones: what is not in here is this host.
            "group_source": extensions.group_owners(self.extensions),
            # What has a history ring. A graph of anything else can only draw the live
            # value twice, which is a flat line whatever the machine is doing. Rings the
            # collector keeps and rings a source answers for itself both count: they are
            # the same thing to whoever is choosing a field.
            "graphed": [f"{group}.{field}" for group, field in
                        _GRAPHED + self._extra("graphed") + self._extra("history")],
            "series_fields": [f"{group}.{field}"
                              for group, field in _GRAPHED_SERIES + self._extra("series")],
            # Extensions are reported by the server, which describes every discovered one and
            # not only those that loaded. Two lists under one name was one list too many.
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

# Fields whose value is already a list, kept as a ring of lists so a page can plot one
# lane per element over time. Rounded to whole numbers and held shorter than the scalar
# rings: a ring of twelve-core samples is twelve times the wire cost of a scalar one.
_GRAPHED_SERIES = (
    ("cpu", "cores"),
)
SERIES_LEN = 64

# A peak left alone halves in about ten minutes at a sample a second, so the scale follows
# the machine rather than remembering one busy night. The floor keeps a quiet link from
# scaling a trickle up to a full ring.
PEAK_DECAY = 0.99885
PEAK_FLOOR = 64 * 1024.0


def _merge_declared(described, declared):
    """Fold the extensions' own groups into the contract the config UI reads.

    The model's tables are the built-in groups and cannot know about a group that arrived
    with a pip install, so what an extension declares is merged in beside them: a picker
    then names its fields and units the way it names everything else, and a gauge is
    offered the ones with a top end.
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
    described["percent_fields"].sort()
    described["list_fields"].sort()


def _dig(frame, group, field):
    value = frame.get(group)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get(field)
    return None
