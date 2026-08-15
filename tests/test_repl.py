"""Talking to a badge over serial: disk mode, the port, the raw REPL."""

import pathlib
import sys
import time
import tomllib

from statsbadge import install

class FakeBoard:
    """The board's half of the raw REPL, enough to answer what repl.py asks of it.

    Stands in for a serial port, so the framing is checked with no badge on the cable.
    """

    def __init__(self, printed="ok\r\n", failed=""):
        self.printed, self.failed = printed.encode(), failed.encode()
        self.written = b""
        self.scripts = []
        self.out = b""
        self.closed = False
        self.phase = "friendly"
        self._script = b""

    # -- what pyserial offers -----------------------------------------------
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, count=1):
        data, self.out = self.out[:count], self.out[count:]
        return data

    def write(self, data):
        self.written += data
        for byte in data:
            self._take(bytes((byte,)))
        return len(data)

    def close(self):
        self.closed = True

    # -- and what a MicroPython board answers -------------------------------
    def _take(self, byte):
        if self.phase == "raw" and byte != b"\x04":
            self._script += byte
            return
        if byte == b"\x01":
            self.out += b"\r\n" + FakeBoard.PROMPT + b">"
            self.phase = "armed"
        elif byte == b"\x04" and self.phase == "armed":
            self.out += b"\r\n" + b"soft reboot\r\n" + FakeBoard.PROMPT + b">"
            self.phase = "raw"
        elif byte == b"\x04" and self.phase == "raw":
            self.scripts.append(self._script.decode())
            self._script = b""
            self.out += b"OK" + self.printed + b"\x04" + self.failed + b"\x04>"
        elif byte == b"\x02":
            self.phase = "friendly"

    PROMPT = b"raw REPL; CTRL-B to exit\r\n"


def test_the_badge_is_sent_back_out_of_disk_mode():
    """Windows has no eject to send, so the volume is flushed and the badge hard reset."""
    # Elsewhere the eject itself is the SCSI stop the firmware reboots on. A reset pulls
    # the volume out from under Windows, and a write still cached goes with it.
    ran, reset = [], []
    was = (install.platform, install.subprocess, install.hard_reset)
    install.platform = type(sys)("platform")
    install.subprocess = type(sys)("subprocess")
    install.subprocess.run = lambda argv, **_kwargs: ran.append(argv)
    install.hard_reset = lambda port, settle=True: reset.append((port, settle))
    try:
        for system in ("Darwin", "Linux", "Windows"):
            install.platform.system = lambda system=system: system
            ran.clear()
            install.eject("E:\\" if system == "Windows" else "/Volumes/BADGE", "/dev/x")
            words = " ".join(word for argv in ran for word in argv)
            if system == "Windows":
                # The cmdlet takes a drive letter, where the volume is "E:\".
                assert "Write-VolumeCache -DriveLetter E" in words, words
                assert reset == [("/dev/x", False)], reset
            else:
                assert "Write-VolumeCache" not in words, words
                assert not reset, (system, reset)
                assert "eject" in words or "unmount" in words, words

        # A flush that will not take is swallowed: it surfaces as the port failing to come
        # back, and there is nothing the caller can do differently.
        def refuse(*_argv, **_kwargs):
            raise OSError(5, "no such volume")

        install.subprocess.run = refuse
        install.eject("E:\\", "/dev/x")
    finally:
        install.platform, install.subprocess, install.hard_reset = was


class FakePort:
    def __init__(self, device, pid, product, vid=0x2E8A):
        self.device, self.vid, self.pid = device, vid, pid
        self.product, self.manufacturer = product, "Pimoroni"


def test_a_badge_is_found_by_the_product_id_it_declares():
    """A badge is picked out by product id, since every board here shares one vendor id."""
    # 0x1101 is a Tufty 2350, read off a plugged-in one and off MICROPY_HW_USB_PID in the
    # board definition it was built from.
    plugged = [
        FakePort("/dev/probe", 0x000C, "Debug Probe (CMSIS-DAP)"),
        FakePort("/dev/tufty", 0x1101, "Pimoroni Tufty 2350 MicroPython"),
        FakePort("/dev/pico", 0x0005, "Board in FS mode"),
        FakePort("/dev/badger", 0x1100, "Pimoroni Badger 2350 MicroPython"),
        FakePort("/dev/bootsel", 0x0003, "RP2 Boot"),
        FakePort("/dev/arduino", 0x1101, "Something else entirely", vid=0x2341),
    ]
    listing = type(sys)("serial.tools.list_ports")
    listing.comports = lambda: plugged
    tools = type(sys)("serial.tools")
    tools.list_ports = listing
    stub = type(sys)("serial")
    stub.tools = tools
    was = {name: sys.modules.get(name)
           for name in ("serial", "serial.tools", "serial.tools.list_ports")}
    sys.modules.update({"serial": stub, "serial.tools": tools,
                        "serial.tools.list_ports": listing})
    try:
        assert install.find_ports() == ["/dev/badger", "/dev/tufty"], install.find_ports()
    finally:
        for name, module in was.items():
            if module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = module


def test_the_badge_is_talked_to_over_the_raw_repl_and_nothing_else():
    """Running a script and hard resetting are spoken here, with no mpremote and no
    interpreter spawned."""
    # A dependency's console script is off PATH when this is installed as a uv tool.
    from statsbadge import repl

    board = FakeBoard(printed="2e8a01\r\n")
    fault = type("SerialException", (Exception,), {})
    stub = type(sys)("serial")
    stub.SerialException = fault
    stub.Serial = lambda *_args, **_kwargs: board
    was = sys.modules.get("serial")
    sys.modules["serial"] = stub
    try:
        assert repl.run("/dev/fake", "print(badge.uid)") == "2e8a01\r\n"
        # Interrupt, raw mode, then a soft reset for a clean interpreter, or the app is
        # still in memory holding the screen.
        assert board.written.startswith(b"\r\x03\x03\r\x01\x04"), board.written
        assert board.scripts == ["print(badge.uid)"], board.scripts
        # The badge is handed back out of raw mode, which otherwise blanks the screen.
        assert board.written.endswith(b"\r\x02") and board.closed, board.written

        # A script that raised is an exception here, not output the caller has to inspect.
        board = FakeBoard(printed="", failed="Traceback:\r\nImportError: no module named x")
        try:
            repl.run("/dev/fake", "import x")
        except repl.ReplError as exc:
            assert "ImportError" in str(exc), exc
        else:
            raise AssertionError("a traceback came back as ordinary output")

        # A script longer than one chunk still arrives whole.
        board = FakeBoard()
        long_one = "\n".join(f"print({number})" for number in range(200))
        repl.run("/dev/fake", long_one)
        assert board.scripts == [long_one], len(board.scripts)

        # The reset is a hard one, so the badge runs main.py again and does not sit at
        # a prompt. It sleeps first, letting the acknowledgement get out.
        board = FakeBoard()
        repl.reset("/dev/fake")
        assert "machine.reset()" in board.scripts[0], board.scripts
        assert "sleep_ms" in board.scripts[0], board.scripts

        # A board of any other kind is named in the message. Every script here starts by
        # importing badgeware, which on anything else is a traceback naming a module the
        # reader would have to go and look up.
        board = FakeBoard(printed="BOARD Raspberry Pi Pico2 with RP2350\r\n")
        try:
            install.check_board("/dev/fake")
        except install.InstallError as exc:
            assert "Pico2" in str(exc) and install.BOARD in str(exc), exc
        else:
            raise AssertionError("a board with no badgeware on it was accepted")
        board = FakeBoard(printed=f"BOARD Pimoroni {install.BOARD} with RP2350\r\n")
        assert install.BOARD in install.check_board("/dev/fake")

        # Somebody else holding the port is answer enough, the fix being theirs.
        def held(*_args, **_kwargs):
            raise fault("Could not exclusively lock port /dev/fake")

        stub.Serial = held
        try:
            repl.run("/dev/fake", "print(1)")
        except repl.Busy:
            pass
        else:
            raise AssertionError("a held port is not reported as busy")
        # install.py says whose problem that is, in the words of the thing to close.
        try:
            install._exec("/dev/fake", "print(1)")
        except install.PortBusy as exc:
            assert "busy" in str(exc) and "Thonny" in str(exc), exc
        else:
            raise AssertionError("a held port is not reported as busy")

        # A port that failed to open is skipped on the way out. Every command hard resets
        # there, and a reset that cannot happen would spend the enumeration timeout before
        # announcing one.
        started = time.monotonic()
        assert install.hard_reset("/dev/fake") is False
        assert time.monotonic() - started < 2.0, "waited for a reset that never happened"
    finally:
        if was is None:
            del sys.modules["serial"]
        else:
            sys.modules["serial"] = was

    # mpremote has gone from the runtime, and pyserial is a plain dependency.
    assert "mpremote" not in pathlib.Path("src/statsbadge/install.py").read_text(encoding="utf-8")
    with open("pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    assert any(name.startswith("pyserial") for name in project["dependencies"]), project
    assert "install" not in project.get("optional-dependencies", {}), (
        "an extra that no longer adds anything")
