"""What a source has to implement."""


class Source:
    name = "source"

    # Which groups this source can contribute to, for the config UI's benefit.
    provides = ()

    def __init__(self, config):
        self.config = config
        self.faults = 0
        self.last_fault = None

    @classmethod
    def available(cls):
        """True if this source can run here. Cheap: no sampling, no subprocesses."""
        return False

    def start(self):
        """Called once before the first sample. Spawn helpers here."""

    def stop(self):
        """Called on shutdown. Reap helpers here."""

    def sample(self, frame, dt):
        """Fill in what this source knows.

        `frame` is a dict from model.empty_frame(); mutate it. `dt` is seconds since
        the previous sample, for anything that needs a rate. Only set a field if the
        value is real - leave it absent so a later source can fill it.
        """
        raise NotImplementedError

    def note_fault(self, exc):
        self.faults += 1
        self.last_fault = f"{type(exc).__name__}: {exc}"

    def __repr__(self):
        return f"<{self.name}>"
