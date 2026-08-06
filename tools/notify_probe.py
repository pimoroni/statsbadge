"""Draw the notifications page on a badge, with and without pictures, and dump the frames.

    python3 tools/image_themes.py --cards            # writes build/probe_pictures.py
    mpremote connect PORT mount . run tools/notify_probe.py
    python3 tools/shots.py build/shots               # the frames, as PNGs

Mount the repo root: frames go to /remote/build/shots. The pictures come from
build/probe_pictures.py, which is generated - a thumbnail is bytes, and bytes do not belong
in a source file.

What this is for is the half that cannot be checked on the host: an indexed PNG has to decode
on the firmware, its table has to come back the size the bit depth says, and writing the
theme's shades into it has to recolour the picture. Each of those is a print here.
"""

import gc
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")
sys.path.insert(0, "/remote/build")

import draw
import look
import pages as pages_module

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

draw.prepare()

try:
    import probe_pictures
    PICTURES = {"low": probe_pictures.LOW, "high": probe_pictures.HIGH}
    PALETTES = probe_pictures.PALETTES
except ImportError:
    PICTURES, PALETTES = {}, {}
    print("no build/probe_pictures.py - drawing without pictures")

OUT_DIR = "/remote/build/shots"

TEXT = ("All of the above! I inherited my dad's old cameras, my mum taught me how to see "
        "and knit, and friends did the rest")


def item(title, text, age, note=None, picture=None):
    return {"title": title, "text": text, "age_s": age, "note": note, "image": picture}


def frame_with(picture=None):
    return {"feed": {
        "home": item("Maaike", TEXT, 420, "boosted", picture),
        "mention": item("dinkster75", "how did you side load onto the yaber t2? what "
                                      "cable and what commands?", 34200, None, picture),
        "third": item("someone else", "a third message, to squeeze the blocks down to "
                                      "the point a picture has to be cropped", 900,
                      None, picture),
        "followers": 1350, "following": 663, "posts": 6466, "likes": 21,
    }}


def shot(name):
    with open(f"{OUT_DIR}/{name}.raw", "wb") as handle:
        handle.write(screen.raw)


def report(theme):
    """What the firmware makes of each picture, which is the part the host cannot answer."""
    print(f"theme {theme.name}: ramps for {sorted(theme.image)} shades")
    for preset, data in sorted(PICTURES.items()):
        draw.clear_cache()
        img = draw.picture(theme, data)
        if img is None:
            print(f"  {preset}: did not decode")
            continue
        table = img.palette
        entries = len(table) if table else 0
        chosen = draw.shades_for(theme, entries) if table else None
        print(f"  {preset}: {img.width}x{img.height}, table {entries} entries, "
              f"ramp of {len(chosen) if chosen else 0}")
        if table:
            print("     first three: "
                  + str([(table[i].r, table[i].g, table[i].b)
                         for i in range(min(3, entries))]))


PAGES = [
    ("notify_plain", {"id": "n", "kind": "notify", "title": "Mastodon",
                      "fields": ["feed.home", "feed.mention", "feed.followers",
                                 "feed.following", "feed.posts", "feed.likes"]}, None),
    ("notify_picture", {"id": "n", "kind": "notify", "title": "Mastodon",
                        "fields": ["feed.home", "feed.mention", "feed.followers",
                                   "feed.posts"]}, "low"),
    ("notify_picture_large", {"id": "n", "kind": "notify", "title": "Mastodon",
                              "fields": ["feed.home", "feed.followers"]}, "high"),
    # A large picture in a block that cannot hold it: two to a page is 78px against its 96,
    # three is 52, and it used to spill into the message under it.
    ("notify_large_two", {"id": "n", "kind": "notify", "title": "Mastodon",
                          "fields": ["feed.home", "feed.mention", "feed.followers"]},
     "high"),
    ("notify_large_three", {"id": "n", "kind": "notify", "title": "Mastodon",
                            "fields": ["feed.home", "feed.mention", "feed.third",
                                       "feed.followers"]}, "high"),
]

# Built from a palette the host sent rather than `look.get`, which is the one theme the app
# carries to boot with and has no picture ramp: a picture needs a layout to have landed.
THEMES = ("dark", "luminescence", "mono")

for name in THEMES:
    theme = look.from_palette(name, PALETTES.get(name)) or look.get(name)
    report(theme)
    for label, page, preset in PAGES:
        frame = frame_with(PICTURES.get(preset) if preset else None)
        # Three, because a label is drawn live on its first sighting, baked into a sprite
        # on its second and blitted from then on: the middle one is the most expensive
        # frame this page ever has, and the third is what a poll actually costs.
        draw.clear_cache()
        taken = []
        for _ in range(3):
            t0 = time.ticks_us()
            pages_module.render(page, frame, {}, theme, 0, len(PAGES), "host")
            taken.append(time.ticks_diff(time.ticks_us(), t0) / 1000.0)
        badge.update()
        shot(label if len(THEMES) == 1 else f"{label}_{name}")
        print(f"  {label}: first {taken[0]:.1f}, baking {taken[1]:.1f}, "
              f"settled {taken[2]:.1f} ms")

gc.collect()
print(f"free memory: {gc.mem_free() // 1024} KB")
print("done; frames are in build/shots")
