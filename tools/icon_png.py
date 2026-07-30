#!/usr/bin/env python3
"""Convert the badge's icon.raw dump into the 24x24 icon.png the launcher wants.

    python3 tools/icon_png.py stats/icon.raw stats/icon.png
"""

import pathlib
import struct
import sys
import zlib

SIZE = 24


def main(source, target):
    raw = pathlib.Path(source).read_bytes()
    if len(raw) != SIZE * SIZE * 4:
        return f"{source} is {len(raw)} bytes, expected {SIZE * SIZE * 4}"

    rgba = bytearray(len(raw))
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a and a != 255:
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        rgba[i:i + 4] = bytes((r, g, b, a))

    rows = b"".join(b"\x00" + bytes(rgba[y * SIZE * 4:(y + 1) * SIZE * 4])
                    for y in range(SIZE))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    pathlib.Path(target).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b""))
    print(f"wrote {target}")
    return None


if __name__ == "__main__":
    fault = main(sys.argv[1] if len(sys.argv) > 1 else "stats/icon.raw",
                 sys.argv[2] if len(sys.argv) > 2 else "stats/icon.png")
    if fault:
        sys.exit(fault)
