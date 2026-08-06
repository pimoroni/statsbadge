#!/usr/bin/env python3
"""One picture in every theme's shades, as a sheet to look at.

    python3 tools/image_themes.py somewhere/photo.jpg          # into build/image-themes
    python3 tools/image_themes.py photo.jpg --levels 4
    python3 tools/image_themes.py photo.jpg --out /tmp/look

What the badge does with a picture is: take the indices the host dithered, and write the
theme's own shades into the image's colour table. This does the second half here, so whether
a ramp is any good is a thing you can look at instead of a set of numbers - across every
theme at once, which is the only way to see that one of them has gone muddy or that a
picture has stopped belonging to its page.

Each tile is drawn on that theme's own background with its ink beside it, because a picture
is only ever seen on a page: shades that look fine on white can disappear on the page they
are actually going on.

Needs `statsbadge[images]` for the decoding, the same as `imaging` itself.
"""

import argparse
import pathlib
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from statsbadge import derive, imaging, layout  # noqa: E402

# The tile, and the room around it. A picture is shown at the size the badge draws it, since
# what a dither looks like is a question about pixels and scaling it answers a different one.
PAD = 10
LABEL_H = 14
COLUMNS = 4


def sheet(indices, width, height, themes, levels, tint):
    """Every theme's take on one picture, as an RGB raster and its size."""
    tile_w, tile_h = width + PAD * 2, height + PAD * 2 + LABEL_H
    rows = -(-len(themes) // COLUMNS)
    sheet_w, sheet_h = tile_w * COLUMNS, tile_h * rows
    raster = bytearray(sheet_w * sheet_h * 3)

    for at, name in enumerate(themes):
        palette = layout.palette_for(name, tint)
        shades = [tuple(rgb) for rgb in palette["image"][str(levels)]]
        background, ink = tuple(palette["bg"]), tuple(palette["ink"])
        left, top = (at % COLUMNS) * tile_w, (at // COLUMNS) * tile_h

        for y in range(tile_h):
            for x in range(tile_w):
                at_pixel = ((top + y) * sheet_w + left + x) * 3
                raster[at_pixel:at_pixel + 3] = bytes(background)
        for y in range(height):
            for x in range(width):
                at_pixel = ((top + PAD + y) * sheet_w + left + PAD + x) * 3
                raster[at_pixel:at_pixel + 3] = bytes(shades[indices[y * width + x]])
        # The shades themselves under the picture, as a strip: what the eye reads off a
        # photograph and what the ramp actually is are different questions.
        strip = top + PAD + height + 3
        for step, shade in enumerate(shades):
            wide = width // len(shades)
            for y in range(6):
                for x in range(wide):
                    at_pixel = ((strip + y) * sheet_w + left + PAD + step * wide + x) * 3
                    raster[at_pixel:at_pixel + 3] = bytes(shade)
        # A rule in the ink, so a tile whose picture has vanished into its page still shows
        # where it was.
        for x in range(width):
            at_pixel = ((strip + 8) * sheet_w + left + PAD + x) * 3
            raster[at_pixel:at_pixel + 3] = bytes(ink)
    return bytes(raster), sheet_w, sheet_h


def write_png(path, raster, width, height):
    rows = b"".join(b"\x00" + raster[y * width * 3:(y + 1) * width * 3]
                    for y in range(height))

    def chunk(tag, body):
        block = tag + body
        return (struct.pack(">I", len(body)) + block
                + struct.pack(">I", zlib.crc32(block) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 9))
                     + chunk(b"IEND", b""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("picture", help="any image Pillow can read")
    parser.add_argument("--out", default="build/image-themes")
    parser.add_argument("--levels", type=int, choices=sorted(imaging.LEVELS.values()),
                        default=None, help="both, unless one is named")
    parser.add_argument("--orientation", default="landscape",
                        choices=("landscape", "portrait"))
    args = parser.parse_args(argv)

    if not imaging.available():
        return "install statsbadge[images] - Pillow does the decoding"
    data = pathlib.Path(args.picture).read_bytes()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from PIL import Image
    import io

    wanted = [args.levels] if args.levels else sorted(set(imaging.LEVELS.values()))
    themes = list(layout.THEMES)
    tint = layout.DEFAULT_CONFIG["tint"]
    for levels in wanted:
        preset = next(name for name, count in imaging.LEVELS.items() if count == levels)
        png = imaging.thumbnail(data, preset, args.orientation)
        indexed = Image.open(io.BytesIO(png))
        width, height = indexed.size
        raster, sheet_w, sheet_h = sheet(indexed.tobytes(), width, height, themes,
                                         levels, tint)
        path = out / f"themes-{preset}-{args.orientation}-{levels}.png"
        write_png(path, raster, sheet_w, sheet_h)
        print(f"{path}  {len(themes)} themes at {width}x{height}, {levels} shades")

    # What each theme is actually asking for, since a sheet says which looks wrong and this
    # says why: a picture is as colourful as the theme's own accent is.
    print()
    print(f"{'theme':22} {'accent share':>13} {'strongest shade':>16}")
    for name in themes:
        palette = layout.palette_for(name, tint)
        light, chroma, hue = derive.oklch(tuple(palette["accent"]))
        limit = derive.max_chroma(light, hue)
        share = (chroma / limit) if limit else 0.0
        loudest = max(derive.oklch(tuple(rgb))[1]
                      for rgb in palette["image"][str(wanted[-1])])
        print(f"  {name:20} {share:13.2f} {loudest:16.3f}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
