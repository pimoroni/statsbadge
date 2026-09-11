"""Break the firmware's `fetch` mid-exchange, then check the same AsyncFetch works again.

    python3 tools/fetch_test.py                                 the fault server, on this machine
    mpremote connect PORT mount . run tools/fetch_test.py       the badge half, in another terminal

badge_app/net.py is a hand-rolled HTTP client because the firmware's `fetch` used to leave
a dead generator in `_fetch` after a socket error, so every later request on that instance
raised "Cannot interrupt a running fetch...". This is what says whether that still holds.

Plain HTTP. The faults are socket-level, so TLS would only add a handshake to go wrong.
"""

import os

PORT = 8099
ADDRESS_FILE = "build/fetch_test_host.txt"
BADGE_ADDRESS_FILE = "/remote/" + ADDRESS_FILE

OK_PATH = "/ok"
OK_BODY = b'{"ok": true}'

# One socket error per point in the exchange the badge can be sitting at.
FAULTS = ("/reset", "/close-early", "/half-headers", "/short-body")

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


# -- the fault server -------------------------------------------------------

def lan_address():
    """This machine's address on the badge's network. Connects nothing, sends nothing."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        return probe.getsockname()[0]
    finally:
        probe.close()


def answer(conn, path):
    import socket
    import struct

    if path == "/reset":
        # A zero linger timeout closes with RST rather than FIN.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        return

    if path == "/close-early":
        return

    if path == "/half-headers":
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
        return

    if path == "/short-body":
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 64\r\n"
                     b"Connection: close\r\n\r\nhalf a bo")
        return

    head = ("HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(OK_BODY)}\r\n"
            "Connection: close\r\n\r\n")
    conn.sendall(head.encode() + OK_BODY)


def handle(conn):
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = conn.recv(1024)
        if not chunk:
            conn.close()
            return
        request += chunk
    path = request.split(b" ")[1].decode()
    print(f"  {path}")
    try:
        answer(conn, path)
    finally:
        conn.close()


def serve():
    import pathlib
    import socket
    import threading

    here = pathlib.Path(__file__).resolve().parent.parent
    address = lan_address()
    written = here / ADDRESS_FILE
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(f"{address} {PORT}\n", encoding="utf-8")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", PORT))
    listener.listen(8)

    print(f"serving faults on {address}:{PORT}, address in {ADDRESS_FILE}")
    print("now: mpremote connect PORT mount . run tools/fetch_test.py")
    while True:
        conn, _peer = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


# -- the badge half ---------------------------------------------------------

def attempt(client, path):
    """Run one request to the end. Returns (status, error), error None where it landed."""
    try:
        client.fetch(path)
    except Exception as exc:  # noqa: BLE001
        return None, exc
    try:
        client.finish()
    except Exception as exc:  # noqa: BLE001
        return client.http_status, exc
    return client.http_status, None


def said(error):
    return f"{type(error).__name__}: {error}"


def run_on_badge():
    import time

    import fetch
    import wifi

    with open(BADGE_ADDRESS_FILE) as handle:
        host, port = handle.read().split()

    while not wifi.connect():
        time.sleep_ms(50)

    print(f"firmware {os.uname()[3]}")
    print(f"against {host}:{port}\n")

    client = fetch.AsyncFetch(host, int(port), use_tls=False)

    status, error = attempt(client, OK_PATH)
    check("a clean fetch lands", error is None and status == 200,
          said(error) if error else f"status {status}")

    for path in FAULTS:
        status, error = attempt(client, path)
        check(f"{path} raises", error is not None, f"no error, status {status}")

        status, error = attempt(client, OK_PATH)
        check(f"the same client fetches after {path}",
              error is None and status == 200 and bytes(client.body) == OK_BODY,
              said(error) if error else f"status {status}, body {bytes(client.body)!r}")

    print()
    print(f"{len(failures)} failed" if failures else "fetch recovers from every fault")


if __name__ == "__main__":
    if os.uname().sysname == "rp2":
        run_on_badge()
    else:
        serve()
