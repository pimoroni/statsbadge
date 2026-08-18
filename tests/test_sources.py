"""What a source measures, caches, and reports when it cannot."""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from statsbadge import extensions, install, model


def test_a_source_keeps_what_it_worked_out():
    """A store holds what a source worked out: one file per source, oldest keys dropped
    at the cap."""
    from statsbadge import state

    directory = tempfile.mkdtemp(prefix="statsbadge-state-")
    store = state.for_source(directory, "clock")
    assert store.path == os.path.join(directory, "clock.json")
    store.set("geocoded", {"sheffield": [53.38, -1.47, "Sheffield, GB"]})
    store.update({"kept": 1, "dropped": 2})
    store.forget("dropped")

    again = state.for_source(directory, "clock")
    assert again.get("geocoded")["sheffield"][2] == "Sheffield, GB"
    assert again.get("kept") == 1 and again.get("dropped") is None
    assert again.all() == store.all()
    # A different source cannot see it, or read it by accident.
    assert state.for_source(directory, "other").all() == {}

    # Nowhere to write is still a store: `install` loads every extension only to read what
    # it ships, and can drop what it learns.
    memory = state.for_source(None, "clock")
    memory.set("geocoded", {})
    assert memory.get("geocoded") == {} and memory.path is None

    # Refused before the store changes, so what is in memory always matches what is on disk.
    try:
        store.set("no", object())
    except TypeError:
        pass
    else:
        raise AssertionError("stored something that cannot be written")
    assert "no" not in state.for_source(directory, "clock").all()
    assert store.get("no") is None, "the store kept a value the file did not"

    # A cache keyed by something a user types grows by one on every typo.
    for index in range(state.MAX_KEYS + 8):
        store.set(f"key{index}", index)
    assert len(store.all()) == state.MAX_KEYS
    assert store.get(f"key{state.MAX_KEYS + 7}") == state.MAX_KEYS + 7, "dropped the newest"

    # Setting a key again keeps it, so the one dropped at the cap is the longest untouched.
    kept, dropped = f"key{state.MAX_KEYS + 4}", f"key{state.MAX_KEYS + 5}"
    store.set(kept, "still wanted")
    for index in range(state.MAX_KEYS - 1):
        store.set(f"later{index}", index)
    assert store.get(kept) == "still wanted", "evicted a key that was being used"
    assert store.get(dropped) is None, "the cap is not being reached"

    # A name that would be a bad filename is still made into one: this ends up as a path.
    assert state.for_source(directory, "../etc/passwd").path == os.path.join(
        directory, "___etc_passwd.json")

    # Every source has one from the start, in memory until the host hands one over.
    from statsbadge.sources import base

    class Nothing(base.Source):
        def sample(self, frame, dt):
            pass

    assert Nothing({}).store.path is None
    shutil.rmtree(directory, ignore_errors=True)


def test_a_slow_lookup_does_not_hold_up_a_frame():
    """A weather lookup runs off the collector's thread, so sampling returns before it
    lands."""
    clock = pytest.importorskip("statsbadge_clock")

    source = clock.Clock({"place": "Sheffield"})
    asked = []

    def slow(_where, **_named):
        asked.append(time.monotonic())
        time.sleep(0.4)
        return {"temp": 11.0, "place": "Sheffield", "utc_offset": 0}

    source._fetch = slow
    source._geocode = lambda _place: (53.38, -1.47, "Sheffield, GB")
    source.pages([{"id": "clock1", "kind": "clockface", "place": "Sheffield"}])
    source.start()
    try:
        frame = {}
        started = time.monotonic()
        source.sample(frame, 1.0)
        assert time.monotonic() - started < 0.1, "sampling waited on the fetch"
        assert frame["clock"]["time"], "no clock in the frame"
        # What it brings back does reach a frame, once it has.
        for _ in range(40):
            time.sleep(0.1)
            source.sample(frame, 1.0)
            if frame["weather"] and frame["places"]:
                break
        assert frame["weather"]["temp"] == 11.0, frame["weather"]
        assert frame["places"]["clock1"]["temp"] == 11.0, frame["places"]
    finally:
        source.stop()
    assert asked, "nothing was ever fetched"

    # A refused lookup waits out the retry timer and is then tried again.
    refused = clock.Clock({"place": "Sheffield"})
    tries = []

    def failing(place):
        tries.append(place)
        raise OSError("rate limited")

    refused._geocode = failing
    assert refused._where() is None and refused.faults == 1
    assert refused._where() is None and len(tries) == 1, "hammered a rate limited geocoder"
    refused._retry_at = 0.0
    assert refused._where() is None and len(tries) == 2, "never tried again"

    # A town stays put, so coordinates in the store outlive a launch and the badge comes up
    # knowing where it is looking even while the geocoder is refusing everyone.
    from statsbadge import state

    directory = tempfile.mkdtemp(prefix="statsbadge-clock-")
    kept = clock.Clock({"place": "Sheffield"})
    kept.store = state.for_source(directory, "clock")
    calls = []
    kept._fetch = lambda where, **_named: calls.append(where) or {"temp": 9.0}
    real_urlopen = clock.urllib.request.urlopen
    clock.urllib.request.urlopen = lambda *_args, **_named: (_ for _ in ()).throw(
        AssertionError("asked the geocoder for a place it had already resolved"))
    try:
        state.for_source(directory, "clock").set(
            clock.GEOCODED, {"sheffield": [53.38, -1.47, "Sheffield, GB"]})
        kept.store = state.for_source(directory, "clock")
        assert kept._where() == (53.38, -1.47, "Sheffield, GB")
    finally:
        clock.urllib.request.urlopen = real_urlopen

    # The host settles where that file goes, one per extension name.
    loaded = extensions.load({"extensions": {}}, directory)
    for source in loaded:
        assert source.store.path == os.path.join(directory, f"{source.name}.json"), (
            source.name, source.store.path)
    assert any(source.name == "clock" for source in loaded), "the clock was not loaded"
    shutil.rmtree(directory, ignore_errors=True)


def test_a_weather_reading_carries_its_units_and_a_symbol():
    """Units travel with the readings, and every condition the table can produce has an
    icon."""
    clock = pytest.importorskip("statsbadge_clock")

    assert "wind_units" in [setting["key"] for setting in clock.Clock.settings]

    source = clock.Clock({"units": "fahrenheit", "wind_units": "mph"})
    assert (source.units, source.wind_units) == ("fahrenheit", "mph")
    # An unknown unit would be passed straight to Open-Meteo, which rejects the request.
    assert clock.Clock({"wind_units": "furlongs"}).wind_units == "kmh"
    assert clock.Clock({}).wind_units == "kmh"

    for condition in set(clock.CONDITIONS.values()):
        assert condition in clock.ICONS, f"no icon for {condition!r}"
    letters = set(clock.ICONS.values()) | set(clock.NIGHT_ICONS.values())
    corpus = os.path.join(os.path.dirname(clock.__file__), "..", "..", "icons.txt")
    if os.path.exists(corpus):
        packed = set()
        with open(corpus, encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#", 1)[0].split()
                if len(line) == 3:
                    packed.add(line[2])
        missing = letters - packed
        assert not missing, f"icons.txt does not pack {sorted(missing)}"


def test_the_reported_disk_is_the_one_with_your_files_on():
    """On macOS "/" is a sealed system volume, and reporting it reads far too empty."""
    import platform

    import psutil

    from statsbadge.sources.portable import default_disk

    path = default_disk()
    if platform.system() == "Darwin":
        assert path == "/System/Volumes/Data", path
        # Both volumes share the container, so free space matches and only `used`
        # differs: the sealed root claims a fraction of what is actually in use.
        root = psutil.disk_usage("/")
        data = psutil.disk_usage(path)
        assert data.used > root.used
        assert data.percent > root.percent
    else:
        assert path == "/"


def test_a_rate_is_scaled_by_what_it_has_reached():
    """A throughput ring is scaled by the peak that rate has reached, and the peak decays."""
    from statsbadge.collect import PEAK_FLOOR, PEAK_HALF_LIFE_S, Collector

    def run(rates, interval):
        """The peak after each rate in turn, sampled `interval` seconds apart."""
        collector = Collector(interval=interval, config={"sources": []})
        for rate in rates:
            collector._push_peaks({"net": {"down_bps": rate}}, interval)  # noqa: SLF001
        return collector._peaks["net.down_bps"]  # noqa: SLF001

    peak = run([40e6] * 5, 1.0)
    assert peak == 40e6
    # A trickle afterwards is a small part of the ring, not an eighth of a ring that was
    # already full.
    assert (1.5e6 / peak) < 0.05

    # A peak halves in the same wall-clock time whatever the sample interval is set to.
    halved = run([40e6, *[1.0] * int(PEAK_HALF_LIFE_S)], 1.0)
    slower = run([40e6, *[1.0] * int(PEAK_HALF_LIFE_S / 4)], 4.0)
    assert abs(halved - 20e6) < 1e5, halved
    assert abs(halved - slower) < 1e5, (halved, slower)

    # The floor keeps a quiet link from scaling a trickle up to a full ring.
    assert run([1.0] * 5, 1.0) == PEAK_FLOOR


def test_everything_that_walks_a_frame_steps_over_the_same_scalars(h, ui):
    """A frame carries scalars beside the groups of readings, and every walker skips the
    same list."""
    # app.js keeps a copy, JavaScript being unable to import this one, so it is held to it
    # here.
    from statsbadge import collect

    _status, frame = h.raw("GET", "/api/stats")
    loose = {key for key, value in frame.items() if not isinstance(value, (dict, list))}
    assert loose == set(collect.FRAME_SCALARS), loose

    source = pathlib.Path(install.__file__).parent / "__main__.py"
    assert "collect.FRAME_SCALARS" in source.read_text(encoding="utf-8"), \
        "probe keeps a second list"

    script = ui.script
    named = re.search(r"const FRAME_SCALARS = \[(.*?)\]", script).group(1)
    assert [word.strip().strip('"') for word in named.split(",")] == list(collect.FRAME_SCALARS)


def test_a_source_that_recovered_stops_being_reported_as_broken(h, ui):
    """A fault keeps its count and drops its reason as soon as the source works again."""
    from statsbadge.sources import base

    source = base.Source({})
    source.note_fault(urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", 503,
                                             "Service Unavailable", {}, None))
    # The message names what happened and where, without repeating the exception's name.
    assert source.last_fault == "HTTP 503 Service Unavailable from api.open-meteo.com", \
        source.last_fault
    source.note_ok()
    assert source.last_fault is None and source.faults == 1, vars(source)

    # The failures sources actually hit, in the words of the thing that failed.
    said = {}
    for exc in (urllib.error.URLError("_ssl.c:1063: The handshake operation timed out"),
                subprocess.TimeoutExpired(["ioreg", "-r", "-c", "IOAccelerator"], 4),
                ValueError("something we did not expect")):
        said[type(exc).__name__] = base.readable(exc)
    assert said["URLError"] == "the connection timed out", said
    assert said["TimeoutExpired"] == "ioreg did not finish inside 4s", said
    # Anything unrecognised keeps its type, which is the clue to what went wrong.
    assert said["ValueError"] == "ValueError: something we did not expect", said

    # The API reports both, so the UI can show "failing" and "recovered".
    _status, caps = h.raw("GET", "/api/capabilities")
    assert caps["sources"], caps
    for entry in caps["sources"]:
        assert set(entry) >= {"name", "provides", "faults", "last_fault"}, entry

    # Every source that expects to fail clears it, or the reason sticks for the session.
    for path in ["src/statsbadge/sources/macos.py", "src/statsbadge/sources/linux.py",
                 "src/statsbadge/sources/windows.py",
                 *sorted(str(p) for p in pathlib.Path("extensions").glob("*/src/*/__init__.py"))]:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        if "note_fault" not in text:
            continue
        assert "note_ok" in text, f"{path} records faults and never clears one"
    # The UI puts the reason under the name, keeping both.
    script = ui.script
    assert 'source.last_fault ? "faulty" : null' in script, \
        "a recovered source still shows as broken"
    assert 'provides.join(", ")' in ui.function("renderSources"), (
        "the UI no longer says what a source provides")


def test_the_cpu_temperature_linux_reports_is_the_hottest_one():
    """A labelled sensor outranks a hotter unlabelled one, and unlabelled sets report their
    hottest."""
    import types

    from statsbadge.sources import linux

    def entry(label, current):
        return types.SimpleNamespace(label=label, current=current, high=None, critical=None)

    source = linux.LinuxHwmon({})
    # psutil only grows sensors_temperatures on Linux, so on any other host it is added.
    missing = object()
    was = getattr(linux.psutil, "sensors_temperatures", missing)
    try:
        linux.psutil.sensors_temperatures = lambda: {
            "coretemp": [entry("", 41.0), entry("", 78.4), entry("", 52.0)]}
        assert source._cpu_temp() == 78.4, "reported an idle core"  # noqa: SLF001

        # k10temp offers Tctl as a control value that runs above the die it sits on.
        linux.psutil.sensors_temperatures = lambda: {
            "k10temp": [entry("Tdie", 60.0), entry("Tctl", 91.0)]}
        assert source._cpu_temp() == 91.0, "Tctl outranks Tdie"  # noqa: SLF001
        linux.psutil.sensors_temperatures = lambda: {
            "coretemp": [entry("Package id 0", 60.0), entry("", 91.0)]}
        assert source._cpu_temp() == 60.0, "an unlabelled core beat the package"  # noqa: SLF001

        # A chip that means nothing about the CPU is no reading, and not the hottest drive.
        linux.psutil.sensors_temperatures = lambda: {"nvme": [entry("Composite", 44.0)]}
        assert source._cpu_temp() is None  # noqa: SLF001
    finally:
        if was is missing:
            del linux.psutil.sensors_temperatures
        else:
            linux.psutil.sensors_temperatures = was


def test_the_help_tab_is_told_what_this_platform_needs(h):
    """The help block names what this platform needs before it can read its sensors."""
    status, block = h.raw("GET", "/api/help")
    assert status == 200, (status, block)
    assert block["platform"] in ("Darwin", "Windows", "Linux"), block
    assert block["sources"], "reading nothing at all"

    if block["platform"] == "Darwin":
        # The rule names this user and the path to the command, since sudoers matches on
        # the whole line.
        assert "NOPASSWD:" in block["powermetrics"]["sudoers"]
    if block["platform"] == "Windows":
        assert block["lhm"]["url"].startswith("http")


def test_core_voltages_come_back_as_a_bar_each():
    """A voltage per rail arrives as a labelled list, scaled by the highest CPU rail seen."""
    from statsbadge.sources import windows

    # Shaped like a real reply, down to the sensor labels LHM uses.
    tree = {"Text": "Sensor", "Children": [{
        "Text": "DESKTOP-1", "ImageURL": "images_icon/computer.png",
        "Children": [
            {"Text": "Intel Core i9-10980HK", "ImageURL": "images_icon/cpu.png",
             "Children": [
                 {"Text": "Voltages", "Children": [
                     {"Text": "CPU Core #1", "Value": "1.325 V", "Max": "1.456 V"},
                     {"Text": "CPU Core #2", "Value": "1.294 V", "Max": "1.449 V"},
                     {"Text": "CPU SoC", "Value": "1.100 V", "Max": "1.100 V"},
                 ]},
                 {"Text": "Temperatures", "Children": [
                     {"Text": "Core Average", "Value": "78.1 °C"},
                     {"Text": "CPU Package", "Value": "91.0 °C"},
                 ]},
             ]},
            {"Text": "Alienware m15", "ImageURL": "images_icon/mainboard.png",
             "Children": [
                 # The board's rails are voltages too, and no scale for a core.
                 {"Text": "Voltages", "Children": [
                     {"Text": "Voltage #1", "Value": "11.821 V", "Max": "11.821 V"},
                 ]},
             ]},
            {"Text": "NVIDIA GeForce RTX 2080", "ImageURL": "images_icon/nvidia.png",
             "Children": [
                 {"Text": "Temperatures", "Children": [
                     {"Text": "GPU Core", "Value": "59.0 °C"},
                 ]},
                 {"Text": "Data", "Children": [
                     {"Text": "GPU Memory Used", "Value": "236.0 MB"},
                     {"Text": "GPU Memory Total", "Value": "8192.0 MB"},
                 ]},
             ]},
        ],
    }]}

    source = windows.LibreHardwareMonitor({})
    was = windows._fetch
    frame = model.empty_frame()
    try:
        windows._fetch = lambda _url, **_kwargs: tree
        source.sample(frame, 1.0)
    finally:
        windows._fetch = was

    assert frame["cpu"]["volts"] == [1.325, 1.294, 1.1], frame["cpu"]["volts"]
    # The full scale is the highest LHM has seen a CPU rail reach, and not the board's 12V.
    assert frame["peaks"]["cpu.volts"] == 1.456, frame.get("peaks")
    # Short enough for a lane, read off the field beside the readings.
    assert frame["cpu"]["volts_names"] == ["Core #1", "Core #2", "SoC"], \
        frame["cpu"]["volts_names"]

    # The package figure is the CPU temperature by convention.
    assert frame["cpu"]["temp"] == 91.0, frame["cpu"]

    # VRAM comes as two figures in MB, so the percentage is worked out here.
    assert frame["gpu"][0]["mem_used_mb"] == 236
    assert frame["gpu"][0]["mem_pct"] == 2.9

    # The labels are not a reading, so nothing offers them to a dial.
    assert "volts" in model.LIST_FIELDS
    assert "volts_names" not in model.GROUPS["cpu"]


def test_a_source_that_can_run_now_is_taken_up_without_a_restart():
    """A source that becomes available is built by `reconfigure`, without a restart."""
    # `available()` is called once at startup, and LibreHardwareMonitor answers no while its
    # server is down or on another port.
    from statsbadge import collect

    class Late:
        name = "late"

        def __init__(self, config):
            self.config = config
            self.started = 0

        @classmethod
        def available(cls, config=None):
            return bool((config or {}).get("lhm_url"))

        def start(self):
            self.started += 1

        def stop(self):
            pass

    was = collect.discover
    collect.discover = lambda config=None: (
        [Late(config)] if Late.available(config) else [])
    try:
        collector = collect.Collector(interval=5.0)      # not started: no sampling wanted
        assert collector.sources == [], "it was there before it could be"

        collector.config["lhm_url"] = "http://10.0.0.5:9000/data.json"
        assert collector.reconfigure() == ["late"]
        assert [type(source) for source in collector.sources] == [Late]
        # Not started, since nothing is sampling: `Collector.start` does that for all.
        assert collector.sources[0].started == 0

        # Reconfigured again, it is not built twice.
        assert collector.reconfigure() == []
        assert len(collector.sources) == 1
    finally:
        collect.discover = was


def test_a_sensor_url_typed_in_the_browser_is_kept_and_read():
    """A sensor URL reaches the running source at once, and is stored for the next start."""
    from statsbadge import server as server_module

    with tempfile.TemporaryDirectory() as directory:
        service = server_module.Service(directory, interval=5.0)
        try:
            told = []

            class Fake:
                name = "librehardwaremonitor"

                def reconfigure(self, config):
                    told.append(config.get("lhm_url"))

            service.collector.sources = [Fake()]
            service.set_host_settings({"lhm_url": " http://10.0.0.5:9000/data.json "})
            assert told == ["http://10.0.0.5:9000/data.json"], told

            # Under a name of its own, so a layout save cannot tread on it.
            stored = service.config.snapshot()["settings"][server_module.HOST]
            assert stored == {"lhm_url": "http://10.0.0.5:9000/data.json"}, stored
            again = server_module.Service(directory, interval=5.0)
            assert again.collector.config["lhm_url"] == stored["lhm_url"]
            again.stop()

            # Nothing else a browser sends is stored, though the block reaches the sources.
            service.set_host_settings({"powermetrics": True, "lhm_url": ""})
            assert "powermetrics" not in service.config.snapshot()["settings"][
                server_module.HOST]
        finally:
            service.stop()


def test_a_location_typed_in_the_browser_reaches_every_source():
    """One location per install, so an extension wanting one needs no settings of its own.

    Stored under the host's name beside the sensor URL, and handed to the sources on the
    save rather than at the next start.
    """
    from statsbadge import server as server_module

    with tempfile.TemporaryDirectory() as directory:
        service = server_module.Service(directory, interval=5.0)
        try:
            service.set_host_settings({"place": " Sheffield, GB ", "latitude": "",
                                       "longitude": ""})
            assert service.host_settings()["place"] == "Sheffield, GB"
            stored = service.config.snapshot()["settings"][server_module.HOST]
            assert stored == {"place": "Sheffield, GB", "latitude": None,
                              "longitude": None}, stored
            for source in service.collector.sources + service.collector.extensions:
                assert source.home == {"place": "Sheffield, GB"}, source.name

            # A browser does not enforce min and max on a typed value, so the host clamps.
            service.set_host_settings({"latitude": 120, "longitude": -400})
            assert service.collector.config["latitude"] == 90.0
            assert service.collector.config["longitude"] == -180.0

            # A cache of its own, so a town is looked up once and not once per extension.
            assert service.geocoder.store.path == os.path.join(directory, "geocode.json")

            again = server_module.Service(directory, interval=5.0)
            assert again.collector.config["place"] == "Sheffield, GB"
            again.stop()
        finally:
            service.stop()


def test_powermetrics_is_tried_and_says_nothing_when_refused():
    """powermetrics is tried under `sudo -n`, so a Mac without the rule declines silently."""
    from statsbadge.sources import macos

    tried = macos.MacPowermetrics({})
    assert tried._enabled is True, "not even tried"
    assert tried._asked is False, "a default reports as having been asked for"

    asked = macos.MacPowermetrics({"powermetrics": True})
    assert (asked._enabled, asked._asked) == (True, True)

    off = macos.MacPowermetrics({"powermetrics": False})
    assert off._enabled is False, "--no-powermetrics still ran it"

    # The rule names one command and this user, which sudoers matches on.
    line = macos.sudoers_line()
    assert "NOPASSWD:" in line and "ALL=(root)" in line, line
    assert macos.powermetrics_argv()[0] in line, line

    # A comma separates commands in a rule, so an unescaped one in `--samplers
    # cpu_power,gpu_power,thermal` reads as three of them and visudo rejects the second
    # as not a path. Checked by visudo itself where there is one.
    assert "\\," in line, f"visudo will not take this: {line}"
    assert "cpu_power\\,gpu_power\\,thermal" in line, line

    if not shutil.which("visudo"):
        pytest.skip("no visudo on this host to check the rule with")
    with tempfile.TemporaryDirectory() as work:
        rule = os.path.join(work, "statsbadge")
        with open(rule, "w", encoding="utf-8") as handle:
            handle.write(line + "\n")
        done = subprocess.run(["visudo", "-c", "-f", rule], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
