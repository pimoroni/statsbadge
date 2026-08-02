"""Fetch the real layout and real stats, draw every page, and dump each to the host.

    statsbadge serve
    mpremote connect PORT mount . run tools/live_shots.py
    python3 tools/shots.py build/shots

Unlike tools/probe.py this uses whatever the host actually reports, so it shows what a
page looks like when a field is missing - which on macOS is every temperature.
"""

import os
import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import draw
import net
import pages as pages_module
import look
import wifi

# The badge modules an extension had pushed, imported the way the app imports them, so a page
# kind an extension registers can be shot as well. From the badge's own ext directory, since
# that is where an asset beside a module was installed to.
EXT_DIR = look.APP_DIR + "/ext"
try:
    sys.path.insert(0, EXT_DIR)
    for name in sorted(os.listdir(EXT_DIR)):
        if name.endswith(".py") and not name.startswith("_"):
            __import__(name[:-3])
except OSError:
    pass

for directory in ("/remote/build", "/remote/build/shots"):
    try:
        os.mkdir(directory)
    except OSError:
        pass

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

while not wifi.connect():
    badge.poll()
    time.sleep_ms(50)

draw.prepare()
config = net.Config()
if not config.paired:
    raise SystemExit("not paired; run: statsbadge install --state-only")
client = net.Client(config)


def get(path):
    client.get(path)
    while not client.step():
        pass
    if client.status != net.DONE:
        raise SystemExit(f"{path} failed: {client.http_status} {client.error}")
    return client.json()


layout = get("/v1/layout")
frame = get("/v1/stats")
graph_keys = []
for page in layout["pages"]:
    if page.get("kind") == "graph":
        graph_keys += [ref for ref in page.get("fields", []) if ref not in graph_keys]
keys = ",".join(graph_keys) or "cpu.pct"
history = get(f"/v1/history?keys={keys}&points={layout.get('graph_points', 48)}")

# The colours the host sent, not the one theme this app was built with: a page drawn in the
# default dark is not what the badge is showing.
theme = (look.from_palette(layout.get("theme", look.DEFAULT), layout.get("palette"))
         or look.get(layout.get("theme", look.DEFAULT)))
pages = layout["pages"]
print(f"theme {layout.get('theme')}, {len(pages)} pages, "
      f"host {frame.get('sys', {}).get('host')}")

for index, page in enumerate(pages):
    t0 = time.ticks_us()
    pages_module.render(page, frame, history, theme, index, len(pages),
                        frame.get("sys", {}).get("host"))
    took = time.ticks_diff(time.ticks_us(), t0) / 1000
    badge.update()
    with open("/remote/build/shots/live_{}.raw".format(page["id"]), "wb") as handle:
        handle.write(screen.raw)
    field = page.get("field") or ",".join(page.get("fields", []))
    print(f"  {page['id']:<8} {page['kind']:<6} {took:6.2f} ms  {field}")

print(f"\nwrote {len(pages)} shots to ./shots")
