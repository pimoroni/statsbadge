#!/usr/bin/env python3
"""Turn the badge's raw framebuffer dumps into PNGs.

    python3 tools/shots.py build/shots              # convert what the badge dumped
    python3 tools/shots.py build/shots --publish    # and copy the ones the docs show

The badge writes frames into build/shots, which is ignored. A render of every page and
every theme is a review artefact, and committing all of it means a drawing change shows up
as forty modified images. Only what the README and the project page link is checked in,
and --publish reads both to decide which those are, so the set maintains itself.

The dumps are 320x240 straight from `screen.raw`: R G B A per pixel and
premultiplied, so alpha is divided back out here. Measured, not assumed - a pure red
`color.rgb(255, 0, 0)` comes back as `ff 00 00 ff`, so there is no byte swap.
"""

import pathlib
import re
import shutil
import struct
import subprocess
import sys
import zlib

W, H = 320, 240


def un_premultiply(raw):
    out = bytearray(W * H * 4)
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a and a != 255:
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        out[i] = r
        out[i + 1] = g
        out[i + 2] = b
        out[i + 3] = 255
    return bytes(out)


def write_png(path, rgba):
    rows = b"".join(
        b"\x00" + rgba[y * W * 4 : (y + 1) * W * 4] for y in range(H)
    )

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main(directory):
    folder = pathlib.Path(directory)
    dumps = sorted(folder.glob("*.raw"))
    if not dumps:
        return f"no .raw dumps in {folder}"
    for dump in dumps:
        raw = dump.read_bytes()
        if len(raw) != W * H * 4:
            print(f"skipping {dump}: {len(raw)} bytes, expected {W * H * 4}")
            continue
        write_png(dump.with_suffix(".png"), un_premultiply(raw))
        print("wrote {}".format(dump.with_suffix(".png").name))
    return None


def shrink(path):
    """Halve a shot with pngquant, where it is installed.

    A palette is plenty for a flat vector render. 256 colours against the thousand or so
    of antialiasing takes these from about 13KB to 7KB with nothing visible to tell them
    apart, checked at 2x on the gauges.

    Indexed PNGs are fine here, unlike the app's icon: nothing on the badge loads these,
    and its image.load mis-decodes a palette.
    """
    if not shutil.which("pngquant"):
        return
    subprocess.run(["pngquant", "--force", "--skip-if-larger", "--strip", "--speed", "1",
                    "--output", str(path), "--", str(path)], check=False)


def linked_shots():
    """The shot names the README or the project page shows, which earns a place here.

    Both, because the page publishes out of the same `shots` directory: a figure added to
    index.html and to nowhere else would never be copied in, leaving the site asking for a file
    that is not there.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    text = "".join((root / name).read_text() for name in ("README.md", "index.html")
                   if (root / name).is_file())
    return sorted(set(re.findall(r"shots/([A-Za-z0-9_]+)\.png", text)))


def publish(source, target="shots"):
    """Copy the shots the docs show out of a build directory, and report what is missing."""
    target = pathlib.Path(target)
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    for name in linked_shots():
        found = pathlib.Path(source) / f"{name}.png"
        if not found.exists():
            missing.append(name)
            continue
        shutil.copyfile(found, target / f"{name}.png")
        shrink(target / f"{name}.png")
    print(f"published {len(linked_shots()) - len(missing)} of "
          f"{len(linked_shots())} into {target}")
    if missing:
        print("not in " + str(source) + ": " + ", ".join(missing))
    # Anything checked in that neither of them shows any more is only taking up room.
    extra = sorted(p.stem for p in target.glob("*.png")
                   if p.stem not in linked_shots())
    if extra:
        print("checked in but unreferenced: " + ", ".join(extra))


if __name__ == "__main__":
    where = sys.argv[1] if len(sys.argv) > 1 else "build/shots"
    fault = main(where)
    if not fault and "--publish" in sys.argv:
        publish(where)
    if fault:
        sys.exit(fault)
