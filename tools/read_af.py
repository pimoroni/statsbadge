#!/usr/bin/env python3
"""Report what is in an .af font.

    python3 tools/read_af.py src/statsbadge/badge_app/fonts/lexend-regular.af
    python3 tools/read_af.py MonaSans-Medium.af --chars "HxpO0.,%"

Needs nothing installed. This is how the conventions make_text_font.py builds to were
established: by reading them out of the font the badge already ships rather than assuming
them, and it is how to check a font that was built but does not draw.

Two things worth looking at in the output. A capital should stand 81 units, which
look.py's sizes are in terms of, and an advance should be a little wider than the ink it
sits in. A glyph with ink and an advance of about one is the shape of a units mix-up: it
packs and loads without complaint and draws every letter of a word in the same place.
"""

import argparse

from af import COORD_MAX, WIDE_COORD_MAX, read


def main():
    parser = argparse.ArgumentParser(description="Report what is in an .af font.")
    parser.add_argument("font")
    parser.add_argument("--chars", default="HxpO0.,%-/ ",
                        help="which glyphs to show in detail")
    parser.add_argument("--all", action="store_true", help="show every glyph")
    args = parser.parse_args()

    font = read(args.font)
    glyphs = font["glyphs"]
    by_codepoint = {glyph["codepoint"]: glyph for glyph in glyphs}
    print(f"{args.font}: {font['size']} bytes, {len(glyphs)} glyphs, "
          f"{font['points']} points, {font['size'] // max(1, len(glyphs))} bytes each")

    codepoints = sorted(by_codepoint)
    ascii_have = sum(1 for c in range(0x20, 0x7F) if c in by_codepoint)
    print(f"  codepoints {codepoints[0]:#x}..{codepoints[-1]:#x}, "
          f"printable ASCII {ascii_have}/95, "
          f"degree sign {'yes' if 0xB0 in by_codepoint else 'NO'}")

    tall = max(glyphs, key=lambda g: g["bbox_h"])
    reach = max((max((max(abs(x), abs(y))
                      for x, y in zip(g["points"][0::2], g["points"][1::2], strict=True)),
                     default=0)
                 for g in glyphs), default=0)
    print(f"  {'wide' if font['wide'] else 'narrow'}, {font['units_per_em']} units per em")
    print(f"  tallest {chr(tall['codepoint'])!r} at {tall['bbox_h']}, "
          f"furthest point {reach} of {WIDE_COORD_MAX if font['wide'] else COORD_MAX}")

    suspect = [g for g in glyphs
               if g["contours"] and g["advance"] < g["bbox_w"] * 0.5]
    if suspect:
        print(f"  {len(suspect)} glyphs carry more ink than advance, which is what a "
              f"units mix-up looks like: "
              + " ".join(repr(chr(g["codepoint"])) for g in suspect[:12]))

    show = codepoints if args.all else [ord(c) for c in args.chars]
    print(f"\n  {'char':<6} {'cp':>5} {'bbox x':>7} {'y':>4} {'w':>4} {'h':>4} "
          f"{'adv':>4} {'contours':>9}")
    for codepoint in show:
        glyph = by_codepoint.get(codepoint)
        if glyph is None:
            print(f"  {chr(codepoint)!r:<6} not in this font")
            continue
        print(f"  {chr(codepoint)!r:<6} {codepoint:5d} {glyph['bbox_x']:7d} "
              f"{glyph['bbox_y']:4d} {glyph['bbox_w']:4d} {glyph['bbox_h']:4d} "
              f"{glyph['advance']:4d} {glyph['contours']:9d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
