#!/usr/bin/env bash
# Build a precompiled .mpy copy of a Badgeware app.
#
#   ci/build-mpy.sh [APP_DIR] [OUT_DIR]
#
# The badge compiles from source at every launch, which for this app is 763ms off its own
# flash. Precompiling takes that to 126ms - an 84% saving - at the cost of the app no
# longer being readable on the badge, so it ships alongside the source zip rather than
# replacing it.
#
# A .mpy is only loadable by a firmware whose bytecode version matches, so mpy-cross has
# to be built from the same MicroPython the board ships. That version is not restated
# here: it is read out of the board repo's ci/micropython.sh, which is where the firmware
# build gets it, so the two cannot drift apart.
#
# Nothing here belongs at runtime. The user's `statsbadge install` must never fetch a
# toolchain; CI builds this and the install pushes the result.
#
# Overridable:
#   BOARD_REPO   default pimoroni/tufty2350
#   BOARD_REF    default main. Note that at the time of writing main pins bw-1.27.0 and
#                feature/align-v3 pins bw-1.28.0-3, so a board running the newer firmware
#                needs BOARD_REF=feature/align-v3 until that lands on main
#   EXPECT_MPY   assert the emitted bytecode matches this sys.implementation._mpy
#   ENTRY_MPY    compile __init__.py too. Off by default: the launcher looks for
#                __init__.py when deciding a directory is an app, so a bytecode-only
#                entry point is invisible to it until that is fixed. Leaving the entry as
#                source costs about 40ms of the saving and keeps the app launchable now
#   MPY_CROSS    a prebuilt mpy-cross, skips the clone and build entirely
#   WORK_DIR     where to clone and build, default build/micropython
set -euo pipefail

APP_DIR=${1:-src/statsbadge/badge_app}
OUT_DIR=${2:-build/mpy}
BOARD_REPO=${BOARD_REPO:-pimoroni/tufty2350}
BOARD_REF=${BOARD_REF:-main}
WORK_DIR=${WORK_DIR:-build/micropython}

if [ ! -d "$APP_DIR" ]; then
    echo "error: no app directory at $APP_DIR" >&2
    exit 1
fi

# -- which MicroPython -------------------------------------------------------

read_pin() {
    # Grep rather than source: that script sets a terminal up and defines functions,
    # and all that is wanted from it is two assignments.
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
        # The entry point stays source so the launcher still sees an app here.
        cp "$source" "$OUT_DIR/__init__.py"
        continue
    fi
    "$MPY_CROSS" -o "$OUT_DIR/$name.mpy" "$source"
    compiled=$((compiled + 1))
done

# Everything that is not Python goes across untouched - the launcher wants icon.png.
find "$APP_DIR" -maxdepth 1 -type f ! -name '*.py' -exec cp {} "$OUT_DIR/" \;

if [ -z "${ENTRY_MPY:-}" ]; then
    echo "note: __init__.py left as source for the launcher; set ENTRY_MPY=1 once it"
    echo "      recognises __init__.mpy"
fi

# -- verify ------------------------------------------------------------------

# A .mpy the firmware does not recognise fails at import, on the badge, after the app has
# launched - so it is worth catching here. The header is 'M', version, reserved, flags,
# and (flags << 8) | version is exactly what the badge reports as
# sys.implementation._mpy, so the number written out is directly comparable and the
# installer checks it against the board it is talking to.
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

mpy = seen.pop()
version, flags = mpy & 0xFF, mpy >> 8
print(f"bytecode v{version}.{flags & 3}, arch {flags >> 2}, _mpy {mpy}")
if expect and int(expect) != mpy:
    sys.exit(f"error: built _mpy {mpy} but expected {expect}; wrong MicroPython version")
pathlib.Path(out / "MPY_VERSION").write_text(f"{mpy}\n")
PY

echo "compiled $compiled modules into $OUT_DIR"
ls -la "$OUT_DIR"
