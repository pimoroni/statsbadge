"""statsbadge command line: serve, pair, install, probe."""

import argparse
import getpass
import json
import os
import sys
import threading
import time

from . import auth, beacon, extensions, install, layout, server, tooling
# Named apart from the `version` locals in this module, which are extensions' own.
from . import version as package_version


LEGACY_CONFIG_DIR = os.path.join(os.path.expanduser("~/.config"), "statsbadge")


def config_dir(explicit=None):
    """Where layout.json, badges.json and server.json live.

    Each platform's own location, since ~/.config on Windows is just a dotfile in the
    home directory. XDG_CONFIG_HOME wins anywhere it is set, for people who keep their
    configuration somewhere deliberate.

    An existing ~/.config/statsbadge keeps being used: it holds pairing secrets, and
    moving those without being asked would strand a paired badge.
    """
    if explicit:
        return os.path.abspath(explicit)
    if os.environ.get("XDG_CONFIG_HOME"):
        return os.path.join(os.environ["XDG_CONFIG_HOME"], "statsbadge")
    if os.path.isdir(LEGACY_CONFIG_DIR):
        return LEGACY_CONFIG_DIR
    return os.path.join(_platform_config_base(), "statsbadge")


def _platform_config_base():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    if os.name == "nt":
        # LOCALAPPDATA, not APPDATA: the server id here identifies this machine, and a
        # roaming profile would carry it to another one as a duplicate.
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return base or os.path.expanduser("~")
    return os.path.expanduser("~/.config")


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


def source_config_from(args):
    """What the sources are told. Settings stored by the UI beat --extension.

    Read here as well as by the Service, because `probe` and `install` load extensions
    without one and should see the same configuration the server would.
    """
    stored = layout.Config(
        os.path.join(config_dir(getattr(args, "config_dir", None)), "layout.json")
    ).snapshot().get("settings")
    return {
        "powermetrics": getattr(args, "powermetrics", False),
        "lhm_url": getattr(args, "lhm_url", None),
        "iface": getattr(args, "iface", None),
        "disk_path": getattr(args, "disk_path", None),
        "extensions": layout.merge_settings(
            parse_extension_options(getattr(args, "extension", None)), stored),
        "disabled_extensions": getattr(args, "without", None) or [],
    }


def build_service(args):
    return server.Service(config_dir(args.config_dir),
                          interval=args.interval,
                          source_config=source_config_from(args))


def extension_modules(args):
    """Badge-side modules from installed extensions, unless asked not to.

    Loaded directly instead of through a Service, so an install does not start a
    collector it will only stop again. --without NAME leaves that one out of both the
    frame and the badge, which is what it should mean.
    """
    if getattr(args, "no_extensions", False):
        return []
    return extensions.badge_modules(extensions.load(source_config_from(args)))


# -- serve ------------------------------------------------------------------

def _extension_line(record):
    """One extension on the startup line: its name, and what went wrong if anything did.

    A pip install that did not take is otherwise invisible until a page fails to appear.
    """
    if record.get("error"):
        return f"{record['name']} (failed)"
    if record.get("available") is False:
        return f"{record['name']} (not usable here)"
    return record["name"]


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
        print("  extensions:        {}".format(", ".join(
            _extension_line(record) for record in caps["extensions"])))
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
    # The port is carried in a dict because it changes when the badge is reset, and the
    # reset on the way out has to use whichever one it ended up on.
    session = {"port": ports[0]}
    print("badge on {}".format(session["port"]))
    try:
        return _install(args, session)
    finally:
        install.hard_reset(session["port"])
        print("The badge has been reset.")


def _install(args, session):
    port = session["port"]
    try:
        info = install.badge_info(port)
    except install.InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("  model {}, uid {}, app {}".format(
        info["model"], info["uid"],
        "already installed" if info["app_installed"] else "not installed"))

    # Bytecode is preferred when it matches this badge, and the .py sources are the
    # fallback: they load on any firmware.
    source, modules = None, []
    if not args.state_only:
        try:
            source, note = install.choose_app_source(args.mpy, args.source, info["mpy"])
        except install.InstallError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"  {note}")
        modules = extension_modules(args)
        if modules:
            # One name per extension, however many files it contributes.
            print("  extensions: {}".format(
                ", ".join(sorted({name for name, _ in modules}))))

    # What would change, so the mass storage reset is only paid when it buys something.
    added, changed, removed = [], [], []
    if not args.state_only:
        try:
            added, changed, removed = install.app_changes(
                install.installed_hashes(port),
                install.desired_hashes(source, modules))
        except install.InstallError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for label, names in (("new", added), ("changed", changed), ("stale", removed)):
            if names:
                print(f"  {label}: {', '.join(names)}")
        if not (added or changed or removed):
            print("  the app on the badge is already up to date")

    server_id, server_name = None, None
    secret, start_seq, write_credentials = None, 0, False
    if not args.app_only:
        directory = config_dir(args.config_dir)
        badges = auth.Store(os.path.join(directory, "badges.json"))
        if badges.unreadable:
            print(f"error: {badges.path} cannot be read ({badges.unreadable}).",
                  file=sys.stderr)
            print("Running the server with sudo leaves it owned by root. Fix its "
                  "ownership, or pass --config-dir.", file=sys.stderr)
            return 1
        secret = badges.secret_for(info["uid"])
        service = build_service(args)
        server_id = service.identity["id"]
        server_name = service.identity["name"]
        service.collector.stop()
        # Credentials the badge already holds for this server are left alone: a repeat
        # install is then only a code update, with nothing to lose if it is interrupted.
        held = install.secret_in_state(install.read_state(port), server_id)
        if args.new_secret or not secret:
            secret = badges.provision(info["uid"], args.name)
            print(f"  minted a new secret ({auth.fingerprint(secret)})")
            write_credentials = True
        elif held != secret:
            start_seq = badges.list_badges().get(info["uid"], {}).get("seq", 0)
            print("  reusing the existing secret for this badge "
                  f"(counter at {start_seq})")
            write_credentials = True
        else:
            print(f"  already paired with {server_name}; credentials left alone")

    if args.mpy and args.state_only:
        print("  note: --mpy does nothing with --state-only, which writes credentials only")

    host = args.server_host or (server._local_addresses() or ["127.0.0.1"])[0]

    # The app goes on first, credentials second. Writing /state over the REPL and then
    # resetting into mass storage loses the write: the reset discards it and the volume
    # commits whatever was there before, which with --new-secret would leave the badge
    # holding a secret the host has already replaced.
    copying = bool(added or changed or removed) or args.force_app
    if not args.state_only and (copying or args.ssid):
        if not args.yes:
            print()
            print("This needs the badge's USB volume, which means resetting it into")
            print("mass storage mode.")
            reply = input("Continue? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Nothing was changed.")
                return 0
        print("  switching to mass storage...")
        install.enter_mass_storage(port)
        volume = install.wait_for_volume()
        print(f"  volume at {volume}")
        if copying:
            target, copied, gone = install.copy_app(volume, source=source,
                                                    extra_modules=modules)
            print(f"  copied {len(copied)} files to {target}")
            if gone:
                print(f"  removed {len(gone)}: {', '.join(gone)}")
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
        session["port"] = port
        print(f"  back on {port}")

    if args.app_only:
        print("\nDone. App only; no credentials were written.")
        print("Pair it from the badge: run 'statsbadge pair', then press B on the badge.")
        return 0

    if write_credentials:
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


# -- status -----------------------------------------------------------------

def cmd_status(args):
    """What is on the badge and what this host knows, without touching anything."""
    directory = config_dir(args.config_dir)
    print(f"host: {directory}")
    service = build_service(args)
    print(f"  server:     {service.identity['name']} ({service.identity['id']})")
    paired = service.badges.list_badges()
    unreadable = service.badges.unreadable
    service.collector.stop()
    if unreadable:
        print(f"  badges:     cannot be read: {unreadable}")
    else:
        print("  badges:     {}".format(", ".join(paired) if paired else "none paired"))
    loaded = [e["name"] for e in extensions.describe() if e["available"]]
    print("  extensions: {}".format(", ".join(loaded) if loaded else "none"))

    print()
    ports = [args.port_dev] if args.port_dev else install.find_ports()
    if not ports:
        print("badge: not connected by USB")
        return 0
    port = ports[0]
    try:
        return _badge_status(args, port)
    finally:
        install.hard_reset(port, settle=False)


def _badge_status(args, port):
    try:
        info = install.badge_info(port)
    except install.InstallError as exc:
        print(f"badge: on {port}, but {exc}")
        return 1
    print(f"badge: {info['model']} on {port}")
    print(f"  uid:        {info['uid']}")
    print("  wifi:       {}".format(install.wifi_network(port) or "not set"))
    print("  app:        {}".format("installed" if info["app_installed"]
                                    else "not installed"))
    if info["app_installed"]:
        try:
            source, _note = install.choose_app_source(None, args.source, info["mpy"])
            added, changed, removed = install.app_changes(
                install.installed_hashes(port),
                install.desired_hashes(source, extension_modules(args)))
        except install.InstallError as exc:
            print(f"              cannot compare: {exc}")
        else:
            if added or changed or removed:
                print("              differs from this package: {}".format(
                    ", ".join(added + changed + removed)))
                print("              run 'statsbadge install' to update it")
            else:
                print("              up to date with this package")

    state = install.read_state(port) or {}
    hosts = state.get("hosts") or {}
    if not hosts:
        print("  paired:     nothing yet")
    for server_id, entry in hosts.items():
        mark = " (active)" if server_id == state.get("active") else ""
        print("  paired:     {} at {}:{} seq={}{}".format(
            entry.get("name") or server_id, entry.get("host"), entry.get("port"),
            entry.get("seq"), mark))
    return 0


# -- extensions -------------------------------------------------------------

def cmd_extensions(args):
    """List the extensions on this host, or change which ones are installed."""
    verb = getattr(args, "verb", None) or "list"
    if verb != "list":
        return _change_extensions(args, verb)

    directory = config_dir(args.config_dir)
    found = extensions.describe()
    if not found:
        print("no extensions installed")
        print(_how_to_add("clock"))
        return 0
    for record in found:
        state = "ok" if record["available"] else (
            "not available here" if record["loaded"] else "failed to import")
        version = f" {record['version']}" if record["version"] else ""
        print(f"{record['name']}{version}  {state}")
        if record["provides"]:
            print("  provides:     {}".format(", ".join(record["provides"])))
        if record["badge_module"]:
            print(f"  badge module: {record['badge_module']}")
        if record["error"]:
            print(f"  error:        {record['error']}")

    wanted = tooling.read_wanted(directory)
    if wanted:
        print()
        print(f"asked for in {tooling.wanted_path(directory)}:")
        for requirement in wanted:
            print(f"  {requirement}")
        loaded = {record["name"] for record in found}
        adrift = [r for r in wanted if tooling.short_name(r) not in loaded]
        if adrift:
            print("  not installed yet: {}".format(", ".join(adrift)))
            print("  run: statsbadge ext sync")
    elif tooling.as_uv_tool():
        # Installed with --with rather than from the list, so say where the list would be: the
        # next `uv tool install` replaces the environment and would drop them.
        print()
        print(f"no {tooling.WANTED} yet. The first `statsbadge ext add` writes one, taking")
        print("what this tool was installed with as its starting point.")
    return 0


def _how_to_add(name):
    """The line to run to install an extension, for however statsbadge itself was installed."""
    receipt = tooling.as_uv_tool()
    if receipt and tooling.base_requirement(receipt):
        return f"try: statsbadge ext add {name}"
    return f"try: uv pip install {tooling.PREFIX}{name}"


def _change_extensions(args, verb):
    """add, remove or sync: keep `extensions.txt` and the tool environment in step."""
    directory = config_dir(args.config_dir)
    receipt = tooling.as_uv_tool()
    before = tooling.read_wanted(directory)
    had_list = os.path.isfile(tooling.wanted_path(directory))
    wanted = list(before)
    if not wanted and receipt:
        # Nothing written down yet, but uv remembers what the tool was built with. Adopting that
        # is the whole point: `ext add` on a tool installed with --with would otherwise reinstall
        # naming only the new one, and drop everything already there.
        wanted = tooling.installed_beside(receipt)

    changed = []
    if verb == "add":
        for name in args.names:
            requirement = tooling.as_requirement(name)
            if tooling.short_name(requirement) in tooling.names(wanted):
                print(f"already installed: {tooling.short_name(requirement)}")
                continue
            wanted.append(requirement)
            changed.append(requirement)
    elif verb == "remove":
        for name in args.names:
            requirement = tooling.as_requirement(name)
            matches = [r for r in wanted
                       if r == requirement
                       or tooling.short_name(r) == tooling.short_name(requirement)]
            if not matches:
                print(f"not installed: {tooling.short_name(requirement)}")
                continue
            for match in matches:
                wanted.remove(match)
                changed.append(match)
    if verb != "sync" and not changed:
        return 0

    base = tooling.base_requirement(receipt) if receipt else None
    if base is None:
        # Not a uv tool, or a receipt this cannot read: say what to run instead, and leave the
        # list as it was rather than recording something that has not happened.
        print("statsbadge is not installed as a uv tool, so there is nothing here to rebuild.")
        for requirement in (changed or wanted):
            print(f"  uv pip install {requirement}")
        return 0

    tooling.write_wanted(directory, wanted)
    doing = "installing" if verb != "remove" else "removing"
    print(f"{doing} {', '.join(tooling.short_name(r) for r in changed or wanted)}...")
    ok, why = tooling.run_install(base, directory, fresh=verb != "add",
                                  verbose=args.verbose)
    if ok:
        print("done. Run `statsbadge install` to push any badge-side code they ship.")
        return 0

    # Put the list back: it records what is installed, and nothing was.
    if had_list:
        tooling.write_wanted(directory, before)
    else:
        tooling.forget_wanted(directory)
    print(why, file=sys.stderr)
    return 1


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

INSTALL_EXAMPLES = """
examples:
  statsbadge install --ssid "My Network"   a new badge: app, extensions, WiFi, pairing
  statsbadge install                       update it; only what changed is copied, and
                                           the badge is not reset if nothing did
  statsbadge install --force-app           copy regardless
  statsbadge install --no-extensions       leave extension modules off
  statsbadge install --without clock       everything except that extension
  statsbadge install --ssid "Other" --force-secrets    change the WiFi it uses
  statsbadge install --new-secret          re-key this badge

'statsbadge update' is the same command. 'statsbadge status' says what is on the badge.
"""

def main(argv=None):
    parser = argparse.ArgumentParser(prog="statsbadge",
                                     description="System stats for a Badgeware badge")
    parser.add_argument("--config-dir", help="where to keep layout and pairings")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between samples (default 1.0)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version",
                        version=f"statsbadge {package_version()}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="0.0.0.0")
    common.add_argument("--port", type=int, default=8420)
    common.add_argument("--powermetrics", action="store_true",
                        help="macOS: run powermetrics as root for power and temps")
    common.add_argument("--lhm-url", help="Windows: LibreHardwareMonitor data.json URL")
    common.add_argument("--iface", help="network interface to report (default: busiest)")
    common.add_argument("--disk-path",
                        help="filesystem to report. Defaults to the volume "
                             "holding your files, which on macOS is not /")
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

    inst = subs.add_parser("install", parents=[common], aliases=["update"],
                           help="push or update the app and credentials over USB",
                           epilog=INSTALL_EXAMPLES,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
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
    inst.add_argument("--no-extensions", action="store_true",
                     help="do not push badge-side modules from installed extensions")
    # Accepted and ignored: extensions go on by default now, and scripts that passed
    # this should keep working.
    inst.add_argument("--with-extensions", action="store_true",
                     help=argparse.SUPPRESS)
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

    status = subs.add_parser("status", parents=[common],
                             help="what is on the badge, and what this host knows")
    status.add_argument("--port-dev", help="serial port (default: autodetect)")
    status.add_argument("--source", action="store_true",
                        help="compare against the .py sources, not the bytecode")
    status.set_defaults(func=cmd_status)

    exts = subs.add_parser("extensions", parents=[common], aliases=["ext"],
                           help="list extensions, or add and remove them")
    exts.set_defaults(func=cmd_extensions, verb="list", names=[])
    verbs = exts.add_subparsers(dest="verb", metavar="add|remove|sync")
    for verb, what in (("add", "install an extension and remember it"),
                       ("remove", "uninstall an extension and forget it"),
                       ("sync", "install whatever the list names")):
        step = verbs.add_parser(verb, parents=[common], help=what)
        step.set_defaults(func=cmd_extensions, verb=verb, names=[])
        if verb != "sync":
            step.add_argument("names", nargs="+", metavar="NAME",
                              help="a short name like clock, or any pip requirement")

    badges = subs.add_parser("badges", help="list or forget paired badges")
    badges.add_argument("--forget", metavar="BADGE_ID")
    badges.set_defaults(func=cmd_badges)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
