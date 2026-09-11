"""Talk to the badge over its serial REPL."""

import time

# A USB CDC port ignores the line speed, but pyserial takes a number.
BAUD = 115200
# A script is written in chunks with a pause between them, which keeps it inside the
# board's USB buffer without raw-paste's flow control.
CHUNK = 256
CHUNK_PAUSE = 0.01
POLL = 0.01

INTERRUPT = b"\r\x03\x03"   # ctrl-C twice: stop whatever the badge is running
RAW = b"\r\x01"             # ctrl-A: raw REPL, one script at a time and no echo
FRIENDLY = b"\r\x02"        # ctrl-B: back to the prompt a person would use
END = b"\x04"               # ctrl-D: soft reset, and 'that is the whole script' in raw mode

RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n"
REBOOT = b"soft reboot\r\n"
TAKEN = b"OK"


class ReplError(Exception):
    """The badge did not answer, or answered with a traceback."""


class NotOpened(ReplError):
    """The port would not open, so nothing on the badge was interrupted."""


class Busy(NotOpened):
    """Something else has the port open."""


class Repl:
    """One connection to the badge's raw REPL."""

    def __init__(self, port, timeout=30):
        self.port = port
        self.timeout = timeout
        self.serial = None
        self.buffer = b""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_exception):
        self.close()
        return False

    def open(self):
        # Imported here rather than at the top: nothing but talking to a badge needs
        # pyserial, and the server is the thing that runs all day.
        import serial

        try:
            # Non-blocking, so every read is bounded by a deadline of its own and a badge
            # that stops answering is a timeout rather than a hang.
            self.serial = serial.Serial(self.port, BAUD, timeout=0, exclusive=True)
        except (serial.SerialException, OSError) as exc:
            detail = str(exc).lower()
            if any(word in detail for word in ("busy", "lock", "denied", "in use")):
                raise Busy(f"{self.port} is already open") from exc
            raise NotOpened(f"could not open {self.port}: {exc}") from exc
        try:
            self._enter_raw()
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self.serial is None:
            return
        try:
            self.serial.write(FRIENDLY)
            self.serial.close()
        except (OSError, ValueError):
            # A reset takes the port with it, so this is the ordinary end of a reset.
            pass
        self.serial = None

    def exec(self, script, timeout=None):
        """Run a script on the badge and return what it printed."""
        deadline = self._deadline(timeout)
        self._send(script, deadline)
        printed = self._until(END, deadline, "waiting for the script to finish")
        failed = self._until(END, deadline, "waiting for the error output")
        if failed.strip():
            raise ReplError(failed.decode(errors="replace").strip())
        return printed.decode(errors="replace")

    def reset(self, timeout=None):
        """Hard reset the badge, which is the only way back to `main.py`."""
        self._send("import time, machine; time.sleep_ms(100); machine.reset()",
                   self._deadline(timeout))

    def _enter_raw(self):
        """Interrupt whatever is running, then soft reset into a clean interpreter."""
        deadline = self._deadline(None)
        self.serial.write(INTERRUPT)
        # Whatever the interrupt printed, plus anything the app had in flight.
        while self.serial.in_waiting:
            self.serial.read(self.serial.in_waiting)
        self.buffer = b""
        self.serial.write(RAW)
        self._until(RAW_PROMPT + b">", deadline, "waiting for a raw prompt")
        self.serial.write(END)
        self._until(REBOOT, deadline, "waiting for the soft reset")
        self._until(RAW_PROMPT, deadline, "waiting for a raw prompt after the reset")

    def _send(self, script, deadline):
        """Hand over a script and wait to hear that the badge took it."""
        self._until(b">", deadline, "waiting for a raw prompt")
        payload = script.encode() if isinstance(script, str) else script
        for start in range(0, len(payload), CHUNK):
            self.serial.write(payload[start:start + CHUNK])
            time.sleep(CHUNK_PAUSE)
        self.serial.write(END)
        taken = self._read(len(TAKEN), deadline, "waiting for the badge to take the script")
        if taken != TAKEN:
            raise ReplError(f"the badge would not run it: {taken!r}")

    def _deadline(self, timeout):
        return time.monotonic() + (self.timeout if timeout is None else timeout)

    def _read(self, count, deadline, what):
        while len(self.buffer) < count:
            self._fill(deadline, what)
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _until(self, ending, deadline, what):
        """Read up to `ending` and return what came before it."""
        while True:
            found = self.buffer.find(ending)
            if found >= 0:
                data, self.buffer = self.buffer[:found], self.buffer[found + len(ending):]
                return data
            self._fill(deadline, what)

    def _fill(self, deadline, what):
        chunk = self.serial.read(self.serial.in_waiting or 1)
        if chunk:
            self.buffer += chunk
            return
        self._wait(deadline, what, self.buffer)

    def _wait(self, deadline, what, so_far):
        if time.monotonic() > deadline:
            tail = so_far[-120:].decode(errors="replace").strip()
            raise ReplError(f"the badge stopped answering {what}"
                            + (f": {tail!r}" if tail else ""))
        time.sleep(POLL)


def run(port, script, timeout=30):
    """Run a script on the badge and return what it printed."""
    with Repl(port, timeout=timeout) as badge:
        return badge.exec(script)


def reset(port, timeout=10):
    """Hard reset the badge."""
    with Repl(port, timeout=timeout) as badge:
        badge.reset()
