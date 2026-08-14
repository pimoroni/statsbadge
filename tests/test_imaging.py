"""Turning a fetched picture into something the badge can draw."""

import io
import struct


def test_a_picture_is_cropped_to_what_is_in_it():
    """A feed's picture, small enough to send and indexed so a theme can own it.

    Indices on a ramp the badge assigns travel, and not colours, so one image suits
    every badge whatever theme it is on, and the host never has to know which.
    """
    from PIL import Image

    from statsbadge import imaging

    # A wide picture with everything happening down the right-hand end. A crop by the middle
    # would take the flat grey, which on a photograph is the wall behind the subject.
    source = Image.new("L", (400, 100), 128)
    for x in range(320, 400):
        for y in range(0, 100, 2):
            source.putpixel((x, y), 255 if x % 2 else 0)
    box = imaging._best_crop(source, 1.0)
    assert box[2] - box[0] == 100 and box[3] - box[1] == 100, box
    assert box[0] > 250, f"the crop missed what was in the picture: {box}"

    raw = io.BytesIO()
    source.save(raw, format="PNG")
    data = raw.getvalue()

    for (preset, orientation), (width, height) in imaging.SIZES.items():
        png = imaging.thumbnail(data, preset, orientation)
        assert png.startswith(b"\x89PNG\r\n\x1a\n"), preset
        got_w, got_h, depth, colour = struct.unpack(">IIBB", png[16:26])
        assert (got_w, got_h) == (width, height), (preset, orientation, got_w, got_h)
        assert colour == 3, "not an indexed PNG"
        # Two bits a pixel at four colours, which is the quarter-size the point of this,
        # and four at eight - PNG has no three-bit depth.
        assert depth == imaging.DEPTHS[imaging.LEVELS[preset]], (preset, depth)
        assert b"PLTE" in png

        # Pillow reads back what was written, every index landing inside the palette
        back = Image.open(io.BytesIO(png))
        assert back.mode == "P" and back.size == (width, height), (back.mode, back.size)
        assert max(back.tobytes()) < imaging.LEVELS[preset], "an index past the ramp"

    # An unreadable one is a line somebody can act on, in place of a traceback out of Pillow
    try:
        imaging.thumbnail(b"not a picture at all")
    except imaging.ImagingError as exc:
        assert "cannot read" in str(exc), exc
    else:
        raise AssertionError("anything at all was accepted as a picture")
