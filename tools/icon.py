#!/usr/bin/env python3
"""Draw src/statsbadge/badge_app/icon.png, the 24x24 sprite the launcher shows.

    python3 tools/icon.py

The splash scaled down: the angles and colours come from look.py, the proportions from
splash.py times one scale factor, so the two keep agreeing.
Rendered at 16x and reduced, since there is no anti-aliasing to be had at 24 pixels
otherwise.
"""

import pathlib
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "statsbadge" / "badge_app"
sys.path.insert(0, str(APP))

import look  # noqa: E402
import splash  # noqa: E402  its module-level constants; show() needs a badge

SIZE = 24
SUPERSAMPLE = 16
CORNER = 5
COLOURS = 32                      # 16 also reads fine at this size; 32 leaves margin

OUTER = 11.5                      # leaves a pixel of margin inside the icon
SCALE = OUTER / splash.OUTER

# PicoVector's arc angles and PIL's both run clockwise on screen, but PIL starts at 3
# o'clock where PicoVector starts at 6, so the dial's angles shift by a quarter turn.
PIL_OFFSET = -90


def sector(target, colour, start, end, outer, inner, centre):
    """Paste colour through an annular sector, PIL having no arc with an inner radius."""
    mask = Image.new("L", target.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.pieslice(_box(centre, outer), start, end, fill=255)
    draw.ellipse(_box(centre, inner), fill=0)
    target.paste(colour, mask=mask)


def _box(centre, radius):
    x, y = centre
    return [(x - radius, y - radius), (x + radius, y + radius)]


def main():
    theme = look.get(look.DEFAULT)
    size = SIZE * SUPERSAMPLE
    scale = SCALE * SUPERSAMPLE
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)

    # A plate in the app's own background, so the dial reads against the launcher's brown
    # the same way it does on the badge.
    draw.rounded_rectangle([(0, 0), (size - 1, size - 1)],
                           radius=CORNER * SUPERSAMPLE, fill=theme.bg + (255,))

    centre = (size / 2, size / 2)
    start = look.DIAL_FROM + PIL_OFFSET
    end = look.DIAL_TO + PIL_OFFSET
    sector(icon, theme.grid + (255,), start, end,
           splash.OUTER * scale, splash.INNER * scale, centre)
    sector(icon, theme.accent + (255,), start,
           start + (end - start) * splash.SWEEP,
           splash.OUTER * scale, splash.INNER * scale, centre)

    bar_w = splash.BAR_W * scale
    gap = splash.BAR_GAP * scale
    left = centre[0] - (3 * bar_w + 2 * gap) / 2
    base = centre[1] + splash.BASE_BELOW_CENTRE * scale
    for i, height in enumerate(splash.BAR_HEIGHTS):
        x = left + i * (bar_w + gap)
        draw.rectangle([(x, base - height * scale), (x + bar_w, base)],
                       fill=theme.ink + (255,))

    out = APP / "icon.png"
    small = icon.resize((SIZE, SIZE), Image.LANCZOS)
    small.save(out, compress_level=9)
    print(f"wrote {out.relative_to(ROOT)}, {out.stat().st_size} bytes")
    _shrink(out, small)


def _shrink(out, unquantised):
    """Cut the colour count, which is most of what the file costs.

    pngquant only for the palette it picks: the badge's image.load mis-decodes an indexed
    PNG, returning a short buffer of wrong colours, so the result is written back out as
    RGBA. Fewer distinct colours still compress better, worth about a fifth of the file.
    """
    if not shutil.which("pngquant"):
        print("pngquant not installed; left at full colour")
        return
    before = out.stat().st_size
    quantised = out.with_suffix(".quantised.png")
    try:
        subprocess.run(["pngquant", "--force", "--strip", "--speed", "1", "--nofs",
                        str(COLOURS), "--output", str(quantised), "--", str(out)],
                       check=True)
        with Image.open(quantised) as image_file:
            image_file.convert("RGBA").save(out, compress_level=9)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"pngquant failed ({exc}); left at full colour")
        unquantised.save(out, compress_level=9)
        return
    finally:
        quantised.unlink(missing_ok=True)
    print(f"{COLOURS} colours: {before} -> {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
