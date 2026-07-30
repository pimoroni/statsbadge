"""Fetch the real layout and real stats, draw every page, and dump each to the host.

    statsbadge serve
    mpremote connect PORT mount . run tools/live_shots.py
    python3 tools/shots.py shots

Unlike tools/probe.py this uses whatever the host actually reports, so it shows what a
page looks like when a field is missing - which on macOS is every temperature.
"""

import sys
import time

sys.path.insert(0, "/remote/stats")

import draw
import net
import pages as pages_module
import look
import wifi

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

theme = look.get(layout.get("theme", look.DEFAULT))
pages = layout["pages"]
print(f"theme {layout.get('theme')}, {len(pages)} pages, "
      f"host {frame.get('sys', {}).get('host')}")

for index, page in enumerate(pages):
    t0 = time.ticks_us()
    pages_module.render(page, frame, history, theme, index, len(pages),
                        frame.get("sys", {}).get("host"))
    took = time.ticks_diff(time.ticks_us(), t0) / 1000
    badge.update()
    with open("/remote/shots/live_{}.raw".format(page["id"]), "wb") as handle:
        handle.write(screen.raw)
    field = page.get("field") or ",".join(page.get("fields", []))
    print(f"  {page['id']:<8} {page['kind']:<6} {took:6.2f} ms  {field}")

print(f"\nwrote {len(pages)} shots to ./shots")
