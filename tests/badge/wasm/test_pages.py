"""Every page kind, drawn.

Run under the WASM port, against real picovector and the real fonts, by
`node tools/wasm/run.mjs`. `pages.render` reaches `screen`, `image` and `tween`, so on a
host these could only be read as text.
"""

import unittest

import draw
import look
import pages

# Where the installer puts an extension's badge modules, and where the runner staged them.
EXT_DIR = "/system/apps/stats/ext"

FRAME = {
    "v": 1, "seq": 7, "layout_rev": 3,
    "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512,
            "cores": [31.0, 88.2, 12.5, 74.1, 20.0, 95.5, 60.2, 5.0]},
    "mem": {"pct": 71.2, "used_mb": 23330, "total_mb": 32768, "swap_pct": 12.0},
    "net": {"iface": "en0", "up_bps": 1258291, "down_bps": 11534336},
    "disk": {"pct": 74.2, "read_bps": 52428800, "write_bps": 8388608},
    "power": {"battery_pct": 91, "charging": True, "package_w": 44.2},
    "sys": {"host": "desk", "os": "macOS 15.5", "up_s": 384000},
}

# One page per kind, each carrying what that kind needs. Every ref is in FRAME, though a
# kind drawing "unknown" everywhere would still pass: what is under test is that it draws.
PAGES = (
    {"kind": "dial", "title": "CPU", "field": "cpu.pct", "readouts": ["cpu.temp"]},
    {"kind": "dials", "title": "Load", "fields": ["cpu.pct", "mem.pct", "disk.pct"]},
    {"kind": "bars", "title": "Cores", "field": "cpu.cores"},
    {"kind": "graph", "title": "CPU", "fields": ["cpu.pct"]},
    {"kind": "grid", "title": "All", "fields": ["cpu.pct", "mem.pct", "disk.pct"]},
    {"kind": "text", "title": "Host", "fields": ["sys.host", "sys.os"]},
    {"kind": "rings", "title": "Load", "fields": ["cpu.pct", "mem.pct"]},
    {"kind": "spark", "title": "Load", "fields": ["cpu.pct", "mem.pct"]},
    {"kind": "radar", "title": "Load", "fields": ["cpu.pct", "mem.pct", "disk.pct"]},
    {"kind": "trend", "title": "CPU", "field": "cpu.pct"},
    {"kind": "waterfall", "title": "Cores", "field": "cpu.cores"},
    {"kind": "notify", "title": "Feed", "fields": []},
    {"kind": "badge", "title": "Badge"},
)


def body_pixels():
    """The page band, sampled. RGB only: alpha does not move here."""
    raw = screen.raw  # noqa: F821
    stride = look.W * 4
    out = bytearray()
    for y in range(look.BODY_TOP, look.BODY_TOP + look.BODY_H, 2):
        row = y * stride
        for x in range(0, look.W, 4):
            index = row + x * 4
            out.append(raw[index])
            out.append(raw[index + 1])
            out.append(raw[index + 2])
    return bytes(out)


def chrome_only(theme, title, index, total):
    """The band with the chrome drawn and no handler after it.

    `pages.render` covers the band with the background before it dispatches, so a kind
    measured against a cleared screen would pass having drawn nothing.
    """
    draw.background(theme, title, index, total, None)
    return body_pixels()


def differing(first, second):
    """How much of the sampled band the two disagree on."""
    moved = 0
    for index in range(0, len(first), 3):
        if first[index:index + 3] != second[index:index + 3]:
            moved += 1
    return moved / (len(first) / 3)


class PageKinds(unittest.TestCase):
    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)

    def test_every_builtin_kind_is_offered_and_draws(self):
        offered = set(pages._KINDS)
        covered = {page["kind"] for page in PAGES}
        self.assertEqual(offered - covered, set(), "a kind here has no page to draw it")

        for index, page in enumerate(PAGES):
            chrome = chrome_only(self.theme, page["title"], index, len(PAGES))
            pages.render(page, FRAME, {}, self.theme, index, len(PAGES))
            drawn = differing(chrome, body_pixels())
            # Low, because the waterfall adds a column a frame and one render is a single
            # stripe at 0.36%. A handler that returned measures 0.00%.
            self.assertTrue(
                drawn > 0.001,
                f"{page['kind']} drew {drawn * 100:.2f}% of its band beyond the chrome")

    def test_a_kind_with_no_renderer_says_so_rather_than_raising(self):
        chrome = chrome_only(self.theme, "?", 0, 1)
        pages.render({"kind": "nonesuch", "title": "?"}, FRAME, {}, self.theme, 0, 1)
        self.assertTrue(differing(chrome, body_pixels()) > 0.001,
                        "an unknown kind drew nothing where the message should be")

    def test_a_page_puts_the_clip_back(self):
        """The pages are drawn one after another into the same screen, so a clip left
        behind takes a bite out of whatever is drawn next."""
        for page in PAGES:
            screen.clip = rect(0, 0, screen.width, screen.height)  # noqa: F821
            pages.render(page, FRAME, {}, self.theme, 0, 1)
            # `rect` compares by identity, so the four numbers are the comparison.
            clip = screen.clip  # noqa: F821
            self.assertEqual((clip.x, clip.y, clip.w, clip.h),
                             (0.0, 0.0, float(screen.width), float(screen.height)),  # noqa: F821
                             f"{page['kind']} left a clip behind")


def load_extensions():
    """Import the staged extension modules, the way the app does at startup.

    The app's walk is `app.load_extensions`, which cannot be reached here: importing
    app.py imports net, and this port has no socket module. Sorted and skipping
    underscored names, as that one is, so the registration order matches the badge.
    """
    import os
    loaded = []
    for name in sorted(os.listdir(EXT_DIR)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        __import__(name[:-3])
        loaded.append(name[:-3])
    return loaded


class ExtensionKinds(unittest.TestCase):
    """Whatever the installed extensions registered, through the same dispatch."""

    def setUp(self):
        draw.prepare()
        self.theme = look.get(look.DEFAULT)
        load_extensions()

    def test_an_extension_kind_draws_through_the_same_dispatch(self):
        if not pages.EXTRA:
            self.skipTest("no extension modules were staged")
        for kind in pages.EXTRA:
            chrome = chrome_only(self.theme, kind, 0, 1)
            pages.render({"kind": kind, "title": kind}, FRAME, {}, self.theme, 0, 1)
            drawn = differing(chrome, body_pixels())
            self.assertTrue(
                drawn > 0.001,
                f"{kind} drew {drawn * 100:.2f}% of its band beyond the chrome")


if __name__ == "__main__":
    unittest.main()
