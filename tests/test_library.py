"""Installing, upgrading and generation-naming the extension library."""

import contextlib
import importlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import tomllib

from statsbadge import extensions, library


def test_a_plugin_wanting_a_newer_statsbadge_is_explained():
    """A resolver failure comes out as the two versions and the name to blame."""
    # uv's last line is "your requirements are unsatisfiable", which is true of every
    # resolution failure, and it wraps its prose so the versions span the fold.
    from statsbadge import tooling

    said = (
        "  × No solution found when resolving dependencies:\n"
        "  ╰─▶ Because all versions of statsbadge-cloudflare depend on\n"
        "      statsbadge>=1.1.0 and you require statsbadge==1.0.0, we can conclude\n"
        "      that your requirements and all versions of statsbadge-cloudflare are\n"
        "      incompatible.\n"
        "      And because you require statsbadge-cloudflare, we can conclude that your\n"
        "      requirements are unsatisfiable.\n")
    line = tooling.explain(said)
    assert line == ("statsbadge-cloudflare needs statsbadge>=1.1.0, and this is "
                    "statsbadge==1.0.0"), line
    # A build installs every entry, so the name it trips over need not be the one just
    # asked for.
    assert tooling.blamed(line) == "statsbadge-cloudflare", tooling.blamed(line)
    assert tooling.blamed(said) == "statsbadge-cloudflare"

    # uv reports an unknown name as a resolver error, which otherwise reads as a clash.
    assert tooling.explain("error: Because nosuchthing was not found in the package "
                           "registry and you require nosuchthing, we can conclude that "
                           "your requirements are unsatisfiable.") == (
        "no such package: nosuchthing")

    # The build refuses before it promotes anything.
    with tempfile.TemporaryDirectory() as directory:
        where, why = library.build(directory, ["statsbadge-quakes>=99"])
        assert where is None and why, (where, why)
        assert library.generations(directory) == [], "a failed build was promoted"


def test_an_extension_using_a_new_feature_says_which_statsbadge_it_needs():
    """An extension declaring `groups` or `series` pins a statsbadge floor."""
    # An older collector reads neither and says nothing about it, so the failure is a
    # missing group and a slow one polled every second.
    marks = ("groups = {", "def series(self)")
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        source = "\n".join(path.read_text(encoding="utf-8")
                           for path in sorted(directory.rglob("src/**/__init__.py")))
        if not any(mark in source for mark in marks):
            continue
        with open(pyproject, "rb") as handle:
            requires = tomllib.load(handle)["project"]["dependencies"]
        pinned = [need for need in requires if need.startswith("statsbadge")]
        assert pinned and ">=" in pinned[0], (
            f"{directory.name} declares a group or a series against an unpinned "
            f"statsbadge: {requires}")


def test_the_list_is_what_every_build_is_made_from():
    """One extension can be named three ways, so `extensions.txt` is compared by short
    name."""
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-list-")
    try:
        # A short name becomes the package; anything already specific is left alone.
        assert tooling.as_requirement("clock") == "statsbadge-clock"
        for given in ("statsbadge-clock", "./extensions/statsbadge-iss", "statsbadge-iss>=0.2",
                      "git+https://example.invalid/x.git"):
            assert tooling.as_requirement(given) == given, given
        for requirement, short in (("statsbadge-clock", "clock"),
                                   ("/src/sb/extensions/statsbadge-iss", "iss"),
                                   ("statsbadge-quakes>=0.2", "quakes")):
            assert tooling.short_name(requirement) == short, requirement
        assert tooling.names(["/src/sb/extensions/statsbadge-clock"]) == {"clock"}
        assert tooling.short_name("clock") == tooling.short_name("statsbadge-clock")

        # The resolver explains itself at length; the useful part is the name.
        resolver = ("error: Because statsbadge-nope was not found in the package registry and "
                    "you require statsbadge-nope, we can conclude that your requirements are "
                    "unsatisfiable.")
        assert tooling.explain(resolver) == "no such package: statsbadge-nope"
        assert tooling.explain("error: no internet") == "no internet"
        assert tooling.explain("") == "uv did not say why"

        # Which package it was, out of either form: the caller holds the explained line.
        assert tooling.blamed(resolver) == "statsbadge-nope"
        assert tooling.blamed(tooling.explain(resolver)) == "statsbadge-nope"
        assert tooling.blamed("no internet") is None

        # An index is only asked about a bare name, a path or a specifier being skipped.
        # An unreachable index is no answer either, so both come back None.
        assert tooling.on_index("./extensions/statsbadge-iss") is None
        assert tooling.on_index("statsbadge-clock>=2") is None
        assert tooling.on_index("git+https://example.invalid/x.git") is None

        tooling.write_wanted(work, ["statsbadge-clock", "statsbadge-iss"])
        assert tooling.read_wanted(work) == ["statsbadge-clock", "statsbadge-iss"]
        tooling.forget_wanted(work)
        assert tooling.read_wanted(work) == []
        tooling.write_wanted(work, ["statsbadge-clock", "statsbadge-iss"])
        # The file explains itself, and the comments stay comments.
        assert pathlib.Path(work, tooling.WANTED).read_text(encoding="utf-8").startswith("#")
        # What is asked for against what is here, which is what `ext sync` repairs.
        assert tooling.adrift(work, ["clock"]) == ["statsbadge-iss"]
        assert tooling.adrift(work, ["clock", "iss"]) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_extension_asked_for_but_absent_is_built_back():
    """Asking for an extension the list already names rebuilds the whole list."""
    # A Python upgrade leaves the library unread, so the list asks for what is not here.
    from statsbadge import __main__ as cli
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-upgrade-")
    try:
        tooling.write_wanted(work, ["statsbadge-clock", "/src/statsbadge-cloudflare"])

        class Args:
            names = ["cloudflare"]
            config_dir = work
            verbose = False

        built = []
        was = (cli.tooling.library.build, cli.tooling.library.activate,
               cli.tooling.library.holds, cli.extensions.describe)
        try:
            def build(_directory, requirements, **_kwargs):
                built.append(list(requirements))
                return "/lib/gen", None

            cli.tooling.library.build = build
            cli.tooling.library.activate = lambda *_a: None
            cli.tooling.library.holds = lambda *_a: True
            # After an upgrade: clock is back, cloudflare still missing.
            cli.extensions.describe = lambda: [{"name": "clock"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert built == [["statsbadge-clock", "/src/statsbadge-cloudflare"]], built
            assert "not installed" in said.getvalue(), said.getvalue()
            # The list is untouched: it already asked for exactly this.
            assert tooling.read_wanted(work) == ["statsbadge-clock",
                                                 "/src/statsbadge-cloudflare"]

            # With the extension actually there, adding it again builds nothing.
            built.clear()
            cli.extensions.describe = lambda: [{"name": "clock"}, {"name": "cloudflare"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert built == [], built
            assert "already installed" in said.getvalue(), said.getvalue()
        finally:
            (cli.tooling.library.build, cli.tooling.library.activate,
             cli.tooling.library.holds, cli.extensions.describe) = was
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_extension_already_in_the_environment_is_recorded_and_reported():
    """An extension pip installed in the environment is written down, and a removal that
    cannot reach it says where it is."""
    from statsbadge import __main__ as cli
    from statsbadge import tooling

    work = tempfile.mkdtemp(prefix="statsbadge-add-")
    try:
        class Args:
            names = ["bluesky"]
            config_dir = work
            verbose = False

        was = (cli.tooling.library.build, cli.tooling.library.activate,
               cli.tooling.library.elsewhere, cli.extensions.describe,
               cli.tooling.on_index)
        try:
            cli.tooling.library.build = lambda *_a, **_k: ("/lib/gen", None)
            cli.tooling.library.activate = lambda *_a: None
            cli.tooling.on_index = lambda *_a, **_k: True
            # Installed into the environment, where a build cannot reach it.
            cli.tooling.library.elsewhere = lambda _dir, short: (
                "/venv/site-packages" if short == "bluesky" else None)

            # Installed, with nothing on the list: it has never been written.
            cli.extensions.describe = lambda: [{"name": "bluesky"}]
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert "already installed" in said.getvalue(), said.getvalue()
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]

            # Asking again is quiet, and does not write it twice.
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                assert cli._change_extensions(Args, "add") == 0  # noqa: SLF001
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]

            # Removing cannot take, so nothing is written and nothing is built: the list
            # goes on asking for it because it goes on being installed.
            built = []
            cli.tooling.library.build = lambda _d, r, **_k: (built.append(list(r))
                                                             or "/lib/gen", None)
            said, complained = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(said), contextlib.redirect_stderr(complained):
                assert cli._change_extensions(Args, "remove") == 1  # noqa: SLF001
            assert tooling.read_wanted(work) == ["statsbadge-bluesky"]
            assert built == [], "it built for a removal that could not happen"
            spoken = complained.getvalue()
            assert spoken.startswith("Unable to uninstall bluesky."), spoken
            assert "/venv/site-packages" in spoken, spoken
            assert "Removed" not in said.getvalue(), said.getvalue()

            # And again: the same answer, not a success.
            complained = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(complained):
                assert cli._change_extensions(Args, "remove") == 1  # noqa: SLF001
            assert complained.getvalue().startswith("Unable to uninstall bluesky.")
        finally:
            (cli.tooling.library.build, cli.tooling.library.activate,
             cli.tooling.library.elsewhere, cli.extensions.describe,
             cli.tooling.on_index) = was
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_generation_is_asked_about_by_name_and_not_by_prefix():
    """A dist-info is matched by name, not by the stem it shares with a version."""
    # The separator before the version is the hyphen a package name spells as an
    # underscore, so normalising the whole stem matches nothing.
    work = tempfile.mkdtemp(prefix="statsbadge-named-")
    try:
        for stem in ("statsbadge_clock-1.2.0", "statsbadge-1.3.3", "statsbadge_iss-1.0.3"):
            os.makedirs(os.path.join(work, f"{stem}.dist-info"))
        assert library.resolved(work, "statsbadge-clock") == "1.2.0"
        assert library.resolved(work, "statsbadge_clock") == "1.2.0"
        # The host itself, which is what the version check reads.
        assert library.resolved(work, "statsbadge") == "1.3.3"
        assert library.resolved(work, "statsbadge-quakes") is None
        assert library.holds(work, "clock") and library.holds(work, "iss")
        assert not library.holds(work, "quakes")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_upgrading_one_extension_leaves_the_others_where_they_are():
    """Naming one extension to upgrade leaves every other pinned to what the library
    holds."""
    # A build resolves every unpinned name to its latest, so an unpinned list moves whole.
    from statsbadge import tooling

    assert tooling.without_pin("statsbadge-clock==1.1.0") == "statsbadge-clock"
    assert tooling.without_pin("statsbadge-iss>=2") == "statsbadge-iss"
    assert tooling.without_pin("statsbadge-clock") == "statsbadge-clock"
    # A path resolves to whatever is in it, so there is no pin to take off.
    assert tooling.without_pin("/src/statsbadge-clock") == "/src/statsbadge-clock"
    assert tooling.pinned(["statsbadge-clock==1.1.0", "statsbadge-iss"]) == {"clock"}

    work = tempfile.mkdtemp(prefix="statsbadge-upgrade-")
    try:
        # Everything but the one named is held at the version the library carries.
        where = os.path.join(work, "lib", f"{library.tag()}-0001")
        os.makedirs(os.path.join(where, "statsbadge_iss-1.0.3.dist-info"))
        os.makedirs(os.path.join(where, "statsbadge_clock-1.1.0.dist-info"))
        building = tooling.holding(work, ["statsbadge-clock", "statsbadge-iss"], {"clock"})
        assert building == ["statsbadge-clock", "statsbadge-iss==1.0.3"], building

        # Naming a pinned one takes the pin off, and says so.
        done = tooling.plan("upgrade", ["clock"], ["statsbadge-clock==1.1.0"], set())
        assert done["wanted"] == ["statsbadge-clock"], done["wanted"]
        assert done["changed"] == ["statsbadge-clock"], done["changed"]
        assert done["unpinned"] and "1.1.0" in done["unpinned"][0], done["unpinned"]

        # Naming nothing means all of them, and touches no pin.
        done = tooling.plan("upgrade", [], ["statsbadge-clock==1.1.0"], set())
        assert done["changed"] == ["statsbadge-clock==1.1.0"], done["changed"]
        assert done["unpinned"] == [], done["unpinned"]
        # One that was never asked for cannot be upgraded.
        assert tooling.plan("upgrade", ["nope"], [], set())["absent"] == ["nope"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_generation_a_build_replaced_comes_off_the_path():
    """The replaced generation comes off sys.path, where it sits ahead of the new one."""
    work = tempfile.mkdtemp(prefix="statsbadge-path-")
    try:
        first = os.path.join(work, "lib", f"{library.tag()}-0001")
        second = os.path.join(work, "lib", f"{library.tag()}-0002")
        os.makedirs(first)
        assert library.activate(work) == first
        assert first in sys.path

        os.makedirs(second)
        assert library.activate(work) == second
        assert first not in sys.path, "the replaced generation stayed on the path"
        assert second in sys.path
    finally:
        for entry in (first, second):
            if entry in sys.path:
                sys.path.remove(entry)
        shutil.rmtree(work, ignore_errors=True)


def test_a_rebuild_does_not_prune_away_what_it_is_installing():
    """A build ignores the live generation when pruning, or it prunes what it just
    installed."""
    work = tempfile.mkdtemp(prefix="statsbadge-prune-")
    try:
        live = os.path.join(work, "lib", "gen-0001")
        target = os.path.join(work, "lib", "gen-0002")
        for where in (live, target):
            info = os.path.join(where, "madeup_ext-1.0.dist-info")
            os.makedirs(os.path.join(where, "madeup_ext"))
            os.makedirs(info)
            pathlib.Path(info, "METADATA").write_text(
                "Metadata-Version: 2.1\nName: madeup-ext\nVersion: 1.0\n", encoding="utf-8")
            pathlib.Path(info, "RECORD").write_text(
                "madeup_ext/__init__.py,,\nmadeup_ext-1.0.dist-info/METADATA,,\n",
                encoding="utf-8")
            pathlib.Path(where, "madeup_ext", "__init__.py").write_text("", encoding="utf-8")

        sys.path.append(live)
        try:
            importlib.invalidate_caches()
            assert library.prune(target, ignore=os.path.join(work, "lib")) == []
            assert os.path.isdir(os.path.join(target, "madeup_ext")), \
                "pruned the build it was installing"
            # Without the library ignored, the live generation counts and it goes.
            assert library.prune(target) == ["madeup_ext"]
            assert not os.path.isdir(os.path.join(target, "madeup_ext"))
        finally:
            sys.path.remove(live)
            importlib.invalidate_caches()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_catalogue_says_what_each_extension_is_and_what_it_needs():
    """A catalogue entry says what an extension does, whether it ships a badge page, and
    what it needs typed in."""
    listed = extensions.catalogue()
    named = {entry["name"] for entry in listed}
    assert {"clock", "iss", "quakes"} <= named, named
    for entry in listed:
        assert entry["summary"], entry
    # A badge module travels over USB, so the entry says whether there is one to install.
    ships = {entry["name"] for entry in listed if entry["page"]}
    assert ships == {"clock", "iss", "quakes"}, ships
    assert next(e for e in listed if e["name"] == "cloudflare")["needs"]


def test_an_extension_asked_for_but_absent_is_offered_as_such():
    """An extension on the list but not installed, or installed but not on the list, still
    appears in the tab."""
    # `uv tool install` replaces the environment whole and leaves `extensions.txt` alone,
    # so the two part company without either being edited.
    offered = {entry["name"]: entry for entry in
               extensions.offered(installed=[], wanted=["statsbadge-quakes"])}
    assert offered["quakes"]["asked"] and not offered["quakes"]["installed"]
    assert not offered["clock"]["asked"]

    # Anything the catalogue does not name is listed too, or a third-party extension is
    # invisible and unremovable.
    offered = {entry["name"]: entry for entry in extensions.offered(
        installed=[{"name": "weather", "version": "2.0", "badge_module": "w.py"}],
        wanted=[])}
    assert offered["weather"]["installed"] and offered["weather"]["page"]


def test_the_config_api_offers_the_catalogue_and_guards_what_it_installs(h):
    """An install naming nothing, or naming a package no index has, is refused before
    anything is written."""
    status, body = h.raw("GET", "/api/extensions")
    assert status == 200, (status, body)
    assert body["offered"] and "manageable" in body, body

    # Naming nothing is a bad request and not an empty rebuild.
    status, body = h.raw("POST", "/api/extensions", json.dumps({"add": []}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 400, (status, body)

    # A name absent from every index is refused before anything is written.
    status, body = h.raw("POST", "/api/extensions",
                         json.dumps({"add": ["statsbadge-not-a-real-one-xyz"]}).encode(),
                         {"Content-Type": "application/json"})
    assert status == 200 and body["ok"] is False, (status, body)
    assert body["unknown"] and body["why"], body


def test_uv_is_found_where_it_lives_and_not_only_on_the_path():
    """uv is found under the home directory when PATH does not carry it."""
    # A tray started at login carries the PATH it was given then, and a uv tool environment
    # has no pip behind it to fall back on.
    was_which, was_home = shutil.which, os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as home:
        beside = os.path.join(home, ".local", "bin")
        os.makedirs(beside)
        uv = os.path.join(beside, "uv.exe" if os.name == "nt" else "uv")
        with open(uv, "w", encoding="utf-8") as handle:
            handle.write("")
        try:
            shutil.which = lambda _name: None
            os.environ["HOME"] = home
            # USERPROFILE is what expanduser reads on Windows.
            was_profile = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = home
            assert library._uv() == uv, library._uv()
            assert library.tool()[0] == "uv"
        finally:
            shutil.which = was_which
            for key, value in (("HOME", was_home), ("USERPROFILE", was_profile)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_a_packaged_app_installs_with_a_version_and_not_an_interpreter():
    """A bundle has no interpreter to point uv at, so the installer passes a version
    instead."""
    was_executable, was_uv = sys.executable, library._uv
    try:
        sys.executable = os.path.join(os.sep, "Applications", "statsbadge.app",
                                      "Contents", "MacOS", "statsbadge")
        library._uv = lambda: os.path.join(os.sep, "somewhere", "uv")
        argv = library.installer()
        assert "--python" not in argv, argv
        assert argv[-2:] == ["--python-version",
                             f"{sys.version_info.major}.{sys.version_info.minor}"], argv
    finally:
        sys.executable, library._uv = was_executable, was_uv

    # Anywhere else it is the running interpreter, whose environment is being built against.
    assert "--python" in library.installer()


def test_a_packaged_app_spawns_itself_as_pip():
    """`-m pip` in a bundle starts a second copy of the app, so it spawns itself under a
    verb the tray ignores."""
    from statsbadge import PIP_VERB, __main__ as cli

    app = os.path.join(os.sep, "Applications", "statsbadge.app", "Contents", "MacOS",
                       "statsbadge")
    was_executable, was_uv, was_pip = sys.executable, library._uv, library._packaged_pip
    try:
        sys.executable = app
        library._uv = lambda: None                 # no uv anywhere either
        library._packaged_pip = lambda: True
        assert library.tool() == ("pip", [app, PIP_VERB])
        assert "-m" not in library.installer(), library.installer()

        # Nothing to install with, where pip did not travel with the app.
        library._packaged_pip = lambda: False
        assert library.tool() is None
    finally:
        sys.executable = was_executable
        library._uv, library._packaged_pip = was_uv, was_pip

    # The verb reaches pip rather than the tray, however the app was started.
    ran = []
    was_pip_run = cli.be_pip
    try:
        cli.be_pip = ran.append
        cli.tray_main([PIP_VERB, "install", "nothing"])
        assert ran == [["install", "nothing"]], ran
    finally:
        cli.be_pip = was_pip_run
