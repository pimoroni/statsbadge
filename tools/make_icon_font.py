#!/usr/bin/env python3
"""Build an .af icon font for a vendored extension.

    python3 tools/make_icon_font.py extensions/statsbadge-clock
    python3 tools/make_icon_font.py extensions/statsbadge-clock --weight 500 --list

Needs the fonts dependency group: uv sync --group fonts.

Each extension keeps an `icons.txt` of `name codepoint [ascii]` lines, and this writes
an .af next to its badge module. `statsbadge install` pushes it to the badge with the
rest of the extension, and the badge loads it with font.load().

The third field remaps a symbol onto an ASCII character, so badge-side code can draw it
with an ordinary string: `sunny e81a s` puts the sun at "s".

The encoder is lifted from alright-fonts (afinate on feature/icon-and-font-merge) and
cut down to the icon case. Fixed on the way in:

  - cubic_to passed a float to range(), so any font with cubic outlines - every OTF, and
    CFF-flavoured TTFs - raised TypeError instead of building
  - both curve decompositions stopped one step short of the segment's end point, so
    contours never quite reached it, and a segment short enough to want one step emitted
    only its start
  - a zero-length segment produced no points at all
  - points pack as signed bytes and advance as unsigned, with no range check, so a glyph
    that overflowed raised struct.error from inside the packer
  - codepoints pack as u16, which silently excludes the Material Symbols that live above
    U+FFFF

Comments in the corpus are this tool's own doing; afinate does not take them.
"""

import argparse
import math
import os
import pathlib
import sys
import urllib.request

from af import Glyph, out_of_range, pack

# Imported when a font is actually read, so the corpus parsing and the packing can be
# tested without the fonts group installed.
freetype = shapely = None


def require_font_tools():
    global freetype, shapely
    if freetype is None:
        try:
            import freetype as freetype_module
            import shapely as shapely_module
        except ImportError as exc:
            raise SystemExit(
                f"{exc}. Install the tools with: uv sync --group fonts") from None
        freetype, shapely = freetype_module, shapely_module

# A capital in MonaSans-Medium.af stands 81 units, so this is an icon a little taller
# than the text beside it.
ICON_SIZE = 100
MAX_ICON_SIZE = 127

# Where the tool gets a font if it is not given one. Cached under build/, which is
# ignored: a 10MB variable font does not belong in the repository.
FONT_SOURCES = {
    "outlined": "MaterialSymbolsOutlined%5BFILL%2CGRAD%2Copsz%2Cwght%5D.ttf",
    "rounded": "MaterialSymbolsRounded%5BFILL%2CGRAD%2Copsz%2Cwght%5D.ttf",
    "sharp": "MaterialSymbolsSharp%5BFILL%2CGRAD%2Copsz%2Cwght%5D.ttf",
}
FONT_BASE = ("https://raw.githubusercontent.com/google/material-design-icons/master/"
             "variablefont/")
FONT_CACHE = pathlib.Path("build/fonts")


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def distance(self, other):
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)


class Bounds:
    """A glyph's extent, from a FreeType bbox."""

    def __init__(self, box):
        self.x, self.y, self.x2, self.y2 = box.xMin, box.yMin, box.xMax, box.yMax

    @property
    def width(self):
        return self.x2 - self.x

    @property
    def height(self):
        return self.y2 - self.y


# -- outlines ---------------------------------------------------------------

def outline_contours(face, scale):
    """Decompose the loaded glyph into polylines, in font units.

    FreeType hands back lines and curves; the .af format only has points, so curves are
    flattened here and the redundant points are taken back out by shapely later.

    `scale` is font units per output unit, and only sets how finely curves are cut. A
    step per output unit is already finer than a signed byte can express. Stepping per
    *font* unit costs hundreds of points a glyph that all quantise to the same handful
    of coordinates.
    """
    contours = []

    def steps(*points):
        """How finely to flatten, from the control polygon's length."""
        length = sum(a.distance(b) for a, b in zip(points, points[1:], strict=False))
        return max(1, int(length / scale))

    def move_to(target, _ctx):
        contours.append([(target.x, target.y)])

    def line_to(target, _ctx):
        contours[-1].append((target.x, target.y))

    def conic_to(control, target, _ctx):
        start = Point(*contours[-1][-1])
        control, target = Point(control.x, control.y), Point(target.x, target.y)
        n = steps(start, control, target)
        # From the step after the start up to and including the end point, or the
        # contour stops short of where the next segment begins.
        for i in range(1, n + 1):
            t = i / n
            contours[-1].append((
                (1 - t) ** 2 * start.x + 2 * (1 - t) * t * control.x + t * t * target.x,
                (1 - t) ** 2 * start.y + 2 * (1 - t) * t * control.y + t * t * target.y,
            ))

    def cubic_to(control_a, control_b, target, _ctx):
        start = Point(*contours[-1][-1])
        a, b = Point(control_a.x, control_a.y), Point(control_b.x, control_b.y)
        target = Point(target.x, target.y)
        n = steps(start, a, b, target)
        for i in range(1, n + 1):
            t = i / n
            contours[-1].append((
                (1 - t) ** 3 * start.x + 3 * (1 - t) ** 2 * t * a.x
                + 3 * (1 - t) * t ** 2 * b.x + t ** 3 * target.x,
                (1 - t) ** 3 * start.y + 3 * (1 - t) ** 2 * t * a.y
                + 3 * (1 - t) * t ** 2 * b.y + t ** 3 * target.y,
            ))

    face.glyph.outline.decompose(None, move_to=move_to, line_to=line_to,
                                 conic_to=conic_to, cubic_to=cubic_to)
    return contours


def clean_contours(contours, tolerance):
    """Resolve overlapping and self-intersecting outlines into simple rings.

    Fonts are not obliged to be tidy: contours overlap, wind either way, cross
    themselves, and a renderer that just fills what it is given shows the seams. This
    is alright-fonts' shapely pipeline, which unions anything that genuinely overlaps
    and then takes the rings back out.

    Nesting is not overlapping, which is the point: a counter sits inside its outer
    ring without touching it, so it survives as its own contour and stays a hole.
    """
    rings = [shapely.LinearRing(contour) for contour in contours if len(contour) > 3]
    if not rings:
        return []
    # buffer(0) is the usual trick for making a self-intersecting ring valid.
    polygons = []
    for polygon in shapely.polygons(rings):
        polygon = polygon.buffer(0)
        # buffer(0) can split one ring into several.
        polygons.extend(getattr(polygon, "geoms", None) or [polygon])

    polygons = merge_overlaps(polygons)
    polygons = [p if p.is_valid else p.buffer(0) for p in polygons]
    # Every ring, inner and outer, becomes a contour in its own right.
    polygons = shapely.polygons(shapely.get_rings(polygons))
    if tolerance:
        polygons = shapely.coverage_simplify(polygons, tolerance=tolerance)
    return [shapely.get_coordinates(polygon) for polygon in polygons]


def merge_overlaps(polygons):
    """Union any polygons that partly overlap, until none do."""
    def overlapping_pair(items):
        for i, a in enumerate(items):
            for j, b in enumerate(items):
                if i < j and shapely.overlaps(a, b):
                    return i, j
        return None

    polygons = list(polygons)
    while True:
        pair = overlapping_pair(polygons)
        if pair is None:
            return polygons
        i, j = pair
        polygons[i] = shapely.union(polygons[i], polygons[j])
        polygons.pop(j)


def load_icon(face, codepoint, size, tolerance):
    """One icon, fitted to a `size` box and placed like a text glyph.

    None if the font has no such glyph, or it has no outline.

    The conventions are the reference font's, read out of MonaSans-Medium.af rather than
    assumed: points are y-down from the baseline, so a glyph above the baseline has
    negative y, while bbox_y is y-up and goes negative only for a descender. x starts at
    the left of the advance, ink offset by the side bearing.
    """
    if face.get_char_index(codepoint) == 0:
        return None
    face.load_char(codepoint, freetype.FT_LOAD_PEDANTIC)

    source = Bounds(face.glyph.outline.get_bbox())
    if not source.width or not source.height:
        return None

    # An icon fills its box rather than following the font's own metrics, so it scales on
    # whichever axis is tighter and keeps its aspect ratio.
    scale = max(source.width / size, source.height / size)
    ink_w, ink_h = source.width / scale, source.height / scale
    # Centred in the box on both axes.
    pad_x, pad_y = (size - ink_w) / 2, (size - ink_h) / 2

    # Scaled before cleaning, so the simplification tolerance is in the units the points
    # are finally stored in.
    contours = [[((x - source.x) / scale + pad_x,
                  -((y - source.y) / scale + pad_y)) for x, y in contour]
                for contour in outline_contours(face, scale)]
    contours = clean_contours(contours, tolerance)
    contours = [contour for contour in contours if len(contour) > 2]
    if not contours:
        return None

    glyph = Glyph(codepoint)
    glyph.contours = [[(round(x), round(y)) for x, y in contour] for contour in contours]
    glyph.bbox_x, glyph.bbox_y = round(pad_x), round(pad_y)
    glyph.bbox_w, glyph.bbox_h = round(ink_w), round(ink_h)
    glyph.advance = size
    return glyph


# -- the corpus -------------------------------------------------------------

def read_corpus(path):
    """`name codepoint [ascii]` per line. Blank lines and # comments are skipped."""
    entries = []
    for number, line in enumerate(
            pathlib.Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            raise SystemExit(f"{path}:{number}: want 'name codepoint [ascii]', got {line!r}")
        try:
            codepoint = int(parts[1], 16)
        except ValueError:
            raise SystemExit(
                f"{path}:{number}: {parts[1]!r} is not a hex codepoint") from None
        remap = None
        if len(parts) == 3:
            if len(parts[2]) != 1:
                raise SystemExit(f"{path}:{number}: the remap must be one character")
            remap = ord(parts[2])
        entries.append((parts[0], codepoint, remap))
    if not entries:
        raise SystemExit(f"{path} lists no symbols")
    return entries


# -- fonts ------------------------------------------------------------------

def font_path(explicit, style):
    if explicit:
        return explicit
    name = FONT_SOURCES[style]
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    cached = FONT_CACHE / urllib.parse.unquote(name)
    if not cached.exists():
        print(f"fetching Material Symbols {style} into {cached}")
        # Downloaded beside it and renamed, since a transfer that stops partway leaves a
        # file that exists: every run after it hands FreeType a truncated font, and the
        # only cure is knowing to delete it.
        partial = cached.with_suffix(cached.suffix + ".part")
        try:
            urllib.request.urlretrieve(FONT_BASE + name, partial)
            partial.replace(cached)
        finally:
            partial.unlink(missing_ok=True)
    return str(cached)


def set_axes(face, requested):
    """Set variable axes by name, ignoring any the font does not have.

    Design coordinates, not 16.16: passing fixed point clamps every axis to its maximum.
    """
    try:
        axes = face.get_variation_info().axes
    except Exception:                      # noqa: BLE001  a static font has none
        return {}
    coords, applied = [], {}
    for axis in axes:
        name = axis.name.decode() if isinstance(axis.name, bytes) else axis.name
        key = name.lower().replace("opticalsize", "optical-size")
        value = requested.get(key)
        if value is None:
            coords.append(axis.default // 65536)
            continue
        coords.append(int(value))
        applied[name] = value
    if applied:
        face.set_var_design_coords(coords)
    return applied


# -- putting it together ----------------------------------------------------

def build(font, entries, size, tolerance, axes, quiet=False):
    require_font_tools()
    face = freetype.Face(font)
    face.set_char_size(64 * 64)
    applied = set_axes(face, axes)
    if applied and not quiet:
        print("  axes: " + ", ".join(f"{k}={v}" for k, v in applied.items()))

    glyphs, missing = [], []
    for name, codepoint, remap in entries:
        glyph = load_icon(face, codepoint, size, tolerance)
        if glyph is None:
            missing.append(name)
            continue
        if remap is not None:
            glyph.codepoint = remap
        if not quiet:
            shown = f" as {chr(remap)!r}" if remap is not None else ""
            print(f"  {name:<24} {codepoint:04x}{shown:<8} "
                  f"{len(glyph.contours):>2} contours, "
                  f"{sum(len(c) for c in glyph.contours):>4} points")
        glyphs.append(glyph)
    return glyphs, missing


def default_output(extension):
    """An extension's badge directory, which the installer pushes."""
    found = sorted(pathlib.Path(extension).glob("src/*/badge"))
    if not found:
        raise SystemExit(f"no src/*/badge directory under {extension}")
    return found[0] / "icons.af"


def write_web(font, entries, out):
    """The same corpus as a woff2, for the config UI.

    The preview draws the badge's pages, so it draws the badge's symbols. Built from the
    corpus and source font the .af came from, so a second hand-kept list cannot drift.

    Needs fonttools, which the fonts dependency group brings in for the .af anyway.
    """
    try:
        from fontTools import subset
    except ImportError:
        raise SystemExit("--web needs fonttools: uv sync --group fonts") from None
    points = ",".join(f"U+{codepoint:04X}" for _name, codepoint, _remap in entries)
    subset.main([str(font), f"--unicodes={points}", "--flavor=woff2",
                 "--layout-features=", "--no-hinting", "--desubroutinize",
                 f"--output-file={out}"])
    size = pathlib.Path(out).stat().st_size
    print(f"wrote {out}, {size} bytes for {len(entries)} icons")


def main():
    parser = argparse.ArgumentParser(
        description="Build an .af icon font for a vendored extension.")
    parser.add_argument("extension", nargs="?",
                        help="the extension directory, holding icons.txt")
    parser.add_argument("--corpus", help="symbol list (default: EXTENSION/icons.txt)")
    parser.add_argument("--out", help="output .af (default: EXTENSION/src/*/badge/icons.af)")
    parser.add_argument("--font", help="a .ttf or .otf to take glyphs from, instead of "
                                       "fetching Material Symbols")
    parser.add_argument("--style", default="outlined", choices=sorted(FONT_SOURCES),
                        help="which Material Symbols to fetch (default: outlined)")
    parser.add_argument("--size", type=int, default=ICON_SIZE,
                        help=f"box each icon is fitted to, in the same units as the text "
                             f"font where a capital is 81 (default: {ICON_SIZE}, "
                             f"maximum {MAX_ICON_SIZE})")
    parser.add_argument("--quality", type=float, default=0.75,
                        help="simplification tolerance, in output units of 254. 0 keeps\n"
                             "every point (default: 0.75)")
    parser.add_argument("--weight", type=int, help="variable weight axis, e.g. 400")
    parser.add_argument("--fill", type=int, help="variable fill axis, 0 or 1")
    parser.add_argument("--grade", type=int, help="variable grade axis")
    parser.add_argument("--optical-size", type=int, help="variable optical size axis")
    parser.add_argument("--list", action="store_true",
                        help="show what would be built, and write nothing")
    parser.add_argument("--web", metavar="OUT.woff2",
                        help="also subset the same corpus to a woff2, which the config "
                             "UI's preview draws the badge's symbols with")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.extension and not (args.corpus and args.out):
        parser.error("give an extension directory, or both --corpus and --out")
    corpus = args.corpus or os.path.join(args.extension, "icons.txt")
    if not os.path.exists(corpus):
        raise SystemExit(f"no symbol list at {corpus}")
    entries = read_corpus(corpus)

    if args.list:
        for name, codepoint, remap in entries:
            print(f"{name} {codepoint:04x}" + (f" {chr(remap)}" if remap else ""))
        print(f"{len(entries)} symbols in {corpus}")
        return 0

    if not 1 <= args.size <= MAX_ICON_SIZE:
        raise SystemExit(f"--size must be 1 to {MAX_ICON_SIZE}: the advance is stored in "
                         "a signed byte, and a larger one draws every glyph on the spot")

    font = font_path(args.font, args.style)
    axes = {"weight": args.weight, "fill": args.fill, "grad": args.grade,
            "optical-size": args.optical_size}
    axes = {key: value for key, value in axes.items() if value is not None}

    if not args.quiet:
        print(f"{len(entries)} symbols from {font}")
    glyphs, missing = build(font, entries, args.size, args.quality, axes, args.quiet)
    if not glyphs:
        raise SystemExit("no glyphs were built")
    if missing:
        print(f"warning: not in this font, skipped: {', '.join(missing)}")
    over = out_of_range(glyphs)
    if over:
        print("warning: clamped to the coordinate range: "
              + ", ".join(f"{c:04x}" for c in over))

    blob = pack(glyphs)
    out = pathlib.Path(args.out) if args.out else default_output(args.extension)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"wrote {out}, {len(blob)} bytes for {len(glyphs)} icons "
          f"({len(blob) // len(glyphs)} bytes each)")
    if args.web:
        write_web(font, entries, args.web)
    return 0


if __name__ == "__main__":
    sys.exit(main())
