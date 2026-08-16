"""Break the stored address, then check the badge finds its host again.

    statsbadge serve
    mpremote connect PORT mount . run tools/failover_test.py

This is the DHCP case: same server, different address. The badge should hear the
beacon, recognise the id it is already paired with, and follow it without re-pairing.
"""

import sys
import time

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None
BUTTON_HOME.irq(None)

import draw
import net
import wifi

while not wifi.connect():
    badge.poll()
    time.sleep_ms(50)
draw.prepare()

with open(net.STATE_FILE) as handle:
    saved = handle.read()

try:
    config = net.Config()
    real_host = config.host
    server_id = config.active
    print(f"paired with {config.name} ({server_id}) at {real_host}:{config.port}")

    # Move it somewhere nothing is listening, as a changed DHCP lease would.
    config.hosts[server_id]["host"] = "10.10.1.222"
    config.save()

    config = net.Config()
    print(f"broke the address: now {config.host}")
    client = net.Client(config)

    # A few failed polls, which drives the app to go looking.
    for attempt in range(3):
        client.get("/v1/stats")
        while not client.step():
            pass
        print(f"  poll {attempt + 1}: {client.error}, failures={client.failures}")

    print("\nlistening for a beacon...")
    found = net.discover(timeout_ms=4000)
    for entry in found:
        print(f"  saw {entry['name']} id={entry['id']} at {entry['host']}:{entry['port']}")

    healed = False
    for entry in found:
        if entry.get("id") == server_id:
            healed = config.note_address(server_id, entry["host"], entry["port"],
                                         entry.get("name"))
            break
    print(f"\nfollowed the beacon to a new address: {healed}")
    print(f"stored address now {config.host}:{config.port}")

    if healed:
        client.close()
        client.get("/v1/stats")
        while not client.step():
            pass
        ok = client.status == net.DONE
        frame = client.json() or {}
        print(f"poll after healing: {'OK' if ok else client.error}")
        if ok:
            print(f"  host says it is {frame.get('sys', {}).get('host')}, "
                  f"cpu {frame.get('cpu', {}).get('pct')}%")
        print()
        print("PASS" if ok and config.host == real_host else "FAIL")
    else:
        print("FAIL: never recovered")
finally:
    with open(net.STATE_FILE, "w") as handle:
        handle.write(saved)
    print("(restored the original state file)")
