"""What a source has to implement."""

from .. import state


class Source:
    name = "source"

    # Which groups this source can contribute to, for the config UI's benefit.
    provides = ()

    # What this source can be told, so the config UI can offer it and the server can
    # store it. Each entry is a dict:
    #
    #   key       the name it arrives under in self.config
    #   label     what the UI calls it
    #   type      "text", "number", "bool" or "choice"
    #   options   the allowed values, for "choice"
    #   default   what it is worth when nothing is stored
    #   hint      a line of explanation, optional
    #
    # A source with no settings declares none and gets no section in the UI. Anything
    # not declared here cannot be set from the UI, only from --extension.
    settings = ()

    # Settings that belong to one page rather than to the source. Same shape as
    # `settings`, and the badge finds them in the page it is handed, so a page can be
    # told which place to show where the source is told which units to show it in.
    #
    # A source wanting to do different work per page - fetching two locations, say -
    # implements `pages(instances)`, which is handed every page of its own kinds
    # whenever the config changes.
    page_settings = ()

    def __init__(self, config):
        self.config = config
        self.faults = 0
        self.last_fault = None
        # What this source worked out, as against what it was told: `store.get`/`store.set`,
        # namespaced by source name and written by the host, so nothing here has to know
        # where the config lives. The one made here keeps everything in memory; the
        # persistent one is in place by the time `start` runs, which is where to read it.
        self.store = state.Store()

    def configure(self, settings):
        """Take settings while running, on every save rather than only on a change.

        The default suits a source that reads `self.config` as it samples. One that
        copies values out in `__init__` has to override this and copy them again.
        """
        self.config.update(settings)

    def pages(self, instances):
        """Take the pages configured for this source's kinds, on every config change.

        `instances` is a list of page dicts, each carrying whatever `page_settings`
        declared. A source that samples the same thing for every page ignores this.
        """

    @classmethod
    def available(cls):
        """True if this source can run here. Cheap: no sampling, no subprocesses."""
        return False

    def start(self):
        """Called once before the first sample. Spawn helpers here, and read `store` here."""

    def stop(self):
        """Called on shutdown. Reap helpers here."""

    def sample(self, frame, dt):
        """Fill in what this source knows.

        `frame` is a dict from model.empty_frame(); mutate it. `dt` is seconds since
        the previous sample, for anything that needs a rate. Only set a field if the
        value is real - leave it absent so a later source can fill it.

        Be prompt: every source shares the collector's thread and the first sample is taken
        while the server is starting up, so anything that waits on a network belongs in a
        thread of its own, started by `start`, with this serving what it last brought back.
        """
        raise NotImplementedError

    def note_fault(self, exc):
        self.faults += 1
        self.last_fault = f"{type(exc).__name__}: {exc}"

    def __repr__(self):
        return f"<{self.name}>"
