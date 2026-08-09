"""Run the app's drawing on a badge without installing it, time it, and dump frames.

    python3 tools/dump_themes.py          # the palettes the host would send
    mpremote connect PORT mount . run tools/probe.py

Mount the repo root, not the app directory: frames go to /remote/build/shots, which is
ignored - `tools/shots.py --publish` copies the README's out of there. Draws
every
page kind against a canned frame, so it needs no server, then times a real poll if
the badge happens to be paired.
"""

import gc
import json
import os
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")
sys.path.insert(0, "/remote/extensions/statsbadge-clock/src/statsbadge_clock/badge")

import draw
import look
import pages as pages_module

# Registers the clockface kind by importing, the same way the app picks it up out of
# ext/. Without this the clock page has no renderer and draws a message saying so.
import clockface  # noqa: F401

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

t0 = time.ticks_us()
draw.prepare()
print("font.load + prepare: %.1f ms" % (time.ticks_diff(time.ticks_us(), t0) / 1000))

# The picture on a mocked post, encoded the way an extension sends one: a 128x96 indexed
# PNG out of statsbadge.imaging, base64 in the frame. Beside this file rather than in it,
# 4KB of base64 being nothing anybody reads.
with open("/remote/tools/mock_post_image.b64") as _handle:
    MOCK_IMAGE = _handle.read().strip()

# A frame with everything filled in, so no page draws "--" by accident.
FRAME = {
    "v": 1, "seq": 7, "layout_rev": 3,
    "cpu": {"pct": 63.5, "temp": 71.0, "freq": 4200, "procs": 512,
            "cores": [31.0, 88.2, 12.5, 74.1, 20.0, 95.5, 60.2, 5.0,
                      44.0, 18.0, 66.0, 9.0]},
    "mem": {"pct": 71.2, "used_mb": 23330, "total_mb": 32768, "swap_pct": 12.0},
    "gpu": [{"name": "RTX 4070", "pct": 88.0, "temp": 67.0, "mem_pct": 54.0,
             "power": 182.5, "clock": 2610, "fan_pct": 62.0}],
    "net": {"iface": "en0", "up_bps": 1258291, "down_bps": 11534336,
            "up_total_mb": 1952, "down_total_mb": 8710},
    "disk": {"pct": 74.2, "read_bps": 52428800, "write_bps": 8388608,
             "used_mb": 703840, "total_mb": 948584},
    "power": {"battery_pct": 91, "charging": True, "package_w": 44.2},
    # A feed, for the notifications page. Four things - who, what, when and why - which
    # is a post, a mention, a headline and an RSS entry alike.
    # Invented, not copied from anybody's timeline: a shot of this goes on the project
    # page, where a real post is somebody else's words republished without their say-so.
    "feed": {
        "home": {"title": "@alder", "text": "Your Tufty 2350 is crying out for some "
                                            "statsbadge love",
                 "age_s": 420, "note": "boosted", "image": MOCK_IMAGE},
        "mention": {"title": "@questing.beetle", "text": "which pin header did you end up "
                                                         "using for the display?",
                    "age_s": 34200},
        "headline": {"title": "The Wire", "text": "Something has happened somewhere, and "
                                                  "this is the headline about it",
                     "age_s": 90, "note": "Technology"},
        "followers": 1350, "following": 663, "posts": 6466, "likes": 21,
    },
    # A second one, for a page watching a handle and a feed rather than an account.
    "bsky_example": {
        "latest": {"title": "example.bsky.social", "text": "New firmware is up. Two years of "
                                                           "battery on a coin cell, which we "
                                                           "did not expect either",
                   "age_s": 1500},
        "followers": 8420, "likes": 96, "reposts": 31,
    },
    "bskyfeed_makers": {
        "latest": {"title": "@wren.example", "text": "a lamp that gets brighter the longer "
                                                     "you have been sitting still. it is "
                                                     "judging me and it is right",
                   "age_s": 240},
        "likes": 214,
    },
    # What the collector sends as the scale for each rate.
    "peaks": {"net.down_bps": 23068672, "net.up_bps": 2516582,
              "disk.read_bps": 104857600, "disk.write_bps": 33554432},
    "fans": [{"name": "cpu", "rpm": 1820}],
    "sys": {"host": "workshop-pc", "os": "Windows 11", "arch": "AMD64",
            "cpu_name": "Ryzen 7 7800X3D", "uptime_s": 271830},
    # The clock extension's groups. The hands are drawn from hour, minute and seconds
    # and the digits from time, so these have to agree or the shot shows a clock
    # disagreeing with itself.
    "clock": {"time": "10:09", "date": "Fri 31 Jul", "hour": 10, "minute": 9,
              "seconds": 36},
    # One entry per clock page, which is how the host sends a page its own location.
    "places": {page_id: {"time": "10:09", "date": "Fri 31 Jul", "hour": 10, "minute": 9,
                         "seconds": 36, "temp": 16.0, "temp_unit": "C",
                         "high": 19.0, "low": 11.0,
                         "condition": "overcast", "icon": "f", "wind": 14.0,
                         "wind_unit": "km/h", "place": "Sheffield, GB",
                         "utc_offset": 3600}
               for page_id in ("swiss_clock", "face_dots", "face_squircle",
                               "face_digital", "face_lcd")},
    "weather": {"temp": 16.0, "feels": 14.0, "humidity": 78, "wind": 14.0,
                "high": 19.0, "low": 11.0,
                "condition": "overcast", "code": 3,
                # Units travel with the numbers, and the icon is a character in the
                # extension's own icons.af.
                "temp_unit": "C", "wind_unit": "km/h", "icon": "f"},
    # A group a domain, the shape statsbadge-cloudflare reports. Example domains, so a
    # published shot names nobody's sites.
    "cf_example_com": {"requests": 812.4, "bytes_bps": 4194304, "cached_pct": 87.0,
                       "requests_today": 964120, "pageviews_today": 120433,
                       "threats_today": 218},
    "cf_example_org": {"requests": 214.8, "bytes_bps": 1153434, "cached_pct": 74.5,
                       "requests_today": 251880, "pageviews_today": 31204,
                       "threats_today": 46},
    "cf_example_net": {"requests": 63.1, "bytes_bps": 327680, "cached_pct": 91.2,
                       "requests_today": 74210, "pageviews_today": 8801,
                       "threats_today": 12},
}

# What the host sends alongside the layout for groups the badge cannot name from the key.
# Without it a domain draws as CF_EXAMPLE_COM.
LABELS = {"cf_example_com": "example.com", "cf_example_org": "example.org",
          "cf_example_net": "example.net",
          "bsky_example": "example.bsky.social", "bskyfeed_makers": "Makers"}


def ramp(n, peak=100.0):
    """A plausible wiggle for a graph, without needing a server's history."""
    import math
    return [peak * (0.35 + 0.3 * math.sin(i / 4.0) + 0.2 * math.sin(i / 1.7))
            for i in range(n)]


def day(n, peak, low=0.18):
    """A day of hourly points, quiet overnight and busy in the afternoon.

    Cloudflare's history is hourly and a day long, so a graph of it has the shape of a day
    rather than the last ninety seconds a sampled reading would give.
    """
    import math
    return [peak * (low + (1 - low) * max(0.0, math.sin((i / n) * math.pi) ** 1.6))
            for i in range(n)]


def core_ramp(cores, n):
    """A ring of per-core samples, the shape the host's history sends for a list field."""
    import math
    return [[max(0.0, min(100.0, 50 + 45 * math.sin(i / 5.0 + c * 0.8)))
             for c in range(cores)] for i in range(n)]


HISTORY = {
    "cpu.cores": core_ramp(12, 40),
    "cpu.pct": ramp(48), "gpu.pct": ramp(48, 90),
    "cpu.temp": ramp(48, 80), "gpu.temp": ramp(48, 70),
    "net.down_bps": ramp(48, 11534336), "net.up_bps": ramp(48, 1258291),
    "cf_example_com.requests": day(24, 1180), "cf_example_org.requests": day(24, 320),
    "cf_example_net.requests": day(24, 96), "cf_example_com.cached_pct": day(24, 94, 0.82),
}

PAGES = [
    {"id": "cpu", "kind": "dial", "title": "CPU", "field": "cpu.pct",
     "readouts": ["cpu.temp", "cpu.freq", "cpu.procs"]},
    {"id": "cores", "kind": "bars", "title": "Cores", "field": "cpu.cores"},
    {"id": "gpu", "kind": "dial", "title": "GPU", "field": "gpu.pct",
     "readouts": ["gpu.temp", "gpu.power", "gpu.mem_pct"]},
    {"id": "mem", "kind": "dial", "title": "Memory", "field": "mem.pct",
     "readouts": ["mem.used_mb", "mem.total_mb", "mem.swap_pct"]},
    {"id": "gauges", "kind": "dials", "title": "Load",
     "fields": ["cpu.pct", "gpu.pct", "mem.pct", "disk.pct"]},
    {"id": "gauges3", "kind": "dials", "title": "System",
     "fields": ["cpu.pct", "mem.pct", "disk.pct"]},
    {"id": "gauges2", "kind": "dials", "title": "Processor",
     "fields": ["cpu.pct", "cpu.temp"]},
    {"id": "net", "kind": "graph", "title": "Network",
     "fields": ["net.down_bps", "net.up_bps"]},
    {"id": "thermal", "kind": "graph", "title": "Thermals",
     "fields": ["cpu.temp", "gpu.temp"]},
    {"id": "disk", "kind": "grid", "title": "Disk",
     "fields": ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]},
    {"id": "host", "kind": "text", "title": "Host",
     "fields": ["sys.host", "sys.os", "sys.cpu_name", "sys.uptime_s",
                "power.battery_pct", "power.package_w"]},
    {"id": "rings", "kind": "rings", "title": "Load",
     "fields": ["cpu.pct", "mem.pct", "gpu.pct", "disk.pct"]},
    # Rates, whose full scale is the peak the host has seen and so has to be stated.
    {"id": "rings_rate", "kind": "rings", "title": "Throughput",
     "fields": ["net.down_bps", "net.up_bps", "disk.read_bps"]},
    {"id": "spark", "kind": "spark", "title": "At a glance",
     "fields": ["cpu.pct", "cpu.temp", "mem.pct", "gpu.pct", "net.down_bps",
                "disk.read_bps"]},
    {"id": "radar", "kind": "radar", "title": "Shape",
     "fields": ["cpu.pct", "mem.pct", "gpu.pct", "disk.pct", "gpu.temp"]},
    {"id": "trend", "kind": "trend", "title": "CPU", "field": "cpu.pct"},
    # Named apart from the shot the docs show. This renders a page once, and a waterfall
    # puts down one column a frame, so what lands here is a sliver against a screen of
    # background. tools/waterfall_shot.py drives it until the plot is full and writes
    # `waterfall`; sharing the name meant whichever tool ran last won.
    {"id": "waterfall_1frame", "kind": "waterfall", "title": "Cores",
     "field": "cpu.cores"},
    {"id": "notify", "kind": "notify", "title": "Mastodon",
     "fields": ["feed.home", "feed.mention",
                "feed.followers", "feed.following", "feed.posts", "feed.likes"]},
    {"id": "notify_one", "kind": "notify", "title": "Headlines",
     "fields": ["feed.headline"]},
    {"id": "mastodon", "kind": "notify", "title": "Mastodon Feed",
     "fields": ["feed.home", "feed.mention",
                "feed.followers", "feed.posts", "feed.likes"]},
    {"id": "bluesky", "kind": "notify", "title": "BlueSky",
     "fields": ["bsky_example.latest", "bskyfeed_makers.latest",
                "bsky_example.followers", "bsky_example.reposts", "bskyfeed_makers.likes"]},
    # A service rather than a machine, drawn by the pages the badge already has.
    {"id": "cloudflare", "kind": "graph", "title": "Requests",
     "fields": ["cf_example_com.requests", "cf_example_org.requests"]},
    {"id": "cloudflare_spark", "kind": "spark", "title": "Cloudflare",
     "fields": ["cf_example_com.requests", "cf_example_org.requests",
                "cf_example_net.requests", "cf_example_com.cached_pct"]},
    # The clock extension's pages, one per face. The ids name the shots, and each page
    # id is also the key its place is published under.
    {"id": "swiss_clock", "kind": "clockface", "title": "Clock", "face": "railway"},
    {"id": "face_dots", "kind": "clockface", "title": "Clock", "face": "dots"},
    {"id": "face_squircle", "kind": "clockface", "title": "Clock", "face": "squircle"},
    {"id": "face_digital", "kind": "clockface", "title": "Clock", "face": "digital"},
    {"id": "face_lcd", "kind": "clockface", "title": "Clock", "face": "lcd"},
]


OUT_DIR = "/remote/build/shots"
try:
    os.mkdir("/remote/build")
except OSError:
    pass
try:
    os.mkdir(OUT_DIR)
except OSError:
    pass


def shot(name):
    with open(f"{OUT_DIR}/{name}.raw", "wb") as handle:
        handle.write(screen.raw)


def time_page(page, theme, n=12):
    """ms per frame excluding display.update, after one warm-up frame."""
    pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES))
    gc.collect()
    t = time.ticks_us()
    for _ in range(n):
        pages_module.render(page, FRAME, HISTORY, theme, 0, len(PAGES))
    return time.ticks_diff(time.ticks_us(), t) / n / 1000


# The palettes the host would send, and not the one this app boots with: that one carries
# no image ramps, so a picture keeps the greys it arrived in instead of being redrawn in
# the theme's. tools/dump_themes.py writes them.
try:
    with open("/remote/build/themes.json") as _handle:
        PALETTES = json.load(_handle)
except (OSError, ValueError):
    PALETTES = {}
    print("no build/themes.json; run tools/dump_themes.py for themed pictures and the "
          "theme sweep")


def themed(name):
    """`name` as the badge would build it from a layout, or what it booted with."""
    palette = PALETTES.get(name)
    return (look.from_palette(name, palette) if palette else None) or look.get(name)


print()
pages_module.LABELS = LABELS
theme = themed("dark")
for index, page in enumerate(PAGES):
    per_frame = time_page(page, theme)
    pages_module.render(page, FRAME, HISTORY, theme, index, len(PAGES),
                        FRAME["sys"]["host"])
    badge.update()
    shot(page["id"])
    print(f"{page['id']:<8} {page['kind']:<6} {per_frame:6.2f} ms/frame")

# The single dial again with the whole ramp swept round it. A second pass over the same
# page, since the fill is a layout setting and not a page one, and it is worth a shot
# because no other page can show it.
draw.GAUGE_FILL = "ramp"
draw.clear_cache()
ramped = time_page(PAGES[0], theme)
pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), FRAME["sys"]["host"])
badge.update()
shot("dial_ramp")
draw.GAUGE_FILL = "solid"
draw.clear_cache()
print(f"{'dial_ramp':<8} {'dial':<6} {ramped:6.2f} ms/frame  (gauge_fill = ramp)")

# A page that has to redraw its furniture is the worst case; measure a page turn.
draw.clear_cache()
t = time.ticks_us()
pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), "host")
print("\nfirst draw of a page, cold cache: %.1f ms"
      % (time.ticks_diff(time.ticks_us(), t) / 1000))

# Every palette the host has, built the way the badge builds one from a layout: the app
# itself only carries the one it boots with.
print("\nevery theme, on the CPU dial:")
for name in PALETTES:
    theme = themed(name)
    draw.clear_cache()
    per_frame = time_page(PAGES[0], theme)
    pages_module.render(PAGES[0], FRAME, HISTORY, theme, 0, len(PAGES), "workshop-pc")
    badge.update()
    shot(f"theme_{name}")
    print(f"  {name:<12} {per_frame:6.2f} ms/frame")

# Missing data must read as "unknown", not as zero.
draw.clear_cache()
theme = themed("dark")
sparse = {"v": 1, "cpu": {"pct": 12.0}, "mem": {}, "gpu": [], "net": {},
          "disk": {}, "power": {}, "fans": [], "sys": {"host": "quiet"}}
pages_module.render(PAGES[0], sparse, {}, theme, 0, len(PAGES), "quiet")
badge.update()
shot("sparse")
print("\nsparse frame drew without raising")


# Every screen that is not a page, so the shots in the README cannot drift from what
# the app draws. The wording is not repeated here: these call the app's own drawing.
import setup as setup_ui    # noqa: E402
import splash               # noqa: E402

SCREENS = (
    ("splash", splash.show),
    ("banner", lambda: draw.banner(theme, "Not paired", "B to set up",
                                   "or run: statsbadge install")),
    ("err_rejected", lambda: draw.banner(theme, "Not recognised", "workshop-pc",
                                         "B to pair again")),
    ("err_noserver", lambda: draw.banner(theme, "Connecting", "workshop-pc:8420",
                                         "no server answering")),
    ("setup_looking", lambda: draw.banner(theme, "Looking",
                                          "for a host on the network",
                                          "4s  -  HOME to cancel")),
    ("setup_nohost", lambda: draw.banner(theme, "No host", "nothing answered",
                                         "B retry   HOME quit")),
    ("setup_choose", lambda: setup_ui.draw_hosts(theme, DISCOVERED, 0, {"pc-1": {}})),
    ("setup_code", lambda: setup_ui.draw_code(theme, "7F3A9C", "workshop-pc")),
    ("setup_refused", lambda: draw.banner(theme, "Refused", "already pairing",
                                          "B retry   A back   HOME quit")),
    ("setup_paired", lambda: draw.banner(theme, "Paired", "workshop-pc",
                                         "2 host(s) known")),
    ("menu_hosts", lambda: setup_ui.draw_rows(theme, MENU_ROWS, 0)),
)

DISCOVERED = [
    {"id": "pc-1", "name": "workshop-pc", "host": "10.10.1.40", "port": 8420},
    {"id": "mac-1", "name": "studio-mac", "host": "10.10.1.51", "port": 8420},
]

MENU_ROWS = [
    {"kind": "known", "label": "workshop-pc", "detail": "10.10.1.40:8420",
     "note": "active"},
    {"kind": "known", "label": "studio-mac", "detail": "10.10.1.51:8420",
     "note": "here"},
    {"kind": "known", "label": "linux-box", "detail": "10.10.1.62:8420",
     "note": "not seen"},
    {"kind": "new", "label": "spare-pi", "detail": "10.10.1.77:8420", "note": "add"},
    {"kind": "rescan", "label": "Look again", "detail": "", "note": ""},
    {"kind": "exit", "label": "Leave the app", "detail": "", "note": ""},
]

print()
theme = themed("dark")
for name, render in SCREENS:
    draw.clear_cache()
    render()
    badge.update()
    shot(name)
    print(f"  {name}")

gc.collect()
print(f"free memory: {gc.mem_free() // 1024} KB")
print("\ndone; shots are in ./shots")
