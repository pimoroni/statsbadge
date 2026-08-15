"""What ships, and the conventions that keep the builds agreeing."""

import pathlib
import re
import tomllib
import urllib.error
import urllib.request

import yaml

from statsbadge import install


def test_the_build_script_defaults_where_the_installer_looks():
    """ci/build-mpy.sh writes where the installer reads, so a bare rebuild replaces the
    bytecode."""
    script = (pathlib.Path(__file__).parent.parent / "ci" / "build-mpy.sh").read_text(encoding="utf-8")
    default = [line for line in script.splitlines() if line.startswith("OUT_DIR=")]
    assert default, "no OUT_DIR default in the build script"
    assert "src/statsbadge/badge_app/mpy" in default[0], default[0]

    # Both CI workflows compile the app, and both still name the packaged copy.
    for workflow in ("ci.yml", "publish.yml"):
        text = (pathlib.Path(__file__).parent.parent / ".github" / "workflows"
                / workflow).read_text(encoding="utf-8")
        assert "build-mpy.sh" in text, f"{workflow} no longer compiles the app"
        assert "src/statsbadge/badge_app/mpy" in text, workflow


def test_the_version_is_written_down_once():
    """The tag is the version: no package here declares one, and the prefix a workflow
    fires on is the prefix its build strips."""
    import statsbadge

    source = pathlib.Path("src/statsbadge/__init__.py").read_text(encoding="utf-8")
    # The assignment, since the word itself appears in the docstring above.
    assert not re.search(r"^__version__\s*=", source, re.M), "a second copy of the version"
    assert statsbadge.version(), "nothing can say what is installed"

    with open("pyproject.toml", "rb") as handle:
        main = tomllib.load(handle)
    assert main["project"].get("version") is None, "a static version is back"
    assert "version" in main["project"]["dynamic"], main["project"]
    # Hatchling leaves out what the VCS ignores, and the precompiled app is a build
    # artefact, so without this the badge compiles the sources at every launch.
    artifacts = main["tool"]["hatch"]["build"]["artifacts"]
    assert any("badge_app/mpy" in entry for entry in artifacts), artifacts

    workflows = pathlib.Path(".github/workflows")
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with open(pyproject, "rb") as handle:
            plugin = tomllib.load(handle)
        name = plugin["project"]["name"]
        short = name.removeprefix("statsbadge-")
        assert plugin["project"].get("version") is None, name
        assert "version" in plugin["project"]["dynamic"], name
        # Its tags and nobody else's, or a release of one extension versions them all.
        prefix = plugin["tool"]["uv-dynamic-versioning"]["pattern-prefix"]
        assert prefix == f"{short}-", (name, prefix)
        # The prefix the workflow fires on is the prefix the build strips.
        workflow = yaml.safe_load(
            (workflows / f"publish-{short}.yml").read_text(encoding="utf-8"))
        assert workflow["env"]["TAG_PREFIX"] == f"{prefix}v", (short, prefix)
        for module in (directory / "src").rglob("__init__.py"):
            assert not re.search(r"^__version__\s*=", module.read_text(encoding="utf-8"), re.M), module


def test_every_package_here_can_be_published():
    """Every package here has a publish workflow, and each one fires on its tag prefix
    alone."""
    # Trusted publishing matches on a workflow filename, and a release fires them all.
    workflows = pathlib.Path(".github/workflows")
    main = (workflows / "publish.yml").read_text(encoding="utf-8")
    # The top-level package takes the plain tags, and lets an extension's release alone.
    assert "startsWith(github.event.release.tag_name, 'v')" in main

    found = []
    for directory in sorted(pathlib.Path("extensions").iterdir()):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        with open(pyproject, "rb") as handle:
            name = tomllib.load(handle)["project"]["name"]
        short = name.removeprefix("statsbadge-")
        found.append(short)

        path = workflows / f"publish-{short}.yml"
        assert path.is_file(), f"{name} has no publish workflow"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        settings = workflow["env"]
        assert settings["PACKAGE"] == name, (path.name, settings)
        assert settings["DIRECTORY"] == f"extensions/{name}", (path.name, settings)
        # Twice in each workflow: the guard that gates the run and the strip that checks
        # the version.
        assert settings["TAG_PREFIX"] == f"{short}-v", (path.name, settings)
        job = workflow["jobs"]["publish"]
        assert f"startsWith(github.event.release.tag_name, '{short}-v')" in job["if"], path.name

        # Every step runs in the extension's directory, not the repository root.
        running = [step for step in job["steps"] if "run" in step]
        assert running, path.name
        for step in running:
            assert step.get("working-directory") == "${{ env.DIRECTORY }}", (
                path.name, step.get("name"))
        assert any("uv publish --trusted-publishing always" in step["run"]
                   for step in running), path.name

    assert len(found) >= 3, found
    # Every workflow names a package that is here; a stale one publishes whatever it finds.
    for workflow in workflows.glob("publish-*.yml"):
        short = workflow.stem.removeprefix("publish-")
        assert short in found, f"{workflow.name} publishes an extension that is not here"


def test_a_published_readme_links_to_somewhere_that_exists():
    """Every link in a published README is absolute, names this repository, and resolves."""
    # PyPI serves the README as the project page and resolves a relative link against
    # pypi.org, where `shots/cpu.png` is a broken image and `DEVELOPMENT.md` a 404.
    for pyproject in [pathlib.Path("pyproject.toml"),
                      *sorted(pathlib.Path("extensions").glob("*/pyproject.toml"))]:
        with open(pyproject, "rb") as handle:
            project = tomllib.load(handle)["project"]
        readme = pyproject.parent / project["readme"]
        text = readme.read_text(encoding="utf-8")
        repository = project["urls"]["Repository"].removesuffix("/")
        slug = repository.removeprefix("https://github.com/")
        raw = f"https://raw.githubusercontent.com/{slug}/main/"

        for label, target in re.findall(r"\[([^\]]*)\]\(([^)\s]+)\)", text):
            where = f"{readme}: [{label}]({target})"
            assert target.startswith(("http", "#")), f"{where} does not resolve on PyPI"
            # A picture carries the repository name, so a rename leaves it broken.
            if target.startswith("https://raw.githubusercontent.com/"):
                assert target.startswith(raw), where
            # A link naming a path in this tree can be looked at, so it is. Anything else
            # belongs to whoever owns it.
            for prefix in (f"{repository}/blob/main/", f"{repository}/tree/main/", raw):
                if target.startswith(prefix):
                    assert pathlib.Path(target.removeprefix(prefix)).exists(), where


def test_the_mark_is_the_same_one_everywhere(h, ui):
    """The badge, the config UI, the site and the tray all draw one mark, to one
    geometry."""
    # As bytes: the server hands the file over unchanged, and a Windows checkout with CRLF
    # line endings would not match text read back through universal newlines.
    icon = pathlib.Path("src/statsbadge/web/icon.svg").read_bytes().decode("utf-8")
    page = ui.markup
    site = pathlib.Path("index.html").read_text(encoding="utf-8")

    # A favicon served as octet-stream is one the browser ignores. Safari reads no SVG
    # favicon at all, so a raster of the same mark is offered first.
    assert '<link rel="icon" href="/icon.svg"' in page
    assert page.index('href="/icon.png"') < page.index('href="/icon.svg"'), \
        "the fallback is behind the SVG Safari cannot read"
    with urllib.request.urlopen(h.url("/icon.png"), timeout=5) as response:
        assert response.headers.get("content-type") == "image/png", response.headers
        assert response.read()[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    with urllib.request.urlopen(h.url("/icon.svg"), timeout=5) as response:
        assert response.status == 200
        assert response.headers.get("content-type") == "image/svg+xml", response.headers
        assert response.read().decode() == icon

    # The site's data URI quotes attributes with apostrophes, so the numbers are compared
    # and not the markup around them.
    assert 'rel="icon"' in site, "the site has no mark to be the same as"
    for outline in re.findall(r'd="([^"]+)"', icon):
        assert outline in site, outline
    for numbers in re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" '
                              r'width="([\d.]+)" height="([\d.]+)"', icon):
        for number in numbers:
            assert f"'{number}'" in site, (numbers, number)

    # splash.py is what the badge draws before it has a font, and the bars carry the mark.
    splash = (pathlib.Path(install.app_source_dir()) / "splash.py").read_text(encoding="utf-8")
    numbers = {}
    for line in splash.splitlines():
        if line.startswith(("BAR_W", "BAR_GAP", "BAR_HEIGHTS", "OUTER", "INNER")):
            exec(line, numbers)  # noqa: S102  a module in this repo, five constants off the top
    boxes = [(float(w), float(t)) for w, t in
             re.findall(r'<rect x="[\d.]+" y="[\d.]+" width="([\d.]+)" height="([\d.]+)"', icon)]
    assert len(boxes) == len(numbers["BAR_HEIGHTS"]), boxes
    scale = boxes[0][0] / numbers["BAR_W"]
    for (_wide, tall), height in zip(boxes, numbers["BAR_HEIGHTS"], strict=True):
        assert abs(tall / scale - height) < 0.5, (tall, height, scale)
    # The arc's radii are the dial's, at the same scale.
    radii = sorted({float(r) for r in re.findall(r"A([\d.]+) [\d.]+ 0 1 [01]", icon)})
    assert abs(radii[0] / scale - numbers["INNER"]) < 0.5, radii
    assert abs(radii[-1] / scale - numbers["OUTER"]) < 0.5, radii

    # A template icon is shape in the alpha alone: AppKit paints it to suit the menu bar.
    from PIL import Image
    assets = pathlib.Path("src/statsbadge/tray/assets")
    for name in ("tray", "tray-attention", "tray-template", "tray-template-attention"):
        with Image.open(assets / f"{name}.png") as art:
            assert art.size[0] == art.size[1], (name, art.size)
            if "template" not in name:
                continue
            coloured = [rgba for _count, rgba in art.convert("RGBA").getcolors(1 << 20)
                        if rgba[3] and rgba[:3] != (0, 0, 0)]
            assert not coloured, (name, coloured[:4])
    assert (assets / "statsbadge.ico").is_file(), "the Windows installer wants an .ico"
