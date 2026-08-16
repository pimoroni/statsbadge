"""What an install puts on a badge, and what it removes again."""

import json
import os
import pathlib
import tempfile
import time

from statsbadge import install


def test_a_stale_precompile_is_not_what_gets_installed():
    """Bytecode older than the sources it was built from is refused, and the file named."""
    import hashlib

    app = pathlib.Path(install.app_source_dir())
    digest = hashlib.sha256((app / "look.py").read_bytes()).hexdigest()
    built = pathlib.Path(tempfile.mkdtemp(prefix="statsbadge-mpy-"))
    (built / "look.mpy").write_bytes(b"M\x06\x00\x03")

    def bundled():
        return str(built)

    original = install.packaged_mpy_dir
    install.packaged_mpy_dir = bundled
    try:
        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": digest}}),
                                          encoding="utf-8")
        assert install._stale_modules(built) == []
        source, _note = install.choose_app_source(None, False, None)
        assert source == str(built), source

        (built / "BUILD_INFO").write_text(json.dumps({"sources": {"look.py": "0" * 64}}),
                                          encoding="utf-8")
        assert install._stale_modules(built) == ["look.py"]
        source, note = install.choose_app_source(None, False, None)
        assert source is None, source
        assert "look.py" in note, note
    finally:
        install.packaged_mpy_dir = original


def test_write_secrets_keeps_the_rest_of_the_file():
    """Setting WiFi details leaves the other settings and their comments as they were."""
    import tempfile

    from statsbadge import install

    template = ('WIFI_SSID = ""\n'
                'WIFI_PASSWORD = ""\n'
                'REGION = "eu"  # Options are us, cuba, eu, moldova\n'
                'TIMEZONE = 0  # Offset from GMT as number of hours\n')
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        path = os.path.join(volume, "system", "secrets.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(template)

        assert install.secrets_file(volume) == path
        assert not install.wifi_configured(volume)

        # A backslash and quotes in the password, which a regex replacement writes back out
        # as escapes.
        password = 'p@ss "w0rd"\\'
        install.write_secrets(volume, "Some Network", password, region="us")

        with open(path, encoding="utf-8") as handle:
            after = handle.read()
        values = {}
        exec(compile(after, "secrets.py", "exec"), values)
        assert values["WIFI_SSID"] == "Some Network"
        assert values["WIFI_PASSWORD"] == password
        assert values["REGION"] == "us"
        assert values["TIMEZONE"] == 0, "an untouched setting was lost"
        assert "Options are us" in after, "REGION's comment was dropped"
        assert install.wifi_configured(volume)

        # Writing again replaces, and does not append a second WIFI_SSID.
        install.write_secrets(volume, "Other", "pw")
        with open(path, encoding="utf-8") as handle:
            after = handle.read()
        assert after.count("WIFI_SSID") == 1
        values = {}
        exec(compile(after, "secrets.py", "exec"), values)
        assert values["WIFI_SSID"] == "Other"
        assert values["TIMEZONE"] == 0

        # A key the file lacks is appended.
        install.write_secrets(volume, "Third", "pw", timezone=-7)
        with open(path, encoding="utf-8") as handle:
            values = {}
            exec(compile(handle.read(), "secrets.py", "exec"), values)
        assert values["TIMEZONE"] == -7


def test_an_update_prunes_the_files_it_owns_and_leaves_the_rest():
    """An update removes the files the installer put there, and nothing else."""
    import tempfile

    from statsbadge import install

    with tempfile.TemporaryDirectory() as work:
        source = os.path.join(work, "built")
        os.makedirs(os.path.join(source, "mpy"))
        for name in ("__init__.mpy", "net.mpy", "icon.png", "MPY_VERSION",
                     "BUILD_INFO", ".hidden"):
            with open(os.path.join(source, name), "w", encoding="utf-8") as handle:
                handle.write(name)
        with open(os.path.join(source, "mpy", "stowaway.mpy"), "w", encoding="utf-8") as handle:
            handle.write("not this one either")
        plugin = os.path.join(work, "clockface.py")
        with open(plugin, "w", encoding="utf-8") as handle:
            handle.write("# a badge-side extension module")

        names = dict(install.app_files(source, [("clock", plugin)]))
        assert sorted(names) == ["__init__.mpy", "ext/clockface.py", "icon.png",
                                 "net.mpy"], names

        # A stale .py beside an .mpy wins the import and undoes the precompile.
        target = os.path.join(work, "stats")
        os.makedirs(os.path.join(target, "ext"))
        for name in ("__init__.mpy", "net.mpy", "net.py", "icon.png", "notes.txt"):
            with open(os.path.join(target, name), "w", encoding="utf-8") as handle:
                handle.write("old")
        for name in ("clockface.py", "gone.py"):
            with open(os.path.join(target, "ext", name), "w", encoding="utf-8") as handle:
                handle.write("old")

        removed = install.prune_app(target, set(names))
        assert removed == ["ext/gone.py", "net.py"], removed
        assert os.path.exists(os.path.join(target, "notes.txt")), \
            "pruning took a file the installer does not own"
        assert os.path.exists(os.path.join(target, "net.mpy"))

        installed = {"__init__.mpy": "aaa", "net.mpy": "bbb", "net.py": "ccc",
                     "icon.png": "ddd", "notes.txt": "eee"}
        desired = {"__init__.mpy": "aaa", "net.mpy": "CHANGED", "icon.png": "ddd",
                   "ext/clockface.py": "fff"}
        added, changed, gone = install.app_changes(installed, desired)
        assert added == ["ext/clockface.py"], added
        assert changed == ["net.mpy"], changed
        assert gone == ["net.py"], f"{gone}, and notes.txt must not force a reset"


def test_a_file_that_did_not_write_is_not_left_on_the_badge():
    """A copy that wrote nothing is retried, and one that never completes is an error."""
    # A volume that has only just mounted rejects the first write and leaves an empty file
    # behind rather than raising.
    import shutil as shutil_module

    with tempfile.TemporaryDirectory() as work:
        source = os.path.join(work, "app.py")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("print('hello')\n")
        destination = os.path.join(work, "copy.py")

        real = shutil_module.copy2
        tries = []

        def flaky(src, dst, **kwargs):
            tries.append(dst)
            if len(tries) == 1:
                # What macOS reports when the volume is not ready.
                with open(dst, "w", encoding="utf-8"):
                    pass
                raise OSError(6, "Device not configured")
            return real(src, dst, **kwargs)

        was_wait = install.COPY_WAIT
        install.COPY_WAIT = 0
        shutil_module.copy2 = flaky
        try:
            install._copy(source, destination)
            assert len(tries) == 2, tries
            assert os.path.getsize(destination) == os.path.getsize(source)

            # One that never writes in full is an error, not a short file left behind.
            tries.clear()
            def short(_src, dst, **_kwargs):
                open(dst, "w", encoding="utf-8").close()

            shutil_module.copy2 = short
            try:
                install._copy(source, destination)
            except install.InstallError as exc:
                assert "short" in str(exc), exc
            else:
                raise AssertionError("a short copy was called a good one")
        finally:
            shutil_module.copy2 = real
            install.COPY_WAIT = was_wait


def test_the_installer_and_the_app_name_the_same_extension_directory(badge_constants):
    """The directory the installer writes badge modules into is the one the app adds to
    sys.path."""
    assert badge_constants("app.py")["EXT_DIR"] == install.EXT_DIR, install.EXT_DIR
    # `pages` would be a directory shadowing the app's pages.py on sys.path.
    assert install.EXT_DIR != "pages"


def test_one_writer_owns_the_badge_state_file():
    """net.Config is the only writer inside the app, and the installer merges rather than
    replaces."""
    # Two processes write it, so the path is a literal at each end and only a check holds
    # them together.
    app_dir = pathlib.Path(install.app_source_dir())
    net_source = (app_dir / "net.py").read_text(encoding="utf-8")
    assert f'STATE_FILE = "{install.STATE_FILE}"' in net_source, (
        f"the app does not write {install.STATE_FILE}")

    # Merged, so a page index and a pairing with another host both survive an install.
    assert "data = json.load(open(path))" in (
        pathlib.Path("src/statsbadge/install.py").read_text(encoding="utf-8"))

    app = (app_dir / "app.py").read_text(encoding="utf-8")
    assert "State." not in app, "the app is writing state behind Config's back"
    for owned in ("self.config.page = self.page_index", "self.config.save()"):
        assert owned in app, owned


def test_a_badge_is_called_behind_from_what_it_was_last_seen_holding():
    """The comparison is local, so it can be made with no badge connected."""
    from statsbadge import pushed

    desired = install.desired_hashes()
    missing = sorted(desired)[0]
    with tempfile.TemporaryDirectory() as directory:
        # Nothing recorded is not the same as up to date, and is reported apart from it.
        assert pushed.behind(directory, "badge1") is None

        held = {name: digest for name, digest in desired.items() if name != missing}
        pushed.record(directory, "badge1", held)
        state = pushed.behind(directory, "badge1")
        assert state["behind"] and state["added"] == [missing], state

        pushed.record(directory, "badge1", desired)
        assert pushed.behind(directory, "badge1")["behind"] is False

        # Bytecode and sources hash differently, so a build this package cannot find again
        # is left uncompared.
        pushed.record(directory, "badge2", desired, source="/nowhere/mpy")
        assert pushed.behind(directory, "badge2") is None

        assert pushed.forget(directory, "badge1")
        assert pushed.behind(directory, "badge1") is None


def test_wifi_details_are_kept_unless_replacing_them_was_asked_for():
    """An update must not be a way to lose the network the badge is already on."""
    from statsbadge import push

    said = []
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        with open(os.path.join(volume, "system", "secrets.py"), "w", encoding="utf-8") as handle:
            handle.write('WIFI_SSID = "Already Here"\nWIFI_PASSWORD = "old"\n')

        kept = push._set_wifi({"password": "new"}, volume, "Other", said.append)
        assert kept == "kept", kept
        assert install.wifi_network_on(volume) == "Already Here"

        set_it = push._set_wifi({"password": "new", "force_secrets": True}, volume,
                                "Other", said.append)
        assert set_it == "set", set_it
        assert install.wifi_network_on(volume) == "Other"


def test_a_region_the_firmware_does_not_know_is_refused():
    """A region is checked against the list in secrets.py, and nothing is written if it
    fails."""
    # It sets the radio's country: an unknown one cannot associate, and all the badge can
    # report is that it cannot reach the host.
    template = ('WIFI_SSID = ""\nWIFI_PASSWORD = ""\n'
                'REGION = "eu"  # Options are us, cuba, eu, moldova, nz\n')
    with tempfile.TemporaryDirectory() as volume:
        os.mkdir(os.path.join(volume, "system"))
        with open(os.path.join(volume, "system", "secrets.py"), "w", encoding="utf-8") as handle:
            handle.write(template)

        # The file is the authority, listing what it takes beside the setting.
        assert install.regions_on(volume) == ("us", "cuba", "eu", "moldova", "nz")

        try:
            install.write_secrets(volume, "Some Network", "pw", region="gb")
        except install.InstallError as exc:
            assert "gb" in str(exc) and "moldova" in str(exc), exc
        else:
            raise AssertionError("an unknown region was written to the badge")

        # Nothing was written, region or otherwise.
        assert install.wifi_network_on(volume) is None

        install.write_secrets(volume, "Some Network", "pw", region="EU")
        with open(os.path.join(volume, "system", "secrets.py"), encoding="utf-8") as handle:
            values = {}
            exec(compile(handle.read(), "secrets.py", "exec"), values)
        assert values["REGION"] == "eu", "the region was not written in the firmware's case"

    # A volume with no list falls back to the copy in this package.
    with tempfile.TemporaryDirectory() as bare:
        assert install.regions_on(bare) == install.REGIONS


def test_a_port_that_will_not_open_is_not_called_a_reset():
    """Every command hands the badge back with a reset. One it never reached has none."""
    assert install.hard_reset("/dev/statsbadge-not-a-port", settle=False) is False


def test_the_install_endpoint_runs_one_and_reports_what_it_did(h):
    """An install runs one at a time and reports what happened when it is done."""
    # Driven with a port that is not there: a test must never touch a real badge.
    status, body = h.raw("GET", "/api/install")
    assert status == 200 and body["running"] is False, (status, body)
    assert body["result"] is None and body["log"] == [], body

    status, body = h.raw("POST", "/api/install",
                         json.dumps({"port_dev": "/dev/statsbadge-not-a-port"}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200, (status, body)
    for _ in range(100):
        status, body = h.raw("GET", "/api/install")
        if not body["running"]:
            break
        time.sleep(0.1)
    assert body["result"]["ok"] is False, body
    assert body["result"]["error"], "a failed install said nothing about why"
