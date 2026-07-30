# statsbadge

Your PC's vitals on a Badgeware badge, paged with UP and DOWN. An Afterburner-style panel that is not welded to a keyboard.

![CPU](shots/live_cpu.png) ![Cores](shots/live_cores.png)

A server on the host measures things and serves them; the badge fetches and draws them. The host also serves a web page for choosing which screens appear, so rearranging the display needs no reinstall.

## Install

Uses [uv](https://docs.astral.sh/uv/). As a tool, so it lands on your PATH and keeps its own environment:

```bash
uv tool install statsbadge
statsbadge serve
```

Working on a checkout instead:

```bash
uv sync
uv pip install --no-deps ./extensions/statsbadge-clock   # optional, the example
uv run statsbadge serve
```

Or into whatever environment you already have, with pip or uv:

```bash
uv pip install statsbadge      # or from a checkout: uv pip install .
pip install statsbadge         # plain pip works too
```

Extras, if you want them: `statsbadge[nvidia]` for NVIDIA cards via NVML, `statsbadge[install]` for pushing the app to a badge over USB, `statsbadge[all]` for both.

With the badge on USB, in another terminal:

```bash
statsbadge install              # copies the app and pairs it
```

That writes the pairing secret over the serial REPL and, if the app is not already there, offers to reset the badge into USB mass storage mode to copy it. Answer no and you get credentials only.

No cable? `statsbadge pair` shows a 6-digit code, or open the config UI and press **Pair a badge**. Launch **Stats** on the badge, which finds the host from its broadcast, and spin the code in: UP/DOWN change a digit (hold to run), **A** back, **B** send, **C** next. Moving never erases, so one wrong digit costs one digit.

A server is not in pairing mode until you put it there, and the window closes on its own after five minutes or when you press **Stop pairing**. Wrong codes are rate limited rather than counted out, so nothing can lock you out of your own badge.

Then open <http://127.0.0.1:8420/> to pick screens, themes and button bindings.

## Changing IPs, and more than one computer

Credentials are keyed on a server id the host mints once, not on its address. So:

- **The host's IP changes.** The badge notices the polls failing, hears the host's beacon, recognises the id it is already paired with and follows it to the new address. Nothing to re-pair.
- **Two computers.** Pair with both - `statsbadge install` and `statsbadge pair` add a host rather than replacing one, each with its own secret and counter. The badge uses whichever it can reach, and switches by itself when the current one goes quiet.
- **Same computer, fresh install.** `install` reuses the existing secret unless you pass `--new-secret`, and folds an older single-host config in rather than orphaning it.

`statsbadge badges` lists what a host has paired; `--forget` drops one.

## On the badge

| Button   | What it does                                        |
| -------- | --------------------------------------------------- |
| UP/DOWN  | previous/next page                                  |
| A B C    | whatever the host has bound them to, if anything     |
| HOME     | hold to leave                                        |

## What it reports

| Group   | Fields                                                    |
| ------- | --------------------------------------------------------- |
| `cpu`   | load, per-core, temperature, clock, load average, processes |
| `mem`   | used, total, percentage, swap                              |
| `gpu`   | load, temperature, VRAM, power, clock, fan                 |
| `net`   | up/down rate and totals, interface                         |
| `disk`  | used, total, read/write rate                               |
| `power` | battery, charging, package watts                           |
| `fans`  | RPM                                                        |
| `sys`   | host, OS, CPU name, uptime                                 |

A field the host cannot measure is `null`, and pages that need it are dropped rather than shown empty. On macOS that means temperatures: they need root, so pass `--powermetrics` if you want them and have passwordless sudo. Windows needs [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running with its web server on for temperatures and fans. NVIDIA GPUs need `pip install statsbadge[nvidia]`.

## Page kinds and themes

Five kinds - `dial`, `bars`, `graph`, `grid`, `text` - and any field can go in any of them. Five themes: `afterburner`, `mono`, `amber`, `blueprint`, `vapor`. Everything is drawn as vector shapes taking their colours from one table, so a theme is a palette and not a set of images.

![Memory](shots/live_mem.png) ![Network](shots/live_net.png) ![Disk](shots/live_disk.png) ![Vapor](shots/theme_vapor.png)

## Extensions

An extension is a pip install. It adds data to the frame, and optionally badge-side code for a page the built-in kinds cannot draw:

```
pip install ./extensions/statsbadge-clock
statsbadge install --with-extensions
```

That one is a worked example: a Swiss railway clock whose second hand sweeps at the badge's frame rate, plus weather from Open-Meteo, which needs no API key.

![Clock](shots/swiss_clock.png)

See [DEVELOPMENT.md](DEVELOPMENT.md) for how to write one.

## Security

Plain HTTP on the LAN, with every request signed HMAC-SHA256 against a shared secret from pairing, and a counter the host refuses to accept twice. So a command cannot be forged or replayed, and an unpaired device on the network learns nothing. TLS is affordable on this hardware but buys little without certificate validation - [DEVELOPMENT.md](DEVELOPMENT.md) has the measurements. The config API is bound to loopback because it can mint secrets. Host commands only run if you have bound them to a button.

## Names

The repository is `pimoroni/stats-badge`; the package, the module and the command are all `statsbadge`. Keeping those three identical is deliberate - name them differently and uv_build needs its `module-name` setting, which older uv treats as a fatal parse error rather than a warning.

## Layout

```
src/statsbadge/            the host server, a normal Python package
src/statsbadge/badge_app/  the badge app - MicroPython, runs only on the badge
src/statsbadge/web/        the config UI
extensions/                one package per extension
tools/                     host and on-badge development tools
tests/                     server, auth and framing tests
```

The badge app lives inside the package so that an installed wheel carries it and `statsbadge install` can put it on a badge with no network. It is MicroPython and is never imported on the host.

## Working on it

```bash
uv sync                                             # dev environment
uv run statsbadge probe                             # what this host can measure
uv run python tests/test_core.py                    # server, auth, framing
uv run python tools/check_app.py                    # the app parses and is whole
uv run ruff check --config ci/ruff.toml src tools tests extensions
uv build                                            # sdist + wheel into dist/
ci/build-mpy.sh                                     # precompile the badge app

mpremote connect PORT mount . run tools/probe.py    # draw every page, time it
mpremote connect PORT mount . run tools/live.py     # talk to a real server
mpremote connect PORT mount . run tools/run.py      # the whole app, uninstalled
mpremote connect PORT mount . run tools/multihost_test.py   # pairing config
mpremote connect PORT mount . run tools/failover_test.py    # a changed host IP
uv run python tools/shots.py shots                  # framebuffer dumps to PNGs
```

A precompiled app loads in 66ms where the source takes 763ms, because the badge compiles at every launch. `ci/build-mpy.sh` builds it against whatever MicroPython the board repo pins, and `statsbadge install --mpy build/mpy` checks the bytecode version against the badge before writing anything.

Releases: tag `vX.Y.Z` matching the version in `pyproject.toml` and publish a GitHub release. CI builds the wheel, checks it carries the badge app, attaches both the source and precompiled app zips, and publishes to PyPI over trusted publishing - no API token to store.

[DEVELOPMENT.md](DEVELOPMENT.md) covers how it is put together and what each frame costs. It also explains why the server writes every response in a single `write()`, which is worth 30x on this hardware.
