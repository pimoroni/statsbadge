"""statsbadge command line: serve, pair, install, probe."""

import argparse
import getpass
import json
import os
import sys
import threading
import time

from . import auth, beacon, extensions, install, layout, server


def config_dir(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "statsbadge")


def parse_extension_options(pairs):
    """Turn --extension clock.latitude=52.4 into {"clock": {"latitude": 52.4}}.

    Numbers and booleans are converted, because an extension asking for a latitude
    wants a float and every value off a command line is a string.
    """
    options = {}
    for pair in pairs or ():
        if "=" not in pair or "." not in pair.split("=", 1)[0]:
            raise SystemExit(f"--extension wants name.key=value, not {pair!r}")
        target, value = pair.split("=", 1)
        name, key = target.split(".", 1)
        options.setdefault(name, {})[key] = _coerce(value)
    return options


def _coerce(text):
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def build_service(args):
    source_config = {
        "powermetrics": getattr(args, "powermetrics", False),
        "lhm_url": getattr(args, "lhm_url", None),
        "iface": getattr(args, "iface", None),
        "disk_path": getattr(args, "disk_path", "/"),
        "extensions": parse_extension_options(getattr(args, "extension", None)),
        "disabled_extensions": getattr(args, "without", None) or [],
    }
    return server.Service(config_dir(args.config_dir),
                          interval=args.interval,
                          source_config=source_config)


# -- serve ------------------------------------------------------------------

def cmd_serve(args):
    service = build_service(args)
    service.start()
    httpd = server.make_server(service, args.host, args.port, args.verbose)

    announcer = None
    if not args.no_beacon:
        announcer = beacon.Beacon(args.port, service.identity["name"],
                                  service.identity["id"])
        announcer.start()

    caps = service.capabilities()
    print(f"statsbadge serving on http://{args.host}:{args.port}")
    for address in server._local_addresses():
        print(f"  badge should use:  {address}:{args.port}")
    print(f"  config UI:         http://127.0.0.1:{args.port}/")
    print("  sources:           {}".format(", ".join(
        s["name"] for s in caps["sources"])) or "none")
    print("  groups with data:  {}".format(", ".join(sorted(caps["available"]))) or "none")
    if caps["extensions"]:
        print("  extensions:        {}".format(", ".join(caps["extensions"])))
    paired = service.badges.list_badges()
    print("  paired badges:     %s" % (", ".join(paired) if paired else
                                       "none yet, run 'statsbadge pair'"))
    if announcer:
        print(f"  beacon:            broadcasting on udp/{beacon.PORT}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        if announcer:
            announcer.stop()
        service.stop()
    return 0


# -- pair -------------------------------------------------------------------

def cmd_pair(args):
    """Serve with a pairing window open from the start.

    Keeps serving afterwards: exiting once paired would strand the badge on a host that
    has gone away.
    """
    service = build_service(args)
    service.start()
    httpd = server.make_server(service, args.host, args.port, args.verbose)
    announcer = None
    if not args.no_beacon:
        announcer = beacon.Beacon(args.port, service.identity["name"],
                                  service.identity["id"])
        announcer.start()

    service.badges.begin_pairing(ttl=args.ttl)
    addresses = server._local_addresses()
    print(f"Pairing is open for {args.ttl} seconds, and this keeps serving afterwards.")
    print()
    print("  On the badge: launch Stats, press B to set up, and pick this host")
    print(f"    ({addresses[0] if addresses else 'this machine'}"
          f":{args.port} - the badge should find it by itself)")
    print()
    print("  The badge will show a code. Check it matches what appears here, then")
    print(f"  approve it - or use the config UI at http://127.0.0.1:{args.port}/")
    print("  Ctrl-C to stop.")
    print()

    threading.Thread(target=_approve_loop, args=(service, args.yes), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        if announcer:
            announcer.stop()
        service.stop()
    return 0


def _approve_loop(service, auto):
    """Show badges as they ask and take a yes or no. --yes skips the prompt."""
    seen = set()
    while True:
        try:
            pending = service.badges.pending_enrolments()
        except Exception:  # noqa: BLE001
            # This runs on a thread beside the server; if it dies the server should not.
            return
        for request in pending:
            if request["request_id"] in seen:
                continue
            seen.add(request["request_id"])
            print()
            print(f"  A badge wants to pair: {request['name']} ({request['badge_id']})")
            print(f"  It should be showing:  {request['code']}")
            if auto:
                service.badges.approve_enrolment(request["request_id"])
                print("  --yes given, approved.")
                continue
            try:
                answer = input("  Does that match the badge? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if answer in ("y", "yes"):
                badge_id = service.badges.approve_enrolment(request["request_id"])
                print(f"  approved {badge_id}; the badge should start drawing.")
            else:
                service.badges.deny_enrolment(request["request_id"])
                print("  denied.")
        time.sleep(0.5)


# -- install ----------------------------------------------------------------

def cmd_install(args):
    """Push the app and credentials to a USB-connected badge."""
    ports = [args.port_dev] if args.port_dev else install.find_ports()
    if not ports:
        print("No badge found. Connect it by USB, or pass --port-dev.", file=sys.stderr)
        return 1
    port = ports[0]
    print(f"badge on {port}")

    try:
        info = install.badge_info(port)
    except install.InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("  model {}, uid {}, app {}".format(
        info["model"], info["uid"],
        "already installed" if info["app_installed"] else "not installed"))

    secret, start_seq = None, 0
    if not args.app_only:
        directory = config_dir(args.config_dir)
        badges = auth.Store(os.path.join(directory, "badges.json"))
        secret = badges.secret_for(info["uid"])
        if secret and not args.new_secret:
            # Hand the badge the counter the host has already reached, so its first
            # request is neither a replay nor outside the window.
            start_seq = badges.list_badges().get(info["uid"], {}).get("seq", 0)
            print("  reusing the existing secret for this badge "
                  f"(counter at {start_seq})")
        else:
            secret = badges.provision(info["uid"], args.name)
            print(f"  minted a new secret ({auth.fingerprint(secret)})")

    # Bytecode is preferred when it matches this badge, and the .py sources are the
    # fallback: they load on any firmware.
    source = None
    if args.mpy and args.state_only:
        print("  note: --mpy does nothing with --state-only, which writes credentials only")
    if not args.state_only:
        try:
            source, note = install.choose_app_source(args.mpy, args.source, info["mpy"])
        except install.InstallError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"  {note}")

    host = args.server_host or (server._local_addresses() or ["127.0.0.1"])[0]

    # The app goes on first, credentials second. Writing /state over the REPL and then
    # resetting into mass storage loses the write: the reset discards it and the volume
    # commits whatever was there before, which with --new-secret would leave the badge
    # holding a secret the host has already replaced.
    # --ssid also needs the volume, so it is enough on its own to justify the trip.
    if not args.state_only and (not info["app_installed"] or args.force_app
                                or args.app_only or args.ssid):
        if not args.yes:
            print()
            print("Installing the app needs the badge's USB volume, which means")
            print("resetting it into mass storage mode.")
            reply = input("Continue? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Nothing was changed.")
                return 0
        print("  switching to mass storage...")
        install.enter_mass_storage(port)
        volume = install.wait_for_volume()
        print(f"  volume at {volume}")
        modules = []
        if args.with_extensions:
            service = build_service(args)
            modules = extensions.badge_modules(service.collector.extensions)
            service.collector.stop()
        if not (info["app_installed"] and not args.force_app and not args.app_only):
            target, copied = install.copy_app(volume, source=source,
                                              extra_modules=modules)
            print(f"  copied {len(copied)} files to {target}")
        if args.ssid:
            if args.password is None:
                # Prompted for, so it stays out of shell history.
                args.password = getpass.getpass(f"password for {args.ssid!r}: ")
            if args.force_secrets or not install.wifi_configured(volume):
                written = install.write_secrets(volume, args.ssid, args.password,
                                                args.region, args.timezone)
                print(f"  set {args.ssid!r} in {os.path.basename(written)}")
            else:
                print("  WiFi is already set; --force-secrets to replace it")
        install.eject(volume)
        print("  ejected; waiting for the badge to come back...")
        port = install.wait_for_port(previous=port)
        print(f"  back on {port}")
    elif not args.state_only:
        print("  app already installed, use --force-app to overwrite")

    if args.app_only:
        print("\nDone. App only; no credentials were written.")
        print("Pair it from the badge: run 'statsbadge pair', then press B on the badge.")
        return 0

    service = build_service(args)
    server_id = service.identity["id"]
    server_name = service.identity["name"]
    service.collector.stop()

    install.write_state(port, host, args.port, secret, info["uid"], seq=start_seq,
                        server_id=server_id, name=server_name)
    print(f"  wrote {install.STATE_FILE}: {server_name} at {host}:{args.port}")

    # Read it back: this is the write that must have survived.
    written = install.read_state(port)
    if install.secret_in_state(written, server_id) != secret:
        print("error: the credentials did not stick. Try again, or reset the badge.",
              file=sys.stderr)
        return 1
    others = [k for k in (written.get("hosts") or {}) if k != server_id]
    if others:
        names = ", ".join((written["hosts"][k].get("name") or k) for k in others)
        print(f"  also still paired with: {names}")

    if args.state_only:
        print("\nDone. Credentials only; the app itself was not touched.")
        return 0
    print("\nDone. Run 'statsbadge serve' and launch Stats on the badge.")
    return 0


# -- probe ------------------------------------------------------------------

def cmd_probe(args):
    """Print one frame of what this host can measure, and stop."""
    service = build_service(args)
    service.start()
    time.sleep(args.interval * 1.5)
    frame = service.collector.sample_once()
    caps = service.capabilities()
    service.stop()

    if args.json:
        print(json.dumps({"frame": frame, "capabilities": caps}, indent=2))
        return 0

    print("sources:")
    for source in caps["sources"]:
        note = ""
        if source["faults"]:
            note = f"  ({source['faults']} faults, last: {source['last_fault']})"
        provides = ",".join(source["provides"])
        print(f"  {source['name']:<24} provides {provides}{note}")
    print()
    for group in sorted(frame):
        if group in ("v", "t", "seq"):
            continue
        value = frame[group]
        if isinstance(value, list):
            for i, item in enumerate(value):
                print(f"{group}[{i}]  {_fmt(item)}")
            if not value:
                print(f"{group:<6} (nothing)")
        elif value:
            print(f"{group:<6} {_fmt(value)}")
        else:
            print(f"{group:<6} (nothing)")
    print()
    pages = layout.prune(layout.DEFAULT_PAGES, caps)
    print("default pages that survive on this host: {}".format(", ".join(
        p["id"] for p in pages)))
    return 0


def _fmt(mapping):
    parts = []
    for key, value in mapping.items():
        if isinstance(value, list):
            shown = ", ".join(str(v) for v in value[:4])
            more = f" +{len(value) - 4}" if len(value) > 4 else ""
            value = f"[{shown}]{more}"
        parts.append(f"{key}={value}")
    return "  ".join(parts)


# -- badges -----------------------------------------------------------------

def cmd_badges(args):
    directory = config_dir(args.config_dir)
    badges = auth.Store(os.path.join(directory, "badges.json"))
    if args.forget:
        print("forgotten" if badges.forget(args.forget) else "no such badge")
        return 0
    listing = badges.list_badges()
    if not listing:
        print("no badges paired")
        return 0
    for badge_id, record in listing.items():
        print("{}  {}  seq={}  paired {}".format(
            badge_id, record.get("name", ""), record.get("seq"),
            time.strftime("%Y-%m-%d", time.localtime(record.get("paired_at", 0)))))
    return 0


# -- argument parsing -------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="statsbadge",
                                     description="System stats for a Badgeware badge")
    parser.add_argument("--config-dir", help="where to keep layout and pairings")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between samples (default 1.0)")
    parser.add_argument("--verbose", action="store_true")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="0.0.0.0")
    common.add_argument("--port", type=int, default=8420)
    common.add_argument("--powermetrics", action="store_true",
                        help="macOS: run powermetrics as root for power and temps")
    common.add_argument("--lhm-url", help="Windows: LibreHardwareMonitor data.json URL")
    common.add_argument("--iface", help="network interface to report (default: busiest)")
    common.add_argument("--disk-path", default="/", help="filesystem to report")
    common.add_argument("--no-beacon", action="store_true",
                        help="do not broadcast the discovery beacon")
    common.add_argument("--extension", action="append", metavar="NAME.KEY=VALUE",
                        help="configure an installed extension, repeatable "
                             "(e.g. clock.latitude=52.4)")
    common.add_argument("--without", action="append", metavar="NAME",
                        help="disable an installed extension, repeatable")

    subs = parser.add_subparsers(dest="command", required=True)

    serve = subs.add_parser("serve", parents=[common], help="run the server")
    serve.set_defaults(func=cmd_serve)

    pair = subs.add_parser("pair", parents=[common],
                           help="show a pairing code for a badge on the network")
    pair.add_argument("--ttl", type=int, default=300)
    pair.add_argument("-y", "--yes", action="store_true",
                      help="approve without asking, for a scripted setup")
    pair.set_defaults(func=cmd_pair)

    inst = subs.add_parser("install", parents=[common],
                           help="push the app and credentials over USB")
    inst.add_argument("--port-dev", help="serial port (default: autodetect)")
    inst.add_argument("--server-host", help="address to bake in (default: this host's)")
    inst.add_argument("--name", help="a name for this badge")
    what = inst.add_mutually_exclusive_group()
    what.add_argument("--state-only", action="store_true",
                     help="write credentials only, never touch /system")
    inst.add_argument("--ssid", help="WiFi network to set in the badge's secrets.py, "
                                    "if it has none yet")
    inst.add_argument("--pass", dest="password",
                      help="password for --ssid. Prompted for if omitted; pass an empty "
                           "string for an open network")
    inst.add_argument("--region", help="WiFi region: us, eu, australia, nz and so on")
    inst.add_argument("--timezone", type=int, help="hours offset from GMT")
    inst.add_argument("--force-secrets", action="store_true",
                      help="replace WiFi details the badge already has")
    inst.add_argument("--source", action="store_true",
                     help="install the .py sources even if the package carries a "
                          "precompiled build")
    what.add_argument("--app-only", action="store_true",
                     help="copy the app only, minting nothing and writing no "
                          "credentials, so the badge can be paired from its own screen")
    inst.add_argument("--force-app", action="store_true", help="reinstall the app")
    inst.add_argument("--new-secret", action="store_true", help="mint a fresh secret")
    inst.add_argument("--with-extensions", action="store_true",
                     help="also push badge-side modules from installed extensions")
    inst.add_argument("--mpy", metavar="DIR", nargs="?", const="build/mpy",
                     help="install a precompiled app instead of the source. DIR is "
                          "either build/mpy, as left by ci/build-mpy.sh, or the stats/ "
                          "directory from an unzipped -mpy release. Defaults to "
                          "build/mpy if you give --mpy on its own. The bytecode version "
                          "is checked against the badge before anything is written")
    inst.add_argument("-y", "--yes", action="store_true", help="do not ask")
    inst.set_defaults(func=cmd_install)

    probe = subs.add_parser("probe", parents=[common],
                            help="print what this host can measure")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=cmd_probe)

    badges = subs.add_parser("badges", help="list or forget paired badges")
    badges.add_argument("--forget", metavar="BADGE_ID")
    badges.set_defaults(func=cmd_badges)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
