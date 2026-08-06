"""Turning a picture off the internet into something a badge can hold and a theme can own.

A source with an image - a post's attachment, an article's lead picture - hands the bytes
here and gets back a small indexed PNG. Opinionated on purpose, because the badge is 320x240
and every choice a caller could make here is one they would have to make well:

    a content weighted crop, so the subject survives being cut to a fixed shape
    levelled and ordered-dithered, which is what makes four colours read as a picture
    an indexed PNG at 2 or 4 bits a pixel, which is what makes it small enough to send

**What travels is indices, not colours.** The palette written into the file is a grey ramp,
and the badge assigns its own theme's ramp over the top - `img.palette[0:n] = ramp`
recolours every pixel indexing it in one write. So one image suits every badge and every
theme, the host never has to know which theme a badge is on, and a picture arrives in the
same colours as the page around it. A source that quantised to a theme's colours here would
be sending the wrong ones to the second badge.

Pillow does the decoding, which is the one part not worth writing: a JPEG decoder is not a
weekend. It is an extra rather than a dependency - `statsbadge[images]` - so a host that
never shows a picture does not carry it, and `available()` is what a source asks before
offering one.
"""

import struct
import zlib

# The two presets, each way up. Low is a thumbnail beside a message, high is a picture with a
# page to itself; portrait and landscape are the same pixels turned over, so a caller picks
# by what the space is rather than by what the source image happens to be.
SIZES = {
    ("low", "portrait"): (48, 64),
    ("low", "landscape"): (64, 48),
    ("high", "portrait"): (96, 128),
    ("high", "landscape"): (128, 96),
}

# How many greys each preset resolves to, and so how many bits a pixel the PNG needs. Four
# colours is two bits, a quarter of a byte a pixel; eight has to be four, PNG having no
# three-bit depth, so high resolution costs twice per pixel as well as four times the pixels.
LEVELS = {"low": 4, "high": 8}
DEPTHS = {4: 2, 8: 4}

# The energy map is built at this size on its long edge. The crop it picks moves in steps of
# the original divided by this, which is finer than anything the eye will judge on a 64px
# thumbnail, and it keeps the search off a full-size image.
ENERGY_LONG = 96

# Ordered dithering, 4x4. Ordered rather than diffused because the point is a picture that
# reads at four colours: error diffusion at that depth is a field of noise that changes
# completely between two nearly identical frames, where a Bayer pattern is stable, obviously
# deliberate, and compresses - the repeating texture is what keeps the PNG small.
BAYER = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)
BAYER_N = 16

# How much of the histogram is thrown away at each end before levelling. A photograph off a
# feed is rarely using its whole range, and at four levels an unlevelled picture is two of
# them. Enough to ignore a specular highlight, not enough to blow out a face.
CLIP_FRACTION = 0.02


class ImagingError(Exception):
    """A picture that could not be turned into a thumbnail, as one line."""


def available():
    """Whether this host can process a picture at all.

    Asked by a source before it offers one: without the extra there is no decoder, and a
    feed's images should be quietly absent rather than a fault on every fetch.
    """
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def thumbnail(data, preset="low", orientation="landscape"):
    """`data` as an indexed PNG of the chosen preset. Bytes in, bytes out.

    Raises `ImagingError` for anything that is not a picture this can read, which includes
    the extra not being installed - a caller that wants to degrade quietly asks `available()`
    first and does not offer an image at all.
    """
    try:
        from PIL import Image
    except ImportError:
        raise ImagingError(
            "install statsbadge[images] to show pictures - Pillow does the decoding"
        ) from None

    if (preset, orientation) not in SIZES:
        raise ImagingError(f"no such size: {preset} {orientation}")
    width, height = SIZES[(preset, orientation)]
    levels = LEVELS[preset]

    try:
        import io

        source = Image.open(io.BytesIO(data))
        source.load()
    except Exception as exc:
        raise ImagingError(f"cannot read the image: {exc}") from exc

    # Greyscale first and throughout: what travels is a position on a ramp the badge owns,
    # so the colours in the original are only ever a route to a brightness.
    grey = source.convert("L")
    grey = grey.crop(_best_crop(grey, width / height))
    grey = grey.resize((width, height), Image.LANCZOS)
    pixels = _levelled(grey.tobytes(), width * height)
    return _png(_dithered(pixels, width, height, levels), width, height, levels)


def _best_crop(grey, aspect):
    """The window of `aspect` holding the most going on, as a crop box.

    A picture cut to a fixed shape by the middle loses whatever was not in the middle, which
    on a photograph is usually the subject. Edges are the cheapest stand-in for interest
    there is, and integrating them over every candidate window is one pass with a prefix sum
    rather than a search.
    """
    from PIL import Image, ImageFilter

    width, height = grey.size
    if width <= 0 or height <= 0:
        raise ImagingError("the image has no pixels")
    # Wider than what is wanted means the width is what gets cut, and the full height is
    # kept. Backwards, this asks for a window bigger than the picture, which clamps to the
    # whole of it and crops nothing at all.
    if width / height > aspect:
        want_w, want_h = int(round(height * aspect)), height
    else:
        want_w, want_h = width, int(round(width / aspect))
    want_w = max(1, min(width, want_w))
    want_h = max(1, min(height, want_h))
    if (want_w, want_h) == (width, height):
        return (0, 0, width, height)

    # A small copy to score on: the crop moves in steps of the original divided by this, and
    # nothing about which part of a photograph is interesting needs more than 96px to decide.
    scale = ENERGY_LONG / float(max(width, height))
    small = grey.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                        Image.BILINEAR).filter(ImageFilter.FIND_EDGES)
    energy = small.tobytes()
    small_w, small_h = small.size

    if want_w == width:                    # cutting top and bottom
        rows = [sum(energy[y * small_w:(y + 1) * small_w]) for y in range(small_h)]
        band = max(1, int(round(want_h * scale)))
        top = _densest(rows, band) / scale
        top = int(max(0, min(height - want_h, round(top))))
        return (0, top, width, top + want_h)

    columns = [sum(energy[y * small_w + x] for y in range(small_h))
               for x in range(small_w)]
    band = max(1, int(round(want_w * scale)))
    left = _densest(columns, band) / scale
    left = int(max(0, min(width - want_w, round(left))))
    return (left, 0, left + want_w, height)


def _densest(weights, band):
    """Where a window of `band` holds the most, as an index into `weights`."""
    band = max(1, min(len(weights), band))
    running = sum(weights[:band])
    best, at = running, 0
    for start in range(1, len(weights) - band + 1):
        running += weights[start + band - 1] - weights[start - 1]
        if running > best:
            best, at = running, start
    return at


def _levelled(pixels, count):
    """The picture stretched onto its own range, as a list of 0-255.

    A photograph off a feed rarely uses the whole range, and at four levels one that does not
    is a picture in two of them. The ends are clipped first so a specular highlight does not
    define white on its own.
    """
    if not count:
        return []
    histogram = [0] * 256
    for value in pixels:
        histogram[value] += 1
    drop = int(count * CLIP_FRACTION)
    low, high, seen = 0, 255, 0
    for value in range(256):
        seen += histogram[value]
        if seen > drop:
            low = value
            break
    seen = 0
    for value in range(255, -1, -1):
        seen += histogram[value]
        if seen > drop:
            high = value
            break
    if high <= low:
        return list(pixels)
    span = float(high - low)
    return [0 if value <= low else 255 if value >= high
            else int((value - low) * 255.0 / span) for value in pixels]


def _dithered(pixels, width, height, levels):
    """0-255 brightnesses as `levels` indices, ordered dithered.

    The threshold moves with the position rather than with the error so far, so the same
    picture always comes out the same way and two frames of nearly the same picture do not
    differ everywhere - which matters when the result travels only when it changes.
    """
    top = levels - 1
    out = bytearray(width * height)
    for y in range(height):
        row = BAYER[y & 3]
        base = y * width
        for x in range(width):
            # The dither is applied in the gap between two levels, which is what keeps it a
            # texture rather than noise laid over the whole picture.
            nudged = pixels[base + x] * top / 255.0 + (row[x & 3] / BAYER_N - 0.5)
            index = int(nudged + 0.5)
            out[base + x] = 0 if index < 0 else top if index > top else index
    return out


def _png(indices, width, height, levels):
    """An indexed PNG, at the fewest bits a pixel the level count allows.

    The palette is a grey ramp and is not the point: the badge assigns its theme's over the
    top. It is written evenly spaced so the file is a picture in its own right - a preview in
    a browser, or a look at what was actually sent.
    """
    depth = DEPTHS[levels]
    per_byte = 8 // depth
    rows = bytearray()
    for y in range(height):
        rows.append(0)                      # filter: none, the rows being tiny already
        packed, held, count = bytearray(), 0, 0
        for x in range(width):
            held = (held << depth) | indices[y * width + x]
            count += 1
            if count == per_byte:
                packed.append(held)
                held, count = 0, 0
        if count:
            packed.append(held << (depth * (per_byte - count)))
        rows += packed

    ramp = bytearray()
    for level in range(levels):
        grey = level * 255 // (levels - 1)
        ramp += bytes((grey, grey, grey))

    def chunk(tag, body):
        block = tag + body
        return (struct.pack(">I", len(body)) + block
                + struct.pack(">I", zlib.crc32(block) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, 3, 0, 0, 0))
            + chunk(b"PLTE", bytes(ramp))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))
