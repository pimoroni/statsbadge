"""Driving an install: the order the pieces in install.py go in.

The app is copied before credentials are written. Resetting into mass storage discards a
write to /state, and a badge re-keyed that way round holds a secret this host has already
replaced.

Nothing here reads a terminal. Progress goes to `say`. The two questions - whether to reset
into mass storage, and a WiFi password - are asked through callbacks, which lets the config
UI settle both in the request that starts the install.
"""

import os

from . import auth, install, pushed


def _quiet(_text):
    pass


def push(options, badges=None, identity=None, modules=(), say=None, confirm=None,
         password=None):
    """Push the app and this host's credentials to a badge over USB.

    `options` mirrors the flags `statsbadge install` takes. Returns what happened; the
    caller decides what to print and what to call an error.
    """
    say = say or _quiet
    ports = ([options["port_dev"]] if options.get("port_dev")
             else install.find_ports())
    if not ports:
        return _answer("No badge found. Connect it by USB, or pass --port-dev.")
    # In a dict because the port changes when the badge resets, and the reset on the way
    # out has to use whichever one it ended up on.
    session = {"port": ports[0]}
    say("badge on {}".format(session["port"]))
    try:
        return _push(options, session, badges, identity, modules, say, confirm, password)
    finally:
        if install.hard_reset(session["port"]):
            say("The badge has been reset.")


def _answer(error, **rest):
    answer = {"ok": False, "error": error, "cancelled": False, "badge": None,
              "model": None, "copied": [], "added": [], "changed": [], "removed": [],
              "wifi": None, "credentials": False, "up_to_date": False}
    answer.update(rest)
    return answer


def _push(options, session, badges, identity, modules, say, confirm, password):
    port = session["port"]
    state_only = bool(options.get("state_only"))
    app_only = bool(options.get("app_only"))
    try:
        info = install.badge_info(port)
    except install.InstallError as exc:
        return _answer(str(exc))
    say("  model {}, uid {}, app {}".format(
        info["model"], info["uid"],
        "already installed" if info["app_installed"] else "not installed"))
    answer = _answer(None, ok=True, badge=info["uid"], model=info["model"])

    # Bytecode is preferred when it matches this badge, and the .py sources are the
    # fallback, since they load on any firmware.
    source, desired = None, None
    if not state_only:
        try:
            source, note = install.choose_app_source(
                options.get("mpy"), options.get("source"), info["mpy"])
        except install.InstallError as exc:
            return _answer(str(exc), badge=info["uid"], model=info["model"])
        say(f"  {note}")
        if modules:
            # One name per extension, however many files it contributes.
            say("  extensions: {}".format(", ".join(sorted({n for n, _ in modules}))))

    # What would change. The mass storage reset is only paid when it buys something.
    if not state_only:
        try:
            desired = install.desired_hashes(source, modules)
            answer["added"], answer["changed"], answer["removed"] = install.app_changes(
                install.installed_hashes(port), desired)
        except install.InstallError as exc:
            return _answer(str(exc), badge=info["uid"], model=info["model"])
        for label, key in (("new", "added"), ("changed", "changed"), ("stale", "removed")):
            if answer[key]:
                say(f"  {label}: {', '.join(answer[key])}")
        answer["up_to_date"] = not (answer["added"] or answer["changed"]
                                    or answer["removed"])
        if answer["up_to_date"]:
            say("  the app on the badge is already up to date")

    server_id, server_name = None, None
    secret, start_seq, write_credentials = None, 0, False
    if not app_only:
        if badges is None or identity is None:
            return _answer("no host to pair with", badge=info["uid"],
                            model=info["model"])
        if badges.unreadable:
            return _answer(
                f"{badges.path} cannot be read ({badges.unreadable}).\n"
                "Running the server with sudo leaves it owned by root. Fix its "
                "ownership, or pass --config-dir.",
                badge=info["uid"], model=info["model"])
        secret = badges.secret_for(info["uid"])
        server_id, server_name = identity["id"], identity["name"]
        # Credentials the badge already holds for this server are left alone. A repeat
        # install is then only a code update, with nothing to lose if it is interrupted.
        held = install.secret_in_state(install.read_state(port), server_id)
        if options.get("new_secret") or not secret:
            secret = badges.provision(info["uid"], options.get("name"))
            say(f"  minted a new secret ({auth.fingerprint(secret)})")
            write_credentials = True
        elif held != secret:
            start_seq = badges.list_badges().get(info["uid"], {}).get("seq", 0)
            say(f"  reusing the existing secret for this badge (counter at {start_seq})")
            write_credentials = True
        else:
            say(f"  already paired with {server_name}; credentials left alone")

    if options.get("mpy") and state_only:
        say("  note: --mpy does nothing with --state-only, which writes credentials only")

    # Before the badge is touched. write_secrets checks it again against the badge's own
    # list, by which point the app is copied and the volume is mounted.
    region = options.get("region")
    if region and region.lower() not in install.REGIONS:
        return _answer(f"{region} is not a WiFi region the badge knows. One of: "
                       f"{', '.join(install.REGIONS)}",
                       badge=info["uid"], model=info["model"])

    ssid = options.get("ssid")
    if ssid and options.get("password") is None:
        if password is None:
            return _answer(f"no password for {ssid!r}", badge=info["uid"],
                           model=info["model"])
        # Before the badge is touched. A prompt going up over a board already sitting in
        # mass storage mode leaves it there while it waits.
        options = dict(options, password=password(ssid))

    copying = not answer["up_to_date"] or bool(options.get("force_app"))
    if not state_only and (copying or ssid):
        if not options.get("yes") and confirm and not confirm():
            answer["cancelled"] = True
            return answer
        say("  switching to mass storage...")
        try:
            install.enter_mass_storage(port)
            volume = install.wait_for_volume()
        except install.InstallError as exc:
            return _answer(str(exc), badge=info["uid"], model=info["model"])
        say(f"  volume at {volume}")
        try:
            if copying:
                target, copied, gone = install.copy_app(volume, source=source,
                                                        extra_modules=modules)
                answer["copied"] = copied
                say(f"  copied {len(copied)} files to {target}")
                if gone:
                    say(f"  removed {len(gone)}: {', '.join(gone)}")
            if ssid:
                answer["wifi"] = _set_wifi(options, volume, ssid, say)
        except (install.InstallError, OSError) as exc:
            # The volume is left mounted. Whatever went wrong, it is halfway through the
            # app directory, and running this again is what puts it right.
            install.eject(volume)
            return _answer(f"{exc}. Run this again once the badge comes back.",
                           badge=info["uid"], model=info["model"])
        install.eject(volume)
        say("  ejected; waiting for the badge to come back...")
        try:
            port = install.wait_for_port(previous=port)
        except install.InstallError as exc:
            return _answer(str(exc), badge=info["uid"], model=info["model"])
        session["port"] = port
        say(f"  back on {port}")

    if not state_only and desired is not None and options.get("config_dir"):
        # After the copy: a failed one leaves the record saying what is really there.
        pushed.record(options["config_dir"], info["uid"], desired, source)

    if app_only:
        return answer

    if write_credentials:
        install.write_state(port, options.get("host") or "127.0.0.1",
                            options.get("port") or 8420, secret, info["uid"],
                            seq=start_seq, server_id=server_id, name=server_name)
        say("  wrote {}: {} at {}:{}".format(install.STATE_FILE, server_name,
                                             options.get("host"), options.get("port")))

        # Read it back: this is the write that must have survived.
        written = install.read_state(port)
        if install.secret_in_state(written, server_id) != secret:
            return _answer("the credentials did not stick. Try again, or reset the "
                            "badge.", badge=info["uid"], model=info["model"])
        answer["credentials"] = True
        others = [key for key in (written.get("hosts") or {}) if key != server_id]
        if others:
            names = ", ".join((written["hosts"][key].get("name") or key)
                              for key in others)
            say(f"  also still paired with: {names}")
    return answer


def _set_wifi(options, volume, ssid, say):
    """Set the badge's WiFi details, unless it has some and nobody said to replace them."""
    if not options.get("force_secrets") and install.wifi_configured(volume):
        say("  WiFi is already set; --force-secrets to replace it")
        return "kept"
    written = install.write_secrets(volume, ssid, options["password"],
                                    options.get("region"), options.get("timezone"))
    say(f"  set {ssid!r} in {os.path.basename(written)}")
    return "set"
