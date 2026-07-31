"""Shoot the same pages in every candidate font, on a badge.

    python3 tools/font_shots.py                 copy the fonts, then run the badge side
    mpremote connect PORT mount . run tools/font_shots.py     the badge side alone

Fonts have to be on the badge's own filesystem: `font.load()` on a file under
`mpremote mount` dies in a UnicodeDecodeError partway through, because the mount serves it
as text, and takes the REPL with it. So the host half of this copies them to /fonts first.

Pages rather than a specimen line. A font is chosen for how a reading looks inside a ring
and whether the label under it still reads, which a row of letters does not show.
"""

import os
import sys

# Whatever is in badge_app/fonts, so a candidate is compared by dropping it in there and
# running this. The app's own font is among them and gets shot too, which is the point: a
# candidate is only interesting next to what it would replace.


def copy_to_badge(port=None):
    """Put the fonts where the badge can load them from.

    pathlib and subprocess are imported here rather than at the top: the badge compiles
    this whole module before deciding which half to run, and has neither.
    """
    import pathlib
    import subprocess

    app = pathlib.Path(__file__).resolve().parent.parent / "src" / "statsbadge" / "badge_app"
    fonts = sorted(path.stem for path in (app / "fonts").glob("*.af"))
    if not fonts:
        print("  no fonts in badge_app/fonts, build one with tools/make_text_font.py")
        return
    # Concatenation rather than unpacking into a list literal: the badge compiles this
    # whole module before deciding which half to run, and MicroPython cannot parse [*x, y].
    base = ["python3", "-m", "mpremote"]
    if port:
        base = base + ["connect", port]
    subprocess.run(base + ["fs", "mkdir", "/fonts"], capture_output=True, check=False)
    for name in fonts:
        source = app / "fonts" / f"{name}.af"
        done = subprocess.run(base + ["fs", "cp", str(source), f":/fonts/{name}.af"],
                              capture_output=True, check=False)
        print(f"  {'copied' if done.returncode == 0 else 'FAILED'} {name}")


# -- the badge half ---------------------------------------------------------

def run_on_badge():
    sys.path.insert(0, "/remote/src/statsbadge/badge_app")
    sys.path.insert(0, "/remote/extensions/statsbadge-clock/src/statsbadge_clock/badge")

    import draw
    import look
    import pages as pages_module

    badge.mode(HIRES | VSYNC)                                    # noqa: F821
    screen.antialias = image.X4                                  # noqa: F821
    badge.default_clear = None                                   # noqa: F821
    BUTTON_HOME.irq(None)                                        # noqa: F821
    draw.prepare()

    frame = {
        "v": 1, "seq": 7,
        "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512},
        "mem": {"pct": 71.2, "used_mb": 23330, "total_mb": 32768},
        "gpu": [{"name": "RTX 4070", "pct": 88.0, "temp": 67.0, "power": 182.5}],
        "disk": {"pct": 74.2, "read_bps": 52428800, "write_bps": 8388608},
        "sys": {"host": "workshop-pc", "os": "Windows 11", "arch": "AMD64",
                "cpu_name": "Ryzen 7 7800X3D", "uptime_s": 271830},
        "power": {"battery_pct": 91, "package_w": 44.2},
    }
    page = {"id": "load", "kind": "dials", "title": "Load",
            "fields": ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct"]}
    theme = look.get(look.DEFAULT)

    # Whatever the host half copied over. The app's own font is registered by prepare under
    # look.FONT_NAME, so it is in the registry already and shot alongside the rest.
    candidates = [look.FONT_NAME]
    try:
        for entry in sorted(os.listdir("/fonts")):
            if entry.endswith(".af"):
                name = entry[:-3]
                if draw.add_font(name, "/fonts/" + entry) and name not in candidates:
                    candidates.append(name)
    except OSError:
        print("  nothing in /fonts; run the host half first")

    for name in candidates:
        draw.use_font(name)
        pages_module.render(page, frame, {}, theme, 0, 4, frame["sys"]["host"])
        badge.update()                                           # noqa: F821
        with open(f"/remote/shots/font_{name}.raw", "wb") as handle:
            handle.write(screen.raw)                             # noqa: F821
        print(f"  shot font_{name}")

    print("FONT SHOTS: done")


if __name__ == "__main__":
    if os.uname().sysname == "rp2":                              # noqa: F821
        run_on_badge()
    else:
        copy_to_badge(sys.argv[1] if len(sys.argv) > 1 else None)
        print("now: mpremote connect PORT mount . run tools/font_shots.py")
