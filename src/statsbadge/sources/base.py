"""What a source has to implement."""


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

    def __init__(self, config):
        self.config = config
        self.faults = 0
        self.last_fault = None

    def configure(self, settings):
        """Take settings while running, on every save rather than only on a change.

        The default suits a source that reads `self.config` as it samples. One that
        copies values out in `__init__` has to override this and copy them again.
        """
        self.config.update(settings)

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
