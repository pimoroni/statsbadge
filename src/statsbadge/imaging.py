"""Turning a picture off the internet into something a badge can hold."""

import struct
import zlib

# Low is a thumbnail beside a message, high a picture with a page to itself. Portrait
# and landscape are the same pixels turned over.
SIZES = {
    ("low", "portrait"): (48, 64),
    ("low", "landscape"): (64, 48),
    ("high", "portrait"): (96, 128),
    ("high", "landscape"): (128, 96),
}

# Greys per preset, which sets the PNG's bit depth. Four colours is two bits; eight has
# to be four, PNG having no three-bit depth.
LEVELS = {"low": 4, "high": 8}
DEPTHS = {4: 2, 8: 4}

# The energy map's long edge. The crop steps by the original divided by this.
ENERGY_LONG = 96

# Ordered dithering, 4x4. Error diffusion at four colours changes completely between two
# nearly identical frames; a Bayer pattern is stable and its texture compresses.
BAYER = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)
BAYER_N = 16

# Thrown away at each end before levelling: a photograph off a feed rarely uses its full
# range, and at four levels that draws two of them.
CLIP_FRACTION = 0.02


class ImagingError(Exception):
    """A picture that could not be turned into a thumbnail, as one line."""


def thumbnail(data, preset="low", orientation="landscape"):
    """Return `data` as an indexed PNG of the chosen preset. Bytes in, bytes out."""
    from PIL import Image

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

    # Greyscale throughout: what travels is a position on a ramp the badge owns.
    grey = source.convert("L")
    grey = grey.crop(_best_crop(grey, width / height))
    grey = grey.resize((width, height), Image.LANCZOS)
    pixels = _levelled(grey.tobytes(), width * height)
    return _png(_dithered(pixels, width, height, levels), width, height, levels)


def _best_crop(grey, aspect):
    """Return the window of `aspect` holding the most going on, as a crop box."""
    from PIL import Image, ImageFilter

    width, height = grey.size
    if width <= 0 or height <= 0:
        raise ImagingError("the image has no pixels")
    # Too wide, so the width is cut and the full height kept.
    if width / height > aspect:
        want_w, want_h = int(round(height * aspect)), height
    else:
        want_w, want_h = width, int(round(width / aspect))
    want_w = max(1, min(width, want_w))
    want_h = max(1, min(height, want_h))
    if (want_w, want_h) == (width, height):
        return (0, 0, width, height)

    # A small copy to score on: which part of a photograph is interesting needs no more
    # than 96px to decide.
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
    """Return where a window of `band` holds the most, as an index into `weights`."""
    band = max(1, min(len(weights), band))
    running = sum(weights[:band])
    best, at = running, 0
    for start in range(1, len(weights) - band + 1):
        running += weights[start + band - 1] - weights[start - 1]
        if running > best:
            best, at = running, start
    return at


def _levelled(pixels, count):
    """Return the picture stretched onto its own range, as a list of 0-255."""
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
    """Return 0-255 brightnesses as `levels` indices, ordered dithered."""
    top = levels - 1
    out = bytearray(width * height)
    for y in range(height):
        row = BAYER[y & 3]
        base = y * width
        for x in range(width):
            # Applied in the gap between two levels, which keeps it a texture and not
            # noise over the whole picture.
            nudged = pixels[base + x] * top / 255.0 + (row[x & 3] / BAYER_N - 0.5)
            index = int(nudged + 0.5)
            out[base + x] = 0 if index < 0 else top if index > top else index
    return out


def _png(indices, width, height, levels):
    """Return an indexed PNG, at the fewest bits a pixel the level count allows."""
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
