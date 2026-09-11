#!/usr/bin/env python3
"""Put the version being built into the briefcase table of pyproject.toml."""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
# In the briefcase table alone. The project's own version is dynamic and stays that way.
LINE = re.compile(r'^(version\s*=\s*)"[^"]*"$', re.M)


def release(version):
    """Return the leading numbers, as three of them."""
    found = re.match(r"\D*(\d+(?:\.\d+)*)", version)
    if not found:
        raise SystemExit(f"no version numbers in {version!r}")
    numbers = (found.group(1).split(".") + ["0", "0"])[:3]
    return ".".join(numbers)


def current():
    """Return what is being built: the installed distribution, or the nearest tag."""
    from importlib.metadata import PackageNotFoundError, version as installed
    try:
        return installed("statsbadge")
    except PackageNotFoundError:
        pass
    try:
        described = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                                   capture_output=True, text=True, check=True, cwd=ROOT)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("no installed statsbadge and no tag to read a version "
                         "from") from exc
    return described.stdout.strip()


def main(argv):
    wanted = release(argv[0] if argv else current())
    text = PYPROJECT.read_text(encoding="utf-8")
    head, marker, rest = text.partition("[tool.briefcase]")
    if not marker:
        raise SystemExit("no [tool.briefcase] table in pyproject.toml")
    replaced, count = LINE.subn(rf'\g<1>"{wanted}"', rest, count=1)
    if not count:
        raise SystemExit("no version to set in the briefcase table")
    PYPROJECT.write_text(head + marker + replaced, encoding="utf-8")
    print(f"packaging as {wanted}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
