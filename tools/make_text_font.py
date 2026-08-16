#!/usr/bin/env python3
"""Build an .af text font from a .ttf or .otf.

    python3 tools/make_text_font.py build/fonts/Lexend-Medium.ttf \\
            --out src/statsbadge/badge_app/fonts/lexend-medium.af

Needs the fonts dependency group: uv sync --group fonts.

The container is tools/af.py's and the contour cleaning is make_icon_font's. What differs
from an icon font is the geometry, and all of it:

  - an icon is fitted to a box and given a made-up advance, because it stands alone. A
    text glyph keeps the font's own advance and side bearing, or the words do not space.
  - an icon scales on whichever axis is tighter. A text font takes one scale for every
    glyph, from the cap height, or the letters do not share a baseline.

The conventions here are not assumed. They are read out of the reference font, which is
MonaSans-Medium.af, by tools/read_af.py:

    H       bbox x 8  y   0  w 70  h 81  advance 88   points x 8..79  y -81..0
    p       bbox x 8  y -18  w 56  h 78  advance 69
    space   bbox 0 0 0 0                 advance 25   no contours

So: a capital stands 81 units, the unit look.py's sizes are given in. Points are
y-down from the baseline, so ink above it is negative. bbox_y is y-up and goes negative
only for a descender. bbox_x is the left side bearing, and x is measured from the pen.

Coordinates and the advance are signed and unsigned bytes, so nothing may exceed 127 and
254 respectively. At a cap height of 81 the reference font's widest glyph reaches 90, which
leaves room; a font whose ascenders or advances are unusually long is reported rather than
wrapped, because a wrapped advance draws every glyph on the spot.

--wide lifts that to 16 bits and records the em in the header, putting the cap in a much
finer grid. A font drawn at a large point size needs that: at a cap of 81 a glyph filling
a 240px screen quantises to steps of nearly two pixels. The default wide cap keeps the
cap-to-em ratio, so a given font_size draws the same height either way.
"""

import argparse
import pathlib
import sys
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from af import (  # noqa: E402
    COORD_MAX, NARROW_UNITS_PER_EM, WIDE_COORD_MAX, Glyph, pack,
)
from make_icon_font import (  # noqa: E402
    Bounds, clean_contours, outline_contours, require_font_tools,
)

# What the badge draws: printable ASCII, the degree sign for a temperature, and the
# Latin-1 letters a hostname or an OS string can arrive with. Not the whole of Latin-1:
# every glyph is bytes on a badge, and the reference font's 310 of them cost 66KB.
def default_codepoints():
    wanted = list(range(0x20, 0x7F))
    wanted.append(0xB0)                                   # degree sign
    wanted += [0xD7, 0xF7]                                # multiply, divide
    for codepoint in range(0xC0, 0x180):                  # accented Latin
        try:
            name = unicodedata.name(chr(codepoint))
        except ValueError:
            continue
        if "LATIN" in name:
            wanted.append(codepoint)
    return sorted(set(wanted))


CAP_HEIGHT = 81           # units a capital stands in the reference font
# --wide packs coordinates as 16-bit, so the cap can stand in a much finer grid.
# Eight times the reference, which keeps the cap-to-em ratio exact (648/1024 ==
# 81/128) so a given font_size draws the same height either way.
WIDE_CAP_HEIGHT = CAP_HEIGHT * 8
# Half a unit, keeping what the simplifier gives up inside the rounding the point grid
# already costs.

# Scaled with --cap: a tolerance means nothing except against the size of the glyph it is
# thinning, and at a high cap a fixed one leaves contours over the 512-point buffer.
QUALITY = 0.5
MAX_ADVANCE = 254
# The glyph renderer converts one contour at a time into a fixed buffer and silently
# skips any that does not fit: the glyph loses a piece, or draws nothing where it had
# one contour.

# The limit is per contour, so four contours of 200 in one glyph are fine.

# Measured with synthetic glyphs, not read off the constant: on a firmware whose buffer
# is 256, 256 points draws and 257 draws zero pixels.

# picovector 39a44c3 raises the buffer to 512, the ceiling here. A badge on an older build
# stops at 256, which is why the longest contour is reported on every build.
MAX_CONTOUR = 512
SAFE_CONTOUR = 256        # what an unraised firmware manages


def cap_scale(face, sample="H", cap=CAP_HEIGHT):
    """Font units per output unit, so that a capital stands `cap`."""
    if face.get_char_index(ord(sample)) == 0:
        raise SystemExit(f"the font has no {sample!r} to measure a cap height from")
    import freetype
    face.load_char(sample, freetype.FT_LOAD_PEDANTIC)
    height = Bounds(face.glyph.outline.get_bbox()).height
    if not height:
        raise SystemExit(f"{sample!r} has no outline to measure")
    return height / cap


def text_glyph(face, codepoint, scale, tolerance):
    """One glyph on the reference font's terms. None if the font has not got it."""
    import freetype
    if face.get_char_index(codepoint) == 0:
        return None
    # The same flags cap_scale measured with. Hinting changes an outline, so a scale taken
    # under one set of flags and glyphs built under another do not share a cap height.
    face.load_char(codepoint, freetype.FT_LOAD_PEDANTIC)

    glyph = Glyph(codepoint)
    # advance.x is in the outline's units, not 26.6. After set_char_size the two agree,
    # and dividing by 64 as well gives every glyph an advance of about one, stacking a
    # line of text in one place.
    glyph.advance = round(face.glyph.advance.x / scale)

    source = Bounds(face.glyph.outline.get_bbox())
    if not source.width or not source.height:
        # A space, or anything else that spaces without drawing.
        return glyph

    contours = [[(x / scale, -y / scale) for x, y in contour]
                for contour in outline_contours(face, scale)]
    contours = clean_contours(contours, tolerance)
    contours = [contour for contour in contours if len(contour) > 2]
    if not contours:
        return glyph

    glyph.contours = [[(round(x), round(y)) for x, y in contour] for contour in contours]
    glyph.bbox_x = round(source.x / scale)
    glyph.bbox_y = round(source.y / scale)
    glyph.bbox_w = round(source.width / scale)
    glyph.bbox_h = round(source.height / scale)
    return glyph


def check(glyphs, wide=False):
    """Anything the container cannot hold or the badge cannot draw with.

    The lower bound on an advance matters as much as the upper one. A units mix-up
    produces a glyph with ink and no advance, which packs and loads perfectly happily,
    then draws every letter of a word in the same place.
    """
    max_coord = WIDE_COORD_MAX if wide else COORD_MAX
    max_advance = 0xFFFF if wide else MAX_ADVANCE
    problems = []
    for glyph in glyphs:
        char = chr(glyph.codepoint)
        if glyph.advance > max_advance:
            problems.append(f"{char!r} advance {glyph.advance} over {max_advance}")
        # Half, not all of it: an accent legitimately overhangs its advance by a few
        # units, where the units mix-up this guards against was out by a factor of sixty.
        if glyph.contours and glyph.advance < glyph.bbox_w * 0.5:
            problems.append(f"{char!r} advance {glyph.advance} against "
                            f"{glyph.bbox_w} of ink")
        for contour in glyph.contours:
            if len(contour) > MAX_CONTOUR:
                problems.append(f"{char!r} has a contour of {len(contour)} points, over "
                                f"{MAX_CONTOUR}: raise --quality until it is under")
                break
            worst = max((max(abs(x), abs(y)) for x, y in contour), default=0)
            if worst > max_coord:
                problems.append(f"{char!r} reaches {worst}, over {max_coord}")
                break
    return problems


def main():
    parser = argparse.ArgumentParser(description="Build an .af text font.")
    parser.add_argument("font", help="a .ttf or .otf to take glyphs from")
    parser.add_argument("--out", required=True, help="output .af")
    parser.add_argument("--quality", type=float,
                        help="simplification tolerance, in output units. 0 keeps every "
                             f"point. Defaults to {QUALITY} at a cap of {CAP_HEIGHT} and "
                             "scales with --cap, since a tolerance is only meaningful "
                             "against the size of the glyph it is thinning")
    parser.add_argument("--weight", type=int, help="variable weight axis, e.g. 500")
    parser.add_argument("--wide", action="store_true",
                        help="pack coordinates as 16-bit and record the em in the header, "
                             f"so the cap can stand in a far finer grid (default {WIDE_CAP_HEIGHT} "
                             f"against {CAP_HEIGHT}). Doubles the point storage, and is what a "
                             "font drawn at a large point size needs: a narrow font's whole em "
                             "is 128 units, so big glyphs quantise visibly")
    parser.add_argument("--cap", type=int,
                        help=f"units a capital stands in the output, where the reference "
                             f"font is {CAP_HEIGHT} (the default, or {WIDE_CAP_HEIGHT} with "
                             f"--wide). Higher is finer; draw.add_font takes the same number "
                             f"so a caller still asks for the size it wants")
    parser.add_argument("--chars",
                        help="only these characters, for a font built for one job")
    parser.add_argument("--cap-from", default="H", metavar="CHAR",
                        help="the character to measure the cap height from, for a face "
                             "that has not got an H (default: H)")
    parser.add_argument("--list", action="store_true",
                        help="report coverage and size, and write nothing")
    args = parser.parse_args()

    require_font_tools()
    import freetype

    face = freetype.Face(args.font)
    face.set_char_size(1000)
    if args.weight is not None:
        try:
            face.set_var_design_coords([args.weight])
        except Exception as exc:  # noqa: BLE001  a static font has no axes
            raise SystemExit(f"--weight needs a variable font: {exc}") from None

    cap = args.cap if args.cap is not None else (WIDE_CAP_HEIGHT if args.wide else CAP_HEIGHT)
    # The em is the cap on the reference font's terms, so the same font_size draws
    # the same height whichever width the font was packed at.
    units_per_em = round(cap * NARROW_UNITS_PER_EM / CAP_HEIGHT) if args.wide else None
    quality = (args.quality if args.quality is not None
               else QUALITY * cap / CAP_HEIGHT)
    scale = cap_scale(face, sample=args.cap_from, cap=cap)
    wanted = ([ord(c) for c in args.chars] if args.chars
              else default_codepoints())
    glyphs, missing = [], []
    for codepoint in wanted:
        glyph = text_glyph(face, codepoint, scale, quality)
        if glyph is None:
            missing.append(codepoint)
            continue
        glyphs.append(glyph)

    # A glyph the container cannot hold is left out, not clamped: a missing ligature is a
    # gap, where a clamped one is drawn wrong every time it appears.
    problems = check(glyphs, wide=args.wide)
    limit = WIDE_COORD_MAX if args.wide else COORD_MAX
    overflowing = {chr(g.codepoint) for g in glyphs
                   if any(max(abs(x), abs(y)) > limit
                          for c in g.contours for x, y in c)}
    if overflowing:
        glyphs = [g for g in glyphs if chr(g.codepoint) not in overflowing]
    blob = pack(glyphs, units_per_em=units_per_em)
    points = sum(len(c) for g in glyphs for c in g.contours)
    print(f"{args.font}")
    print(f"  {'wide, ' if args.wide else ''}cap height {cap} units"
          f"{f' of a {units_per_em} unit em' if args.wide else ''}, "
          f"{scale:.3f} font units per output unit, tolerance {quality:.3f}")
    longest = max((len(c) for g in glyphs for c in g.contours), default=0)
    print(f"  {len(glyphs)} glyphs, {points} points, {len(blob)} bytes "
          f"({len(blob) // max(1, len(glyphs))} each)")
    fits = "fits any build" if longest <= SAFE_CONTOUR else "needs picovector 39a44c3"
    print(f"  longest contour {longest} points of {MAX_CONTOUR}, {fits}")
    if missing:
        print(f"  {len(missing)} not in the font: "
              + " ".join(f"{c:04x}" for c in missing[:16])
              + (" ..." if len(missing) > 16 else ""))
    for problem in problems:
        print(f"  warning: {problem}")
    if overflowing:
        print(f"  left out, too big for the format: {' '.join(sorted(overflowing))}")

    if args.list:
        return 0
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
