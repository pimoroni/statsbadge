#!/usr/bin/env python3
"""Draw the app's mark as a PNG: the 24x24 sprite the launcher shows, and a larger copy
for a browser tab, since Safari ignores an SVG favicon.

    python3 tools/icon.py

The splash scaled down. Angles and colours come from look.py, proportions from splash.py
times one scale factor, which keeps the two agreeing. Rendered at 16x and reduced, since
there is no anti-aliasing to be had at 24 pixels otherwise.
"""

import pathlib
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

import badgefakes

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "statsbadge" / "badge_app"
WEB = ROOT / "src" / "statsbadge" / "web"
TRAY = ROOT / "src" / "statsbadge" / "tray" / "assets"
# The packaged app's icon, which is not shipped in the wheel: briefcase reads it from here
# and builds the .icns and .ico each platform needs.
APP_ICONS = ROOT / "packaging" / "icons"
sys.path.insert(0, str(APP))

badgefakes.install()

import look  # noqa: E402
import splash  # noqa: E402  its module-level constants; show() needs a badge

SIZE = 24
WEB_SIZE = 64                     # a tab asks for 32, and twice that for a dense screen
SUPERSAMPLE = 16
CORNER = 5
COLOURS = 32                      # 16 also reads fine at this size; 32 leaves margin

TRAY_SIZE = 44                    # every tray scales down from one square
ICO_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (256, 256))
APP_SIZE = 1024                   # what an .icns holds at its largest
APP_INSET = 0.78                  # of the plate, so the mark sits in it as a Dock icon does
CLEAR = (0, 0, 0, 0)
MONO_INK = (0, 0, 0, 255)
MONO_GRID = (0, 0, 0, 90)         # a template carries shading in its alpha
DOT_R = 0.19                      # of the icon's width
DOT_RING = 0.055

OUTER = 11.5                      # leaves a pixel of margin inside the icon
SCALE = OUTER / splash.OUTER

# PicoVector's arc angles and PIL's both run clockwise on screen, but PIL starts at 3
# o'clock where PicoVector starts at 6, putting a quarter turn between the two.
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


def render(px, grid, accent, ink, plate=None, dot=None, ring=None, supersample=SUPERSAMPLE):
    """The mark at px, drawn large and reduced."""
    size = SIZE * supersample
    scale = SCALE * supersample
    icon = Image.new("RGBA", (size, size), CLEAR)
    draw = ImageDraw.Draw(icon)

    # A plate in the app's own background, so the dial reads against the launcher's brown
    # the same way it does on the badge.
    if plate:
        draw.rounded_rectangle([(0, 0), (size - 1, size - 1)],
                               radius=CORNER * supersample, fill=plate)

    centre = (size / 2, size / 2)
    start = look.DIAL_FROM + PIL_OFFSET
    end = look.DIAL_TO + PIL_OFFSET
    sector(icon, grid, start, end, splash.OUTER * scale, splash.INNER * scale, centre)
    sector(icon, accent, start, start + (end - start) * splash.SWEEP,
           splash.OUTER * scale, splash.INNER * scale, centre)

    bar_w = splash.BAR_W * scale
    gap = splash.BAR_GAP * scale
    left = centre[0] - (3 * bar_w + 2 * gap) / 2
    base = centre[1] + splash.BASE_BELOW_CENTRE * scale
    for i, height in enumerate(splash.BAR_HEIGHTS):
        x = left + i * (bar_w + gap)
        draw.rectangle([(x, base - height * scale), (x + bar_w, base)], fill=ink)

    if dot:
        _dot(draw, size, dot, ring)
    return icon.resize((px, px), Image.LANCZOS)


def _dot(draw, size, fill, ring):
    """A badge is waiting. Cut clear of the dial under it, to read at menu bar size."""
    radius = size * DOT_R
    edge = size * DOT_RING
    centre = (size - radius - edge, radius + edge)
    if ring is not None:
        draw.ellipse(_box(centre, radius + edge), fill=ring)
    draw.ellipse(_box(centre, radius), fill=fill)


def main():
    theme = look.get(look.DEFAULT)
    # PIL takes a tuple of channels; a theme holds the badge's colour objects.
    plate = theme.bg.parts()
    colours = (theme.grid.parts(), theme.accent.parts(), theme.ink.parts())

    for out, px in ((APP / "icon.png", SIZE), (WEB / "icon.png", WEB_SIZE)):
        small = render(px, *colours, plate=plate)
        small.save(out, compress_level=9)
        print(f"wrote {out.relative_to(ROOT)}, {out.stat().st_size} bytes")
        _shrink(out, small)

    TRAY.mkdir(parents=True, exist_ok=True)
    for name, dot in (("tray", None), ("tray-attention", theme.accent.parts())):
        _write(TRAY / f"{name}.png",
               render(TRAY_SIZE, *colours, plate=plate, dot=dot, ring=plate))
    for name, dot in (("tray-template", None), ("tray-template-attention", MONO_INK)):
        _write(TRAY / f"{name}.png",
               render(TRAY_SIZE, MONO_GRID, MONO_INK, MONO_INK, dot=dot, ring=CLEAR))

    # Drawn at twice the size wanted and reduced, so this is not the 24 pixel mark
    # enlarged. macOS reads the .icns and Windows the .ico; briefcase falls back to its
    # own mascot for a format it cannot find, noted in one line nobody reads.
    APP_ICONS.mkdir(parents=True, exist_ok=True)
    inner = round(APP_SIZE * APP_INSET)
    art = Image.new("RGBA", (APP_SIZE, APP_SIZE), CLEAR)
    ImageDraw.Draw(art).rounded_rectangle(
        [(0, 0), (APP_SIZE - 1, APP_SIZE - 1)],
        radius=round(APP_SIZE * CORNER / SIZE), fill=plate)
    art.alpha_composite(
        render(inner, *colours, supersample=max(SUPERSAMPLE, -(-inner * 2 // SIZE))),
        ((APP_SIZE - inner) // 2, (APP_SIZE - inner) // 2))
    for out in (APP_ICONS / "icon.icns", APP_ICONS / "icon.ico"):
        art.save(out, **({"sizes": ICO_SIZES} if out.suffix == ".ico" else {}))
        print(f"wrote {out.relative_to(ROOT)}, {out.stat().st_size} bytes")

    out = TRAY / "statsbadge.ico"
    render(ICO_SIZES[-1][0], *colours, plate=plate, supersample=SUPERSAMPLE * 2).save(
        out, sizes=ICO_SIZES)
    print(f"wrote {out.relative_to(ROOT)}, {out.stat().st_size} bytes")


def _write(out, image):
    image.save(out, compress_level=9)
    print(f"wrote {out.relative_to(ROOT)}, {out.stat().st_size} bytes")


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
