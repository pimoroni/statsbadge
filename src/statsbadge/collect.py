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
            self.frame = frame
            self._push_history(frame)
        return frame

    def _push_peaks(self, frame):
        """Track the high-water mark of each rate, decaying so it follows the machine.

        Without the decay one overnight transfer flattens the gauge for as long as the
        server runs. Without a peak at all a rate has to be scaled by a guess: the 100Mbit
        this used to assume reads as pegged on a gigabit link and as idle on a slow one.
        """
        for group, field in _GRAPHED:
            if not field.endswith("_bps"):
                continue
            value = _dig(frame, group, field)
            if value is None:
                continue
            key = f"{group}.{field}"
            decayed = self._peaks.get(key, 0.0) * PEAK_DECAY
            self._peaks[key] = max(float(value), decayed, PEAK_FLOOR)
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
        for group, field in _GRAPHED:
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

        for group, field in _GRAPHED_SERIES:
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

    def history_at(self, keys=None, points=48):
        """The same rings, plus when they were taken.

        `every_ms` is the spacing of the positions and `age_ms` how old the newest is, so a
        plot can place every point on a time axis without knowing anything about this host's
        clock or about how often the badge asked. Ages rather than timestamps: nothing has to
        be aligned between two machines, and the only error left is the trip back.
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
        return {"every_ms": int(self.interval * 1000), "age_ms": age, "series": series}

    def capabilities(self):
        """Which fields this host actually produced, for the config UI to offer.

        Derived from the live frame, not from what a source claims, so a laptop with
        no fan header does not offer a fan page.
        """
        frame = self.latest()
        available = {}
        for group in model.GROUPS:
            value = frame.get(group)
            if isinstance(value, list):
                if value:
                    available[group] = sorted({k for item in value for k in item})
            elif isinstance(value, dict) and value:
                available[group] = sorted(value)
        return {
            "available": available,
            "sources": [
                {"name": s.name, "provides": list(s.provides),
                 "faults": s.faults, "last_fault": s.last_fault}
                for s in self.sources + self.extensions
            ],
            # What has a history ring. A graph of anything else can only draw the live
            # value twice, which is a flat line whatever the machine is doing.
            "graphed": [f"{group}.{field}" for group, field in _GRAPHED],
            "series_fields": [f"{group}.{field}" for group, field in _GRAPHED_SERIES],
            # Extensions are reported by the server, which describes every discovered one and
            # not only those that loaded. Two lists under one name was one list too many.
            "interval": self.interval,
            "uptime_s": int(time.time() - self.started_at),
            **model.describe(),
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


def _dig(frame, group, field):
    value = frame.get(group)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get(field)
    return None
