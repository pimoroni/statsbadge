"""statsbadge command line: serve, pair, install, probe."""

import argparse
import getpass
import json
import os
import sys
import threading
import time
import webbrowser

from . import (auth, autostart, beacon, collect, extensions, install, layout, library,
               logs, runner, server, tooling)
# Named apart from the `version` locals in this module, which are extensions' own.
from . import version as package_version


LEGACY_CONFIG_DIR = os.path.join(os.path.expanduser("~/.config"), "statsbadge")
DEFAULT_PORT = 8420


def config_dir(explicit=None):
    """Where layout.json, badges.json and server.json live.

    Each platform's location, ~/.config on Windows being just a dotfile in the home
    directory. XDG_CONFIG_HOME wins wherever it is set.

    An existing ~/.config/statsbadge keeps being used: it holds pairing secrets, and moving
    those unasked would strand a paired badge.
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

    Numbers and booleans are converted, an extension asking for a latitude taking a
    float where every value off a command line is a string.
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
    try:
        stack = runner.Stack.start(service, args.host, args.port, args.verbose,
                                   announce=not args.no_beacon)
    except runner.AddressInUse as exc:
        return _report_in_use(exc)

    caps = service.capabilities()
    print(f"statsbadge serving on http://{args.host}:{args.port}")
    for address in stack.addresses():
        print(f"  badge should use:  {address}:{args.port}")
    print(f"  config UI:         http://127.0.0.1:{args.port}/")
    sources = ", ".join(source["name"] for source in caps["sources"])
    groups = ", ".join(sorted(caps["available"]))
    print(f"  sources:           {sources or 'none'}")
    print(f"  groups with data:  {groups or 'none'}")
    if caps["extensions"]:
        print("  extensions:        {}".format(", ".join(
            _extension_line(record) for record in caps["extensions"])))
    missing = tooling.adrift(config_dir(args.config_dir),
                             (record["name"] for record in caps["extensions"]))
    if missing:
        print("  not installed:     {} - run `statsbadge ext sync`".format(
            ", ".join(missing)))
        # Which venv, it need not be the one the reader is standing in.
        print(f"  running from:      {sys.prefix}")
    paired = auth.display_names(service.badges.list_badges())
    print("  paired badges:     %s" % (", ".join(paired) if paired else
                                       "none yet, run 'statsbadge pair'"))
    if stack.announcer:
        print(f"  beacon:            broadcasting on udp/{beacon.PORT}")

    try:
        stack.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stack.stop()
    return 0


def _report_in_use(exc):
    print(f"statsbadge: {exc}", file=sys.stderr)
    if exc.by:
        name = exc.by.get("name") or exc.by.get("id") or "another statsbadge"
        print(f"  {name} is already serving there.", file=sys.stderr)
    else:
        print(f"  try --port {exc.port + 1}", file=sys.stderr)
    return 2


# -- pair -------------------------------------------------------------------

def cmd_pair(args):
    """Serve with a pairing window open from the start.

    Keeps serving afterwards: exiting once paired would strand the badge on a host that
    has gone away.
    """
    service = build_service(args)
    try:
        stack = runner.Stack.start(service, args.host, args.port, args.verbose,
                                   announce=not args.no_beacon)
    except runner.AddressInUse as exc:
        return _report_in_use(exc)

    service.badges.begin_pairing(ttl=args.ttl)
    addresses = stack.addresses()
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
        stack.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stack.stop()
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
        if install.hard_reset(session["port"]):
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
    # resetting into mass storage loses the write. The reset discards it and the volume
    # commits whatever was there before, which with --new-secret leaves the badge holding
    # a secret the host has already replaced.
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
    """What is on the badge and what this host has, without touching anything."""
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
        named = auth.display_names(paired)
        print("  badges:     {}".format(", ".join(named) if named else "none paired"))
    found = extensions.describe()
    loaded = [e["name"] for e in found if e["available"]]
    print("  extensions: {}".format(", ".join(loaded) if loaded else "none"))
    missing = tooling.adrift(directory, (e["name"] for e in found))
    if missing:
        print("              not installed: {} - run `statsbadge ext sync`".format(
            ", ".join(missing)))

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
        missing = tooling.adrift(directory, (record["name"] for record in found))
        if missing:
            print("  not installed: {}".format(", ".join(missing)))
            print("  run: statsbadge ext sync")
    else:
        print()
        print(f"no {tooling.WANTED} yet. The first `statsbadge ext add` writes one.")
    return 0


def _how_to_add(name):
    return f"try: statsbadge ext add {name}"


def _change_extensions(args, verb):
    """add, remove or sync: keep `extensions.txt` and the tool environment in step."""
    directory = config_dir(args.config_dir)
    doing = "installing" if verb != "remove" else "removing"
    done = tooling.apply(
        directory, verb, args.names if verb != "sync" else (),
        {record["name"] for record in extensions.describe()}, verbose=args.verbose,
        announce=lambda these: print(
            f"{doing} {', '.join(tooling.short_name(r) for r in these)}..."))

    for short in done["already"]:
        print(f"already installed: {short}")
    for short in done["restored"]:
        print(f"{short} is asked for but not installed: putting it back.")
    for short in done["absent"]:
        print(f"not installed: {short}")
    if done["unknown"]:
        print(f"no such extension: {done['unknown']}", file=sys.stderr)
        print(f"  nothing on PyPI is called that. {tooling.WANTED} is unchanged.",
              file=sys.stderr)
        return 1

    changed, why = done["changed"], done["why"]
    if done["ok"]:
        if done["nothing"]:
            return 0
        for short in done["stuck"]:
            print(f"{short} is installed in the environment itself, so it is still here.",
                  file=sys.stderr)
            print(f"  whatever put it in {sys.prefix} has to take it out again.",
                  file=sys.stderr)
        print("done. Run `statsbadge install` to push any badge-side code they ship.")
        return 0

    print(why, file=sys.stderr)
    if "needs statsbadge" in (why or ""):
        # The library installs beside statsbadge and cannot move it, so the upgrade is the
        # answer and it is not one this can make.
        print("  upgrade statsbadge itself, then add it again.", file=sys.stderr)
        return 1
    # uv names one package, and it need not be one just asked for. The rebuild installs
    # the whole list, so an entry that was already there and cannot be installed fails
    # every add until it is taken out. Saying which is which is what makes the message
    # actionable.
    culprit = tooling.blamed(why)
    if culprit and tooling.short_name(culprit) not in tooling.names(changed):
        print(f"  {culprit} was already in {tooling.WANTED}, and nothing was changed. Take it "
              f"out with:", file=sys.stderr)
        print(f"    statsbadge ext remove {tooling.short_name(culprit)}", file=sys.stderr)
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
        if source["last_fault"]:
            note = f"  (failing: {source['last_fault']})"
        elif source["faults"]:
            count = source["faults"]
            note = f"  (recovered, {count} fault{'' if count == 1 else 's'} so far)"
        provides = ",".join(source["provides"])
        print(f"  {source['name']:<24} provides {provides}{note}")
    print()
    for group in sorted(frame):
        if group in collect.FRAME_SCALARS:
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


# -- tray -------------------------------------------------------------------

def cmd_tray(args):
    from . import tray as tray_app
    from .tray import backend

    if args.check:
        stopped = backend.why_not()
        print(stopped or f"tray backend: {backend.name()}")
        return 0

    directory = config_dir(args.config_dir)
    # Before anything prints. Under pythonw, and inside an .app, there is nowhere to.
    log_path = logs.start(directory) if not args.console else None

    stopped = backend.why_not()
    if stopped:
        print(stopped, file=sys.stderr)
        print("\nserving without one. Ctrl-C to stop.", file=sys.stderr)
        return cmd_serve(args)

    tray_app.block_signals()
    service = build_service(args)
    try:
        stack = runner.Stack.start(service, args.host, args.port, args.verbose,
                                   announce=not args.no_beacon)
    except runner.AddressInUse as exc:
        if exc.by:
            # Already serving, so hand the browser over instead of starting a second one.
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
            print(f"statsbadge is already serving on {args.port}")
            return 0
        return _report_in_use(exc)

    print(f"statsbadge tray on http://127.0.0.1:{args.port}/")
    app = tray_app.TrayApp(stack, log_path=log_path,
                           config_dir=args.config_dir, port=_asked_port(args),
                           launchd_log=logs.path(directory, "launchd"))
    tray_app.quit_on_signal(app.quit)
    try:
        return app.run(backend.Tray(app.title(), app.model), stack.serve_in_background)
    finally:
        stack.stop()


def _asked_port(args):
    return args.port if args.port != DEFAULT_PORT else None


# -- autostart --------------------------------------------------------------

def cmd_autostart(args):
    # Only what was asked for, or a login start would pin this run's defaults.
    kept = args.config_dir
    port = _asked_port(args)

    if args.verb == "enable":
        where = autostart.enable(config_dir=kept, port=port,
                                 log=logs.path(config_dir(args.config_dir), "launchd"))
        print(f"starting at login, from {where}")
        return 0
    if args.verb == "disable":
        print("no longer starting at login" if autostart.disable()
              else "was not starting at login")
        return 0

    state = autostart.describe(config_dir=kept, port=port)
    print("starts at login" if state["enabled"] else "does not start at login")
    print(f"  entry:   {state['where']}")
    print(f"  runs:    {tooling.quoted(state['command'])}")
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
    common.add_argument("--port", type=int, default=DEFAULT_PORT)
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

    tray = subs.add_parser("tray", parents=[common],
                           help="serve, with an icon in the tray or menu bar")
    tray.add_argument("--check", action="store_true",
                      help="report whether a tray works here, and stop")
    tray.add_argument("--console", action="store_true",
                      help="print to this terminal instead of the log file")
    tray.set_defaults(func=cmd_tray)

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
                       ("sync", ("reinstall whatever the list names, after an upgrade "
                                 "of statsbadge itself has replaced the environment"))):
        step = verbs.add_parser(verb, parents=[common], help=what)
        step.set_defaults(func=cmd_extensions, verb=verb, names=[])
        if verb != "sync":
            step.add_argument("names", nargs="+", metavar="NAME",
                              help="a short name like clock, or any pip requirement")

    badges = subs.add_parser("badges", help="list or forget paired badges")
    badges.add_argument("--forget", metavar="BADGE_ID")
    badges.set_defaults(func=cmd_badges)

    auto = subs.add_parser("autostart", parents=[common],
                           help="run the tray when you log in")
    auto.set_defaults(func=cmd_autostart, verb="status")
    auto_verbs = auto.add_subparsers(dest="verb", metavar="enable|disable")
    for verb, what in (("enable", "start the tray at login"),
                       ("disable", "stop starting the tray at login")):
        step = auto_verbs.add_parser(verb, parents=[common], help=what)
        step.set_defaults(func=cmd_autostart, verb=verb)

    args = parser.parse_args(argv)
    # Extensions live beside the config, so every command sees them. Swept first, while
    # nothing has imported out of a generation the last build replaced.
    directory = config_dir(getattr(args, "config_dir", None))
    library.sweep(directory)
    library.activate(directory)
    return args.func(args)


def tray_main(argv=None):
    """The gui-scripts entry point. Defaults to the tray, and takes its flags."""
    return main(["tray", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    sys.exit(main())
