"""The build-time tools: the icon font corpus and how it is packed."""

import os
import struct
import tempfile


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
