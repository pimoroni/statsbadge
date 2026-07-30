"""Talk to a real statsbadge server from the badge, and report what it costs.

    statsbadge install --state-only        # once, to write /state/stats.json
    statsbadge serve
    mpremote connect PORT mount . run tools/live.py

Exercises the signed endpoints the app uses, so a signing or framing mistake shows up
here rather than as a blank page in the app.
"""

import sys
import time

sys.path.insert(0, "/remote/stats")

import net
import wifi

badge.mode(HIRES | VSYNC)
BUTTON_HOME.irq(None)

while not wifi.connect():
    badge.poll()
    time.sleep_ms(50)
print(f"wifi {wifi.ipv4()} -> ", end="")

config = net.Config()
if not config.paired:
    raise SystemExit("no /state/stats.json; run: statsbadge install --state-only")
print(f"host {config.host}:{config.port}, badge {config.badge_id}")

client = net.Client(config)


def fetch(path, n=8):
    """Drive one request the way the app does: one step per frame."""
    totals, worsts, steps = [], [], []
    payload = None
    for i in range(n):
        t0 = time.ticks_ms()
        worst = 0
        count = 0
        client.get(path)
        while True:
            ts = time.ticks_us()
            done = client.step()
            worst = max(worst, time.ticks_diff(time.ticks_us(), ts))
            count += 1
            if done:
                break
        if client.status != net.DONE:
            print(f"  {path:<28} FAILED http={client.http_status} {client.error}")
            return None
        payload = client.json()
        if i:
            totals.append(time.ticks_diff(time.ticks_ms(), t0))
            worsts.append(worst)
            steps.append(count)
    print(f"  {path:<28} {sum(totals) // len(totals):3d}ms avg "
          f"(min {min(totals)} max {max(totals)})  "
          f"{sum(steps) // len(steps):2d} steps  "
          f"worst step {max(worsts) / 1000:.2f}ms")
    return payload


print("\nsigned requests:")
frame = fetch("/v1/stats")
layout = fetch("/v1/layout")
history = fetch("/v1/history?keys=cpu.pct,mem.pct&points=32")

if frame:
    print("\nwhat the host says:")
    print("  host   {} ({})".format(frame.get("sys", {}).get("host"),
                                frame.get("sys", {}).get("cpu_name")))
    print("  cpu    {}%  mem {}%".format(frame.get("cpu", {}).get("pct"),
                                       frame.get("mem", {}).get("pct")))
    gpus = frame.get("gpu") or []
    if gpus:
        print("  gpu    {} at {}%".format(gpus[0].get("name"), gpus[0].get("pct")))
    print("  net    down {} up {} B/s".format(frame.get("net", {}).get("down_bps"),
                                          frame.get("net", {}).get("up_bps")))
    print("  seq {}, layout rev {}".format(frame.get("seq"), frame.get("layout_rev")))

if layout:
    print(f"\nlayout: theme {layout.get('theme')!r}, "
          f"{len(layout.get('pages', []))} pages, "
          f"{layout.get('interval_ms', 0)}ms interval")
    for page in layout.get("pages", []):
        refs = page.get("field") or ",".join(page.get("fields", []))
        print(f"  {page.get('id'):<8} {page.get('kind'):<6} {refs}")

if history:
    for key in history:
        print(f"history {key:<14} {len(history[key])} points")

# Replay protection has to actually be on: reusing a counter must be refused.
print("\nchecking the host rejects a replayed counter:")
seq = config.seq
signature = net.sign(config.secret, "GET", "/v1/stats", seq, b"")
print(f"  (counter {seq}, already used by the requests above)")
import socket

try:
    info = socket.getaddrinfo(config.host, config.port, 0, socket.SOCK_STREAM)[0]
    sock = socket.socket(info[0], info[1], info[2])
    sock.settimeout(5)
    sock.connect(info[4])
    sock.write((f"GET /v1/stats HTTP/1.1\r\nHost: {config.host}\r\n"
                f"Connection: close\r\nX-Badge-Id: {config.badge_id}\r\n"
                f"X-Badge-Seq: {seq}\r\nX-Badge-Sig: {signature}\r\n\r\n"
                ).encode())
    reply = sock.read(200) or b""
    sock.close()
    first = reply.split(b"\r\n")[0].decode()
    print(f"  {first}  <- expected 401")
except OSError as exc:
    print(f"  socket error: {exc}")

# And a bad signature must not get in either.
print("\nchecking the host rejects a bad signature:")
try:
    info = socket.getaddrinfo(config.host, config.port, 0, socket.SOCK_STREAM)[0]
    sock = socket.socket(info[0], info[1], info[2])
    sock.settimeout(5)
    sock.connect(info[4])
    sock.write((f"GET /v1/stats HTTP/1.1\r\nHost: {config.host}\r\n"
                f"Connection: close\r\nX-Badge-Id: {config.badge_id}\r\n"
                f"X-Badge-Seq: {config.next_seq()}\r\n"
                f"X-Badge-Sig: {'00' * 32}\r\n\r\n").encode())
    reply = sock.read(200) or b""
    sock.close()
    print("  {}  <- expected 401".format(reply.split(b"\r\n")[0].decode()))
except OSError as exc:
    print(f"  socket error: {exc}")

config.save()
print(f"\ncounter saved at {config.seq}")
