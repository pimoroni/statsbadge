"""A file to print into when there is no terminal.

statsbadge prints rather than logs. Under pythonw.exe, or inside a macOS .app bundle,
sys.stdout is None and every print in the package raises AttributeError. Replacing the
streams fixes that without touching the prints.

serve does not call this.
"""

import io
import logging
import logging.handlers
import os
import sys
import threading
import traceback

MAX_BYTES = 1 << 20
BACKUPS = 2


def path(config_dir, name="tray"):
    return os.path.join(config_dir, "logs", f"{name}.log")


def start(config_dir, name="tray"):
    """Point stdout and stderr at the log file. Returns where that is."""
    target = path(config_dir, name)
    os.makedirs(os.path.dirname(target), exist_ok=True)

    handler = _Handler(target, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("statsbadge.transcript")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    # Propagating reaches logging's last resort handler, which writes to stderr.
    logger.propagate = False

    # One buffer each, or a half-written stdout line and a stderr line splice together.
    sys.stdout = _Stream(logger)
    sys.stderr = _Stream(logger)
    # Under pythonw the originals are None too.
    sys.__stdout__ = sys.stdout
    sys.__stderr__ = sys.stderr

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return target


class _Handler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):
        """Swallow it. The default reports to sys.stderr, which is this handler."""


class _Stream:
    """A file-like object that turns writes into whole log lines."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger):
        self._logger = logger
        self._parts = []
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            self._parts.append(text)
            if "\n" in text:
                self._drain()
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        with self._lock:
            self._drain(flushing=True)

    def close(self):
        self.flush()

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("fileno")

    def _drain(self, flushing=False):
        """print() writes its newline separately, so a fragment waits for one."""
        held = "".join(self._parts)
        complete, sep, tail = held.rpartition("\n")
        if flushing and tail:
            complete, sep, tail = f"{complete}{sep}{tail}", "\n", ""
        self._parts = [tail] if tail else []
        # On sep, not on complete: a bare newline is a blank line worth keeping.
        if sep:
            for line in complete.split("\n"):
                self._logger.info(line)


def _excepthook(kind, value, tb):
    _report(traceback.format_exception(kind, value, tb))


def _thread_excepthook(args):
    """Catches a thread dying. The HTTP, beacon and tray threads report nothing."""
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "unknown"
    _report([f"statsbadge: thread {name} died\n",
             *traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)])


def _report(lines):
    stream = sys.stderr
    if stream is None:
        return
    for line in lines:
        stream.write(line)
    stream.flush()
