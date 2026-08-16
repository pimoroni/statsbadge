"""The .af container, as alright-fonts writes it and the firmware reads it.

    (a library: tools/make_icon_font.py, make_text_font.py and read_af.py import it)

One definition of the layout, because an encoder and a decoder that each declare it can
disagree without either one failing: a font packs, loads, and draws wrong. `pack` and
`unpack` are held to each other by a round trip in tests/test_tools.py.

A four-byte marker, flags, then counts of glyphs, contours and points, each big-endian
u16. Then the glyph table, the length of every contour, and finally the points.

The glyph fields follow MonaSans-Medium.af, read out of it and not assumed. Points are
y-down from the baseline, so a glyph above it has negative y. bbox_y is y-up and goes
negative only for a descender. x starts at the left of the advance, ink offset by the side
bearing.
"""

import struct

AF_MAGIC = b"af!?"
AF_FLAG_16BIT_POINT_COUNT = 0b0000001
# A wide font stores its bbox, advance and points as 16-bit, then a u16 units-per-em
# after the counts. A narrow one gets the whole em in a signed byte, which is
# what shows as stepped outlines on a glyph drawn a hundred pixels tall.
AF_FLAG_WIDE = 0b0000010
HEADER = ">HHHH"                   # flags, glyphs, contours, points, after the marker
GLYPH_STRUCT = ">HbbBBBB"          # codepoint, then bbox x y w h, advance, contour count
GLYPH_STRUCT_WIDE = ">HhhHHHB"
# A narrow font's em, against which a wide font records its grid.
NARROW_UNITS_PER_EM = 128

# Coordinates and the advance are signed bytes. An advance over 127 reads as negative and
# the glyphs draw on top of each other, which limits the box an icon fills. A wide font
# gets 16 bits for both.
COORD_MIN, COORD_MAX = -128, 127
WIDE_COORD_MIN, WIDE_COORD_MAX = -32768, 32767
MAX_CODEPOINT = 0xFFFF

GLYPH_FIELDS = ("codepoint", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "advance", "contours")


class Glyph:
    """What `pack` takes: a codepoint, its contours, and where the ink sits in the advance."""

    def __init__(self, codepoint):
        self.codepoint = codepoint
        self.contours = []
        self.advance = 0
        self.bbox_x = self.bbox_y = self.bbox_w = self.bbox_h = 0


def limits(wide):
    """The range a coordinate or an advance fits into, with the extent's ceiling."""
    if wide:
        return WIDE_COORD_MIN, WIDE_COORD_MAX, 0xFFFF
    return COORD_MIN, COORD_MAX, 255


def clamp(value, low=COORD_MIN, high=COORD_MAX):
    return max(low, min(high, int(value)))


def out_of_range(glyphs, wide=False):
    """Names of glyphs with points outside the integer the format packs them into."""
    low, high, _extent = limits(wide)
    over = []
    for glyph in glyphs:
        for contour in glyph.contours:
            if any(not low <= x <= high or not low <= y <= high for x, y in contour):
                over.append(glyph.codepoint)
                break
    return over


def pack(glyphs, units_per_em=None):
    """The .af file: header, glyph table, contour lengths, then points.

    Pass units_per_em to write a wide font, whose bbox, advance and points are 16-bit and
    whose em is whatever the caller built the glyphs to. Without it the font is narrow,
    every coordinate is a signed byte and the em is 128 by convention.
    """
    for glyph in glyphs:
        if glyph.codepoint > MAX_CODEPOINT:
            raise SystemExit(
                f"codepoint {glyph.codepoint:x} does not fit the format, which stores "
                "them as u16. Remap it to an ASCII character with a third field in "
                "icons.txt.")

    wide = units_per_em is not None
    if wide and not 1 <= units_per_em <= 0xFFFF:
        raise SystemExit(f"units per em {units_per_em} does not fit the format's u16")
    low, high, extent_high = limits(wide)

    contours = sum(len(glyph.contours) for glyph in glyphs)
    points = sum(len(contour) for glyph in glyphs for contour in glyph.contours)
    flags = AF_FLAG_16BIT_POINT_COUNT | (AF_FLAG_WIDE if wide else 0)
    out = bytearray(AF_MAGIC)
    out += struct.pack(HEADER, flags, len(glyphs), contours, points)
    if wide:
        out += struct.pack(">H", units_per_em)

    glyph_struct = GLYPH_STRUCT_WIDE if wide else GLYPH_STRUCT
    for glyph in glyphs:
        out += struct.pack(glyph_struct, glyph.codepoint,
                           clamp(glyph.bbox_x, low, high), clamp(glyph.bbox_y, low, high),
                           clamp(glyph.bbox_w, 0, extent_high),
                           clamp(glyph.bbox_h, 0, extent_high),
                           clamp(glyph.advance, 0, extent_high), len(glyph.contours))
    for glyph in glyphs:
        for contour in glyph.contours:
            if len(contour) > 0xFFFF:
                raise SystemExit(f"contour of {len(contour)} points is too long to pack")
            out += struct.pack(">H", len(contour))
    point_struct = ">hh" if wide else ">bb"
    for glyph in glyphs:
        for contour in glyph.contours:
            for x, y in contour:
                out += struct.pack(point_struct, clamp(x, low, high), clamp(y, low, high))
    return bytes(out)


def unpack(data, name="<bytes>"):
    """A packed font as data: its glyphs, their points, and the grid they sit on."""
    if data[:4] != AF_MAGIC:
        raise SystemExit(f"{name} does not start with {AF_MAGIC!r}")
    flags, glyph_count, contour_count, point_count = struct.unpack_from(HEADER, data, 4)
    at = 4 + struct.calcsize(HEADER)

    wide = bool(flags & AF_FLAG_WIDE)
    units_per_em = NARROW_UNITS_PER_EM
    if wide:
        units_per_em = struct.unpack_from(">H", data, at)[0]
        at += 2

    glyph_struct = GLYPH_STRUCT_WIDE if wide else GLYPH_STRUCT
    glyphs = []
    for _ in range(glyph_count):
        fields = struct.unpack_from(glyph_struct, data, at)
        at += struct.calcsize(glyph_struct)
        glyphs.append(dict(zip(GLYPH_FIELDS, fields, strict=True)))

    lengths = []
    for _ in range(contour_count):
        if flags & AF_FLAG_16BIT_POINT_COUNT:
            lengths.append(struct.unpack_from(">H", data, at)[0])
            at += 2
        else:
            lengths.append(data[at])
            at += 1

    # Points, in order, so a glyph's own extent can be checked against its bbox. Also
    # split back into contours, the form `pack` takes: a font that cannot be read and
    # written again cannot be checked against the one that shipped.
    point_code, point_size = ("h", 4) if wide else ("b", 2)
    index = 0
    for glyph in glyphs:
        spans = lengths[index:index + glyph["contours"]]
        span = sum(spans)
        glyph["points"] = (struct.unpack_from(f">{span * 2}{point_code}", data, at)
                           if span else ())
        outlines, taken = [], 0
        for length in spans:
            flat = glyph["points"][taken * 2:(taken + length) * 2]
            outlines.append(list(zip(flat[0::2], flat[1::2], strict=True)))
            taken += length
        glyph["outlines"] = outlines
        index += glyph["contours"]
        at += span * point_size
    return {"size": len(data), "flags": flags, "glyphs": glyphs, "points": point_count,
            "wide": wide, "units_per_em": units_per_em}


def to_glyphs(font):
    """A font `unpack` returned, back as the `Glyph` objects `pack` takes."""
    made = []
    for found in font["glyphs"]:
        glyph = Glyph(found["codepoint"])
        glyph.contours = [list(contour) for contour in found["outlines"]]
        glyph.advance = found["advance"]
        glyph.bbox_x, glyph.bbox_y = found["bbox_x"], found["bbox_y"]
        glyph.bbox_w, glyph.bbox_h = found["bbox_w"], found["bbox_h"]
        made.append(glyph)
    return made


def read(path):
    with open(path, "rb") as handle:
        return unpack(handle.read(), str(path))
