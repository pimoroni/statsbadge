#!/usr/bin/env bash
# Build a precompiled .mpy copy of a Badgeware app.
#
#   ci/build-mpy.sh [APP_DIR] [OUT_DIR]

# With no arguments it builds into the package, where the installer looks for it. What
# precompiling buys is under "Launch, and .mpy" in DEVELOPMENT.md. mpy-cross comes from the
# MicroPython the board repo's ci/micropython.sh pins, keeping bytecode and firmware
# versions together.

# Overridable:
#   BOARD_REPO   default pimoroni/tufty2350
#   BOARD_REF    default main, which pins bw-1.27.0. A board on bw-1.28.0-3 needs
#                BOARD_REF=feature/align-v3 until that lands on main
#   EXPECT_MPY   assert the emitted bytecode matches this sys.implementation._mpy
#   ENTRY_MPY    compile __init__.py too. Off by default: the launcher reads __init__.py
#                to decide a directory is an app, so a bytecode-only entry point is
#                invisible to it. Source costs about 40ms of the saving
#   MPY_CROSS    a prebuilt mpy-cross, skips the clone and build entirely
#   WORK_DIR     where to clone and build, default build/micropython
set -euo pipefail

APP_DIR=${1:-src/statsbadge/badge_app}
# Defaults to the copy inside the package, which `statsbadge install` reads and
# the wheel ships. build/mpy is for a release artefact, and CI passes that
# explicitly.
OUT_DIR=${2:-src/statsbadge/badge_app/mpy}
BOARD_REPO=${BOARD_REPO:-pimoroni/tufty2350}
BOARD_REF=${BOARD_REF:-main}
WORK_DIR=${WORK_DIR:-build/micropython}

if [ ! -d "$APP_DIR" ]; then
    echo "error: no app directory at $APP_DIR" >&2
    exit 1
fi

# -- which MicroPython -------------------------------------------------------

read_pin() {
    # Grepped, not sourced: that script sets up a terminal and defines functions, where
    # all this needs from it is two assignments.
    local url="https://raw.githubusercontent.com/$BOARD_REPO/$BOARD_REF/ci/micropython.sh"
    local script
    script=$(curl -fsSL "$url") || {
        echo "error: cannot read $url" >&2
        exit 1
    }
    FLAVOUR=$(printf '%s\n' "$script" | grep -E '^MICROPYTHON_FLAVOUR=' | head -1 | cut -d'"' -f2)
    VERSION=$(printf '%s\n' "$script" | grep -E '^MICROPYTHON_VERSION=' | head -1 | cut -d'"' -f2)
    if [ -z "${FLAVOUR:-}" ] || [ -z "${VERSION:-}" ]; then
        echo "error: could not find MICROPYTHON_FLAVOUR/VERSION in $url" >&2
        exit 1
    fi
}

if [ -n "${MPY_CROSS:-}" ]; then
    echo "using the mpy-cross given: $MPY_CROSS"
else
    read_pin
    echo "board $BOARD_REPO@$BOARD_REF pins MicroPython $FLAVOUR/$VERSION"
    if [ ! -x "$WORK_DIR/mpy-cross/build/mpy-cross" ]; then
        mkdir -p "$(dirname "$WORK_DIR")"
        if [ ! -d "$WORK_DIR/.git" ]; then
            git clone --filter=blob:none "https://github.com/$FLAVOUR/micropython" "$WORK_DIR"
        fi
        git -C "$WORK_DIR" fetch --tags --depth 1 origin "$VERSION"
        git -C "$WORK_DIR" checkout --detach FETCH_HEAD
        make -C "$WORK_DIR/mpy-cross" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
    fi
    MPY_CROSS="$WORK_DIR/mpy-cross/build/mpy-cross"
fi

"$MPY_CROSS" --version

# -- compile -----------------------------------------------------------------

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

compiled=0
for source in "$APP_DIR"/*.py; do
    name=$(basename "$source" .py)
    if [ "$name" = "__init__" ] && [ -z "${ENTRY_MPY:-}" ]; then
        # The entry point stays source for the launcher to recognise an app here.
        cp "$source" "$OUT_DIR/__init__.py"
        continue
    fi
    # -s so a traceback on the badge names the module, not the runner's build path.
    "$MPY_CROSS" -s "$name.py" -o "$OUT_DIR/$name.mpy" "$source"
    compiled=$((compiled + 1))
done

# Everything that is not Python goes across untouched: the launcher reads icon.png, draw
# reads icons.af and fonts/. Subdirectories included, and with their paths. The app's text
# font lives in fonts/, and a depth of one silently shipped a build with no type in it.
# A loop rather than `install -D`, which BSD install has not got: this script is run by
# hand on a Mac as well as by CI on Linux, and one code path is the point of it.
(cd "$APP_DIR" && find . -type f ! -name '*.py' ! -path './mpy/*' \
        ! -path './__pycache__/*' -print) | while IFS= read -r file; do
    mkdir -p "$OUT_DIR/$(dirname "$file")"
    cp "$APP_DIR/$file" "$OUT_DIR/$file"
done

if [ -z "${ENTRY_MPY:-}" ]; then
    echo "note: __init__.py left as source for the launcher; set ENTRY_MPY=1 once it"
    echo "      recognises __init__.mpy"
fi

# -- verify ------------------------------------------------------------------

# An .mpy the firmware does not recognise fails at import, on the badge, after the app
# has launched, which is why it is caught here. The header is 'M', version, reserved,
# flags. `(flags << 8) | version` is the number the badge reports as
# sys.implementation._mpy, so the value written out compares directly against the board
# the installer is talking to.
python3 - "$OUT_DIR" "${EXPECT_MPY:-}" <<'PY'
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
expect = sys.argv[2] if len(sys.argv) > 2 else ""

seen = set()
for path in sorted(out.glob("*.mpy")):
    header = path.read_bytes()[:4]
    if header[:1] != b"M":
        sys.exit(f"{path}: not a .mpy (magic {header[:1]!r})")
    seen.add((header[3] << 8) | header[1])
if not seen:
    sys.exit("no .mpy files were produced")
if len(seen) > 1:
    sys.exit(f"mixed bytecode versions in one build: {sorted(seen)}")

# A .py alongside an .mpy takes precedence, so leaving one behind silently undoes the
# precompile and the app quietly costs full compile time again. Checked, not assumed.
shadowed = [p.name for p in out.glob("*.mpy") if (out / f"{p.stem}.py").exists()]
if shadowed:
    sys.exit(f"error: these .mpy are shadowed by a .py beside them: {shadowed}")

mpy = seen.pop()
version, flags = mpy & 0xFF, mpy >> 8
# The high byte is feature flags, not an architecture: -march makes no difference to a
# bytecode-only .mpy, and armv6m, x64 and the default all emit an identical header.
print(f"bytecode v{version}.{flags & 3}, flags {flags}, _mpy {mpy}")
if expect and int(expect) != mpy:
    sys.exit(f"error: built _mpy {mpy} but expected {expect}; wrong MicroPython version")
(out / "MPY_VERSION").write_text(f"{mpy}\n")
PY

# What was compiled, by content hash. Distinguishes a stale build from a current one
# without relying on mtimes.
python3 - "$APP_DIR" "$OUT_DIR" <<'PY'
import hashlib
import json
import pathlib
import sys

app, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sources = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(app.glob("*.py"))
}
(out / "BUILD_INFO").write_text(json.dumps({"sources": sources}, indent=2))
print(f"recorded hashes for {len(sources)} sources")
PY

echo "compiled $compiled modules into $OUT_DIR"
ls -la "$OUT_DIR"
