"""Exercise the badge's multi-host config: migration, DHCP move, switching.

    mpremote connect PORT mount . run tools/multihost_test.py

Uses a scratch state file, so the real pairing is left alone.
"""

import json
import os
import sys

sys.path.insert(0, "/remote/src/statsbadge/badge_app")

import net

REAL = net.STATE_FILE
STATE_APP = "stats_test"
net.STATE_FILE = f"/state/{STATE_APP}.json"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def write(data):
    with open(net.STATE_FILE, "w") as handle:
        json.dump(data, handle)


# -- an older flat file must keep working ----------------------------------
write({"host": "10.0.0.5", "port": 8420, "secret": "ab" * 32,
       "badge_id": "badge1", "seq": 100})
config = net.Config()
check("flat file loads", config.paired, f"host={config.host}")
check("flat host reads back", config.host == "10.0.0.5" and config.port == 8420)
check("flat counter jumps forward", config.seq == 100 + net.Config.SEQ_FLUSH,
      f"seq={config.seq}")
check("flat file lands under a placeholder id", list(config.hosts) == ["unknown"],
      str(list(config.hosts)))

# ...and the real id replaces the stand-in once a beacon reveals it.
check("adopt_id moves it", config.adopt_id("srv-aaa", "workshop"))
check("keyed on the real id", "srv-aaa" in config.hosts and "unknown" not in config.hosts,
      str(list(config.hosts)))
check("secret survived", config.secret == "ab" * 32)
check("counter survived", config.seq == 100 + net.Config.SEQ_FLUSH)
check("name picked up", config.name == "workshop", config.name)

# -- the DHCP case: same host, new address ---------------------------------
before_secret, before_seq = config.secret, config.seq
check("note_address moves it", config.note_address("srv-aaa", "10.0.0.99", 8420))
check("address updated", config.host == "10.0.0.99")
check("secret unchanged by a move", config.secret == before_secret)
check("counter unchanged by a move", config.seq == before_seq)
check("no-op when the address is the same",
      not config.note_address("srv-aaa", "10.0.0.99", 8420))

# -- a second computer -----------------------------------------------------
config.remember("srv-bbb", "10.0.0.7", 8420, "cd" * 32, "laptop", seq=0)
check("two hosts known", len(config.hosts) == 2, str(list(config.hosts)))
check("the new one is active", config.active == "srv-bbb" and config.name == "laptop")
check("its own secret", config.secret == "cd" * 32)
check("its own counter", config.seq == 0, f"seq={config.seq}")

check("switch back", config.switch("srv-aaa"))
check("first host's secret intact", config.secret == before_secret)
check("first host's counter intact", config.seq == before_seq, f"seq={config.seq}")
check("switching to the active one is a no-op", not config.switch("srv-aaa"))
check("switching to an unknown id refuses", not config.switch("srv-zzz"))

# -- the page index shares this file, so both writers must merge -----------
config.save()
before_hosts = len(net.Config().hosts)
State.modify(STATE_APP, {"page": 3})
check("pairing survives a page save", len(net.Config().hosts) == before_hosts,
      f"{len(net.Config().hosts)} of {before_hosts} hosts left")

# The other way round: writing the pairing must not drop the page.
net.Config().save()
page = {"page": 0}
State.load(STATE_APP, page)
check("page survives a config save", page["page"] == 3, str(page))

# A plain State.save would replace the file; that is why the app uses modify.
State.save(STATE_APP, {"page": 4})
check("a plain State.save does wipe it (hence modify)", len(net.Config().hosts) == 0,
      "State.save left hosts behind")
config = net.Config()
config.badge_id = "badge1"
config.remember("srv-aaa", "10.0.0.99", 8420, "ab" * 32, "workshop", seq=before_seq)
config.remember("srv-bbb", "10.0.0.7", 8420, "cd" * 32, "laptop", seq=0)
config.switch("srv-aaa")
check("rebuilt for the rest of the checks", len(config.hosts) == 2,
      f"{len(config.hosts)} hosts")

# -- it all survives a reload ----------------------------------------------
config.save()
reloaded = net.Config()
check("reload keeps both hosts", len(reloaded.hosts) == 2, str(list(reloaded.hosts)))
check("reload keeps the active one", reloaded.active == "srv-aaa", reloaded.active)
check("reload keeps per-host secrets",
      reloaded.hosts["srv-aaa"]["secret"] == "ab" * 32
      and reloaded.hosts["srv-bbb"]["secret"] == "cd" * 32)
check("counters advance on reload",
      reloaded.seq == before_seq + net.Config.SEQ_FLUSH, f"seq={reloaded.seq}")

# Signing must use the active host's secret, not another's.
sig_a = net.sign(reloaded.hosts["srv-aaa"]["secret"], "GET", "/v1/stats", 5, b"")
sig_b = net.sign(reloaded.hosts["srv-bbb"]["secret"], "GET", "/v1/stats", 5, b"")
check("hosts sign differently", sig_a != sig_b)

try:
    os.remove(net.STATE_FILE)
except OSError:
    pass
net.STATE_FILE = REAL

print()
print(f"{len(failures)} failed" if failures else "all multi-host checks passed")
