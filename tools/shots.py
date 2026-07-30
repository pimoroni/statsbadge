#!/usr/bin/env python3
"""Turn the badge's raw framebuffer dumps into PNGs.

    python3 tools/shots.py shots

The dumps are 320x240 straight from `screen.raw`: R G B A per pixel and
premultiplied, so alpha is divided back out here. Measured, not assumed - a pure red
`color.rgb(255, 0, 0)` comes back as `ff 00 00 ff`, so there is no byte swap.
"""

import pathlib
import struct
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


if __name__ == "__main__":
    fault = main(sys.argv[1] if len(sys.argv) > 1 else "shots")
    if fault:
        sys.exit(fault)
