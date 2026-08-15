"""Serve a real statsbadge to the badge tests, with one badge paired to it.

Run by tools/wasm/run.mjs, which reads the first line of stdout for where to reach it
and which credentials to use. Stays up until it is killed.

    uv run python tools/wasm/host.py

The same server tests/test_server.py drives, so what the badge is held to here is what
the host actually answers.
"""

import json
import sys
import tempfile
import threading

from statsbadge import server

BADGE_ID = "wasmbadge0001"


def main():
    directory = tempfile.mkdtemp(prefix="statsbadge-wasm-")
    service = server.Service(directory, interval=0.2)
    service.start()
    httpd = server.make_server(service, "127.0.0.1", 0)
    secret = service.badges.provision(BADGE_ID, "the wasm runner")

    print(json.dumps({"port": httpd.server_address[1], "badge_id": BADGE_ID,
                      "secret": secret, "dir": directory}), flush=True)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        service.stop()


if __name__ == "__main__":
    sys.exit(main())
