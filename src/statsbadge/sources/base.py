"""What a source has to implement."""

import subprocess
import threading
import urllib.error
import urllib.parse

from .. import geocode, layout, state


class SourceError(Exception):
    """A fault written to be read, which is shown as it stands."""


def readable(exc):
    """A fault as one line somebody can act on."""
    if isinstance(exc, SourceError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        where = urllib.parse.urlsplit(exc.url or "").netloc
        return f"HTTP {exc.code} {exc.reason}" + (f" from {where}" if where else "")
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason)
        if "timed out" in reason or "timeout" in reason.lower():
            return "the connection timed out"
        return f"cannot reach it: {reason}"
    if isinstance(exc, subprocess.TimeoutExpired):
        command = exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
        return f"{command} did not finish inside {exc.timeout:g}s"
    if isinstance(exc, TimeoutError):
        return "it timed out"
    return f"{type(exc).__name__}: {exc}"


class Source:
    name = "source"

    # What the config UI heads this source's groups with, where `name` does not read as
    # a title. Without one the name is titled.
    label = None

    # Which groups this source can contribute to, for the config UI's benefit.
    provides = ()

    # What this source can be told, for the config UI to offer and the server to store.
    # Each entry is a dict:
    #
    #   key       the name it arrives under in self.config
    #   label     what the UI calls it
    #   type      "text", "number", "bool" or "choice"
    #   options   the allowed values, for "choice"
    #   default   what it is worth when nothing is stored
    #   hint      a line of explanation, optional
    #   secret    an API key or token, kept masked behind a button in the UI
    #
    # What is not declared here is only settable from --extension.
    settings = ()

    # What this source puts in the frame that the model does not define, keyed by group:
    #
    #   label     what the UI calls the group
    #   slow      the readings change far slower than the badge polls, so they travel
    #             only when they change
    #   fields    one entry per field, keyed by the name it arrives under:
    #       label       what the UI calls it, unit included
    #       unit        what a badge prints after the reading
    #       full_scale  where a gauge's ring ends, for a reading with a top end
    #       percent     the reading is already 0-100
    #       graphed     keep a history ring, so a graph has something to plot
    #       history     the source answers for the ring, through `series()`, on
    #                   whatever spacing the readings are really on
    #       peak        scale a gauge by the busiest this has been seen, as a rate is
    #       list        the value is a list, for the kinds that draw one lane each
    #       item        a message and not a number, for a `notify` page: {"title": who
    #                   from, "text": the body, "age_s": how long ago, "note": an
    #                   optional qualifier}
    groups = {}

    # Settings belonging to one page and not to the source, in the shape of `settings`.
    # A source doing different work per page implements `pages(instances)`.
    page_settings = ()

    # Where the badge is: `place`, `latitude` and `longitude`, set by the host. A page
    # naming a location overrides it.
    home = {}

    def __init__(self, config):
        # Typed and defaulted where declared. Left as the same dict where not: the
        # built-in sources read the collector's own, which changes under them.
        self.config = layout.settle_settings(config, self.settings) if self.settings else config
        self.faults = 0
        # What stands in the way of each part of this source's work, oldest first.
        self._faults_standing = {}
        # What this source worked out, as against what it was told. The persistent one
        # is in place by the time `start` runs.
        self.store = state.Store()
        # Shared with every other source, so a town is looked up once for the install.
        self.geocode = geocode.Geocoder()

    def location(self, page=None):
        """Return where a page is set to, or where the badge is: (latitude, longitude, label)."""
        for where in (page, self.home):
            where = where or {}
            latitude, longitude = where.get("latitude"), where.get("longitude")
            if latitude is not None and longitude is not None:
                return (float(latitude), float(longitude), None)
            place = (where.get("place") or "").strip()
            if place:
                return self.geocode.lookup(place)
        return None

    def configure(self, settings):
        """Take settings while running, on every save and not only on a change."""
        self.config.update(layout.settle_settings(settings, self.settings, defaults=False))

    def series(self):
        """Return the rings this source keeps itself, keyed "group.field".

            {"cf_pinout_xyz.requests": {"points": [12.0, 9.5, None, ...],
                                        "every_ms": 3600000, "age_ms": 240000}}

        `points` runs oldest to newest, `None` where there was no reading, `every_ms` is
        how far apart they are and `age_ms` how old the newest is now. Declare the field
        with `history` rather than `graphed` so the collector keeps no ring of its own.

        Called on a request's thread as a reply is composed, while `sample` may be
        running on the collector's, so nothing here may wait on a network and what it
        reads has to be safe to read from there.
        """
        return {}

    def pages(self, instances):
        """Take the pages configured for this source's kinds, on every config change."""

    @classmethod
    def available(cls, _config=None):
        """Return True if this source can run here, without sampling or subprocesses."""
        return False

    def start(self):
        """Run once before the first sample. Spawn helpers here, and read `store` here."""

    def stop(self):
        """Run on shutdown. Reap helpers here."""

    def sample(self, frame, dt):
        """Fill in what this source can measure.

        `frame` is a dict from model.empty_frame(); mutate it. `dt` is seconds since the
        previous sample. Only set a field if the value is real, so a later source can
        fill it. Every source shares the collector's thread, so anything that waits on a
        network belongs on a thread started by `start`.
        """
        raise NotImplementedError

    @property
    def last_fault(self):
        """The oldest fault still standing, for the config UI and `statsbadge probe`."""
        return next(iter(self._faults_standing.values()), None)

    @last_fault.setter
    def last_fault(self, text):
        # For an extension that sets it itself. None clears every part's.
        if text is None:
            self._faults_standing.clear()
        else:
            self._faults_standing[None] = text

    def note_fault(self, exc, key=None):
        """Record that this source's work failed. `key` names which part of it, where a
        source does several things that fail apart."""
        self.faults += 1
        self._faults_standing[key] = readable(exc)

    def note_ok(self, key=None):
        """Record that the work `key` names succeeded, which clears its fault.

        A source has to call this itself, at the point the work a fault was noted for
        succeeded: `sample` handing over the last good reading is no evidence that the
        next fetch landed. The count is kept.
        """
        self._faults_standing.pop(key, None)

    def note_waiting(self, why, key=None):
        """Record why this source is doing nothing, such as a setting it has not been
        given, without counting it as a fault. `note_ok` clears it."""
        self._faults_standing[key] = why

    def __repr__(self):
        return f"<{self.name}>"


class PollingSource(Source):
    """A source that fetches on a thread of its own, which calls `poll` about once a
    second and at once on `wake`. `poll` decides whether anything is due."""

    POLL_S = 1.0

    def __init__(self, config):
        super().__init__(config)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._poller = None

    def poll(self):
        """Fetch whatever is due. A raise is noted as a fault and the thread carries on."""
        raise NotImplementedError

    def sample(self, frame, dt):
        raise NotImplementedError

    @property
    def stopping(self):
        return self._stop.is_set()

    def start(self):
        if self._poller is None:
            self._stop.clear()
            self._poller = threading.Thread(target=self._run, daemon=True,
                                            name=f"statsbadge-{self.name}")
            self._poller.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._poller is not None:
            self._poller.join(timeout=2.0)
            self._poller = None

    def wake(self):
        """Poll now, as after a setting that changes what to fetch."""
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001
                self.note_fault(exc)
            self._wake.wait(self.POLL_S)
            self._wake.clear()
