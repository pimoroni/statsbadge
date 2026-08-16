"""What a source has to implement.

The class attributes here are declarations the config UI reads. `provides` and `groups`
say what lands in the frame, `settings` and `page_settings` say what can be set, and
`label` names the source in the picker.

All four are read off the instance and not off the class. A source that only learns its
groups from the network, a domain per site an account holds, can set them in `__init__`
and have them offered as soon as they are known.
"""

import subprocess
import urllib.error
import urllib.parse

from .. import state


def readable(exc):
    """A fault as one line somebody can act on.

    `type(exc).__name__: exc` is what an exception says about itself, and for the ones a
    source actually hits it says it twice: `HTTPError: HTTP Error 503: Service Unavailable`.
    The name is kept for anything not recognised here, since an unexpected fault is worth
    knowing the type of.
    """
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
    # a title: "cloudflare" is a package, "Cloudflare" is the name. Without one the name
    # is titled.
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
    # A source with no settings gets no UI section. What is not declared here is only
    # settable from --extension.
    settings = ()

    # What this source puts in the frame that the model does not define, for the config
    # UI to offer and `prune` to keep a page drawing. Keyed by group name:
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
    # A source doing different work per page implements `pages(instances)`, handed every
    # page of its kinds whenever the config changes.
    page_settings = ()

    def __init__(self, config):
        self.config = config
        self.faults = 0
        self.last_fault = None
        # What this source worked out, as against what it was told. Namespaced by source
        # name and written by the host. This one is in memory; the persistent one is in
        # place by the time `start` runs.
        self.store = state.Store()

    def configure(self, settings):
        """Take settings while running, on every save and not only on a change.

        The default suits a source that reads `self.config` as it samples. One that
        copies values out in `__init__` has to override this and copy them again.

        Merged, not replaced: a key that does not arrive keeps the value it had. A field
        is cleared by sending it as null, as the config UI does. A caller that omits it
        leaves the source on the value it had.
        """
        self.config.update(settings)

    def series(self):
        """Rings this source keeps itself, keyed "group.field".

        The collector samples a ring at its own interval, which is right for a sensor and
        wrong for anything fetched: ninety samples of a reading that moves once a minute is
        a minute and a half of staircase. A source that can answer for its own history -
        Cloudflare reports by the hour, a day at a time - hands one over here instead, on
        the spacing it is really on:

            {"cf_pinout_xyz.requests": {"points": [12.0, 9.5, None, ...],
                                        "every_ms": 3600000, "age_ms": 240000}}

        `points` runs oldest to newest, `None` where there was no reading, `every_ms` is
        how far apart they are and `age_ms` how old the newest is now. Declare the field
        with `history` rather than `graphed` so the collector keeps no ring of its own.

        Called on the collector's thread as a reply is composed, so nothing here may wait
        on a network: hand over what the fetcher last brought back.
        """
        return {}

    def pages(self, instances):
        """Take the pages configured for this source's kinds, on every config change.

        `instances` is a list of page dicts, each carrying whatever `page_settings`
        declared. A source that samples the same thing for every page ignores this.
        """

    @classmethod
    def available(cls, _config=None):
        """True if this source can run here. Cheap: no sampling, no subprocesses.

        The config is optional because the two callers differ: a built-in source is handed
        it (`sources/__init__.py`), an extension is not (`extensions.py`). Keep the
        parameter, or moving a source between the two raises TypeError.
        """
        return False

    def start(self):
        """Called once before the first sample. Spawn helpers here, and read `store` here."""

    def stop(self):
        """Called on shutdown. Reap helpers here."""

    def sample(self, frame, dt):
        """Fill in what this source can measure.

        `frame` is a dict from model.empty_frame(); mutate it. `dt` is seconds since
        the previous sample, for anything that needs a rate. Only set a field if the
        value is real - leave it absent so a later source can fill it.

        Be prompt. Every source shares the collector's thread, and the first sample is
        taken while the server is starting up. Anything that waits on a network belongs
        on a thread started by `start`, with this serving what it last brought back.
        """
        raise NotImplementedError

    def note_fault(self, exc):
        """Record that this source's work failed, for the config UI and `statsbadge probe`."""
        self.faults += 1
        self.last_fault = readable(exc)

    def note_ok(self):
        """Record that the work succeeded, which is what clears a fault.

        A source has to call this itself, since nothing outside it can tell. One that
        fetches on a thread fails and recovers on its own schedule, and `sample` handing
        over the last good reading is no evidence that the next fetch landed.

        So this goes at the point the work a fault was noted for succeeded. A fault that
        is not transient, a missing sudoers rule, is never cleared.

        The count is kept: a source failing every third poll is worth knowing about while it
        is working.
        """
        self.last_fault = None

    def __repr__(self):
        return f"<{self.name}>"
