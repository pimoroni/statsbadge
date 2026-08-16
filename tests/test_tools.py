"""The build-time tools: the icon font corpus, and how a font is packed and read back."""

import os
import struct
import tempfile


def test_a_packed_font_reads_back_the_way_it_was_written():
    """The encoder and the decoder are held to each other here.

    They were once a definition each, in make_icon_font.py and read_af.py. Two that
    disagree do not fail: the font packs, loads, and draws wrong.
    """
    import af

    def glyph(codepoint, contours, **fields):
        made = af.Glyph(codepoint)
        made.contours = contours
        for name, value in fields.items():
            setattr(made, name, value)
        return made

    square = [[(0, 0), (90, 0), (90, -90), (0, -90)]]
    packed = af.pack([glyph(ord("b"), square, bbox_w=90, bbox_h=90, advance=95),
                      glyph(ord(" "), [], advance=25)])

    font = af.unpack(packed)
    assert not font["wide"]
    assert font["units_per_em"] == af.NARROW_UNITS_PER_EM
    letter, space = font["glyphs"]
    assert (letter["codepoint"], letter["bbox_w"], letter["bbox_h"], letter["advance"],
            letter["contours"]) == (ord("b"), 90, 90, 95, 1)
    assert letter["points"] == (0, 0, 90, 0, 90, -90, 0, -90)
    # A space carries an advance and no ink, so the words still separate.
    assert (space["advance"], space["contours"], space["points"]) == (25, 0, ())

    # Wide: 16-bit coordinates and the em in the header, so a cap stands in a finer grid.
    font = af.unpack(af.pack(
        [glyph(ord("H"), [[(0, 0), (600, 0), (600, -648)]],
               bbox_w=600, bbox_h=648, advance=700)], units_per_em=1024))
    assert font["wide"]
    assert font["units_per_em"] == 1024
    cap = font["glyphs"][0]
    assert (cap["bbox_w"], cap["bbox_h"], cap["advance"]) == (600, 648, 700)
    assert cap["points"] == (0, 0, 600, 0, 600, -648)


def test_the_fonts_that_shipped_repack_to_the_same_bytes(repo_root):
    """Every .af in the tree, read and written again, is the file that shipped.

    Holds the encoder to what it wrote before the container became one module, on the wide
    path and the narrow one. Needs neither the fonts group nor a .ttf to build from.
    """
    import af

    fonts = sorted(path for directory in ("src", "extensions")
                   for path in (repo_root / directory).rglob("*.af"))
    assert fonts, "no .af fonts in the tree to check against"

    seen = set()
    for path in fonts:
        original = path.read_bytes()
        font = af.unpack(original, str(path))
        em = font["units_per_em"] if font["wide"] else None
        assert af.pack(af.to_glyphs(font), units_per_em=em) == original, path.name
        seen.add(font["wide"])
    assert seen == {True, False}, "one of the two coordinate widths has no font here"


def test_a_glyph_the_font_format_cannot_hold_is_refused():
    """A malformed corpus line, a point outside a signed byte, and a codepoint past a u16
    are each refused by name."""

    import make_icon_font as tool

    with tempfile.TemporaryDirectory() as work:
        path = os.path.join(work, "icons.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# a comment\n"
                         "\n"
                         "sunny e81a\n"
                         "rainy f176 i    # trailing comment\n")
        assert tool.read_corpus(path) == [("sunny", 0xE81A, None),
                                          ("rainy", 0xF176, ord("i"))]

        for bad, why in (("sunny\n", "one field"),
                         ("sunny nothex\n", "bad codepoint"),
                         ("sunny e81a ab\n", "two-character remap")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(bad)
            try:
                tool.read_corpus(path)
            except SystemExit:
                continue
            raise AssertionError(f"accepted a line with a {why}")

    # Points and the advance are signed bytes, so the caller is told which glyphs are
    # outside that.
    glyph = tool.Glyph(ord("a"))
    glyph.contours = [[(0, 0), (200, -50), (10, -300)]]
    assert tool.out_of_range([glyph]) == [ord("a")]

    ok = tool.Glyph(ord("b"))
    ok.contours = [[(0, 0), (90, 0), (90, -90), (0, -90), (0, 0)]]
    ok.bbox_w = ok.bbox_h = ok.advance = 90
    assert tool.out_of_range([ok]) == []

    blob = tool.pack([ok])
    assert blob[:4] == b"af!?"
    flags, glyphs, contours, points = struct.unpack(">HHHH", blob[4:12])
    assert (glyphs, contours, points) == (1, 1, 5), (glyphs, contours, points)
    codepoint, bx, by, bw, bh, advance, ncontours = struct.unpack(">HbbBBBB", blob[12:20])
    assert (codepoint, advance, ncontours) == (ord("b"), 90, 1)
    # Header, glyph table, one contour length, then the points.
    assert len(blob) == 12 + 8 + 2 + 5 * 2, len(blob)

    # Codepoints are a u16, so a Material Symbol above that has to be remapped in the
    # corpus.
    high = tool.Glyph(0x1FFF0)
    high.contours = [[(0, 0), (10, 0), (10, -10), (0, 0)]]
    try:
        tool.pack([high])
    except SystemExit:
        pass
    else:
        raise AssertionError("packed a codepoint that does not fit")
