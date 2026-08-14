# How it is put together

Two halves sharing one small contract. The host picks *what* to show and the badge picks *how*. The only things crossing the wire are a flat JSON frame and a page list.

| File | What is in it |
| ---- | ------------- |
| [`badge_app/__init__.py`](src/statsbadge/badge_app/__init__.py) | the app: paging, buttons, the poll loop, exit |
| [`badge_app/net.py`](src/statsbadge/badge_app/net.py) | the HTTP client, request signing, discovery, pairing |
| [`badge_app/draw.py`](src/statsbadge/badge_app/draw.py) | every widget, the text cache, the band cache |
| [`badge_app/pages.py`](src/statsbadge/badge_app/pages.py) | a page descriptor to a drawn page |
| [`badge_app/look.py`](src/statsbadge/badge_app/look.py) | the default theme and the 320x240 layout |
| [`badge_app/worldmap.py`](src/statsbadge/badge_app/worldmap.py) | the firmware's world map, for a page that draws on one |
| [`badge_app/setup.py`](src/statsbadge/badge_app/setup.py) | pairing on the badge, with no keyboard |
| [`badge_app/splash.py`](src/statsbadge/badge_app/splash.py) | the mark shown while the rest is still compiling |
| [`model.py`](src/statsbadge/model.py) | the frame's shape, and what "unknown" means |
| [`sources/`](src/statsbadge/sources/) | one file per way of measuring something |
| [`collect.py`](src/statsbadge/collect.py) | the sampling thread and the history rings |
| [`auth.py`](src/statsbadge/auth.py) | pairing, signing, replay refusal |
| [`identity.py`](src/statsbadge/identity.py) | the server id a badge keys its credentials on |
| [`layout.py`](src/statsbadge/layout.py) | page kinds, validation, pruning |
| [`server.py`](src/statsbadge/server.py) | the HTTP server, and the framing that makes it fast |
| [`install.py`](src/statsbadge/install.py) | pushing the app and credentials over USB |
| [`push.py`](src/statsbadge/push.py) | the order those go in, for the CLI and the config UI alike |
| [`pushed.py`](src/statsbadge/pushed.py) | what each badge was last seen holding, so a stale one shows |
| [`runner.py`](src/statsbadge/runner.py) | collector, server and beacon, started and stopped together |
| [`tray/`](src/statsbadge/tray/) | the menu bar icon, its menu, and the pystray adapter |
| [`autostart.py`](src/statsbadge/autostart.py) | run at login, one backend per platform |
| [`logs.py`](src/statsbadge/logs.py) | where a process with no terminal prints |
| [`library.py`](src/statsbadge/library.py) | the extensions directory beside the config, and its generations |
| [`tooling.py`](src/statsbadge/tooling.py) | `extensions.txt`, and turning a change to it into a build |

## The frame

One shape across every platform, with anything the host cannot answer set to `None`. Never
zero, never absent, so the badge can separate an idle GPU from an unmeasurable one and draw
`--`. That distinction is load-bearing twice. The collector derives what a host *can* report
from the live frame, never from what a source claims. Pages whose fields do not exist are
pruned before the layout is sent.

Sampling runs on a thread on an interval. Requests serve the last frame, and ten badges cost
the same as one. macOS is the stingiest host. GPU and VRAM come from `ioreg` unprivileged, where
temperatures, fan RPM and package power need root, leaving `--powermetrics` opt-in.

## Transport and signing

Plain HTTP. Every `/v1/*` request carries `X-Badge-Id`, `X-Badge-Seq` and an HMAC-SHA256 over
method, path, counter and a digest of the body. The path includes the query string, because
the query changes the response. Signing is what stops a command being forged.

The counter only goes up, and the host rejects anything it has already seen. The badge cannot
write flash per request, so it persists in batches of 64 and jumps forward on boot, which
means the two ends can be far apart. A refusal carries `next_seq` and the badge resyncs in one
request, which is safe because the signature is verified *before* the counter is checked.

Pairing is off until asked for. A badge asks at `/v1/enrol` and shows a short code; approving
that request at the host is what pairs it. The code is minted per request, never derived from
`badge.uid`, which travels in clear as `X-Badge-Id`.

## One layout per badge

`layout.json` holds the default at the top level, and a per-badge entry under `badges` keyed by
badge id. `Config.layout_for(badge_id)` is the entry for that badge, or the default.
`for_badge` is the same thing pruned, with the palette resolved and the table of other badges
stripped. A badge is identified by the signature on its request, so `/v1/layout` and
`/v1/stats` need no query string.

Revisions come from one counter across the file, the highest anywhere plus one. A badge
refetches when the `layout_rev` in its signed stats frame moves, which leaves the other badges
alone. Extension settings stay at the top level, being the host's answer rather than a badge's.

Credentials are keyed on a server id from [`identity.py`](src/statsbadge/identity.py) and not
an address, so a host that changes address is still the same host. After three failed polls
the badge listens for beacons and follows one already in its table.

## Rules that will bite you

These are the ones that cost a day each to find.

**A `.py` beside a `.mpy` wins the import.** One stray source file silently undoes a
precompiled build, and nothing errors. `ci/build-mpy.sh` exits non-zero on it, and `install` prunes.

**Talking to the REPL stops whatever the badge was running**, so every command that touches it
hard resets on the way out. A serial port that answers MicroPython is still not a badge:
`install.check_board` reads `os.uname()[4]` first.

**A pen assignment allocates; a transform does not.** `screen.pen = <colour>` is 64 bytes,
`shape.circle` 416, a `mat3` 32, and re-aiming a shape that already exists is zero. So build
geometry once and re-aim it, group what shares a pen, and never build either inside a per-item
loop. The world map draws 288 polygons with 24 pens and two transforms for this reason.

**A string is only worth a picture the second time it is asked for.** `draw.label` bakes a
sprite on the second sighting and draws live on the first, because a value that moves every
poll is a key that never comes again. Labels under 40pt become sprites; larger text is drawn
where it stands, a blit being 187ns a pixel.

**`import os` and `import machine` cost 40ms on every call**, being searched down `sys.path`
each time where `math` and `gc` are built in. An import belongs at module scope, or behind a
one-off cache. `gc.mem_free()` and `micropython.mem_info()` both walk the heap at 44ms, which
rules either out of a frame.

**The collector needs telling when, on a heap this size.** `gc.threshold(256KB)` at launch,
and `App.sweep` collects once a second between frames while the screen holds still.

**A stale reading is not an authority on the time.** A frame carries the time it was polled
at, so a sync is only considered against a *new* reading, keyed on the frame's `seq`.

**`screen.raw` is R G B A, premultiplied, no byte swap.** Get it wrong and red and blue swap.

**A rebuild must not count the generation it is replacing.** Extensions install to
`<config>/lib/<tag>-<n>` with `--target`, which resolves against an empty directory and so
drags in a second copy of statsbadge, Pillow and psutil. `library.prune` drops whatever
this environment already carries at the same version. The live generation is on
`sys.path` by then, so counting it pruned every extension straight back out of the
generation installing it, and a rebuild emptied the library.

**Every build writes a fresh generation.** An imported `.pyd` cannot be replaced on
Windows, so a build lands in `<tag>-<n>.partial` and is renamed into place. Older
generations are swept at the next start, before anything has imported from them.
`Service.reload_extensions` then picks the new one up in place, `entry_points()` walking
`sys.path` on every call. One build at a time, under a lock, or the second resolves from
a list the first has already replaced.

**The tray owns the main thread, and the server does not.** `icon.run()` drives
NSApplication on macOS and pumps messages on Windows, so `serve_forever` runs on a thread
under it. That is the other way round from `serve`. Signals do not arrive either: Python
runs a handler on the main thread between bytecodes, and that thread is inside the
toolkit, so `tray` blocks SIGINT and SIGTERM and waits for them in `sigwait` on a thread.
`launchctl bootout` sends SIGTERM.

**A second bind to a listening port succeeds on Windows.** `Server` sets `SO_REUSEADDR`,
which there means the two split incoming connections, where macOS and Linux fail the bind.
So `tray` asks `/v1/hello` before binding, and that check is the only single-instance guard.

**`sys.stdout` is None under `pythonw`, and inside a `.app` bundle.** This package prints
about 145 times, so `logs.start` replaces the streams before anything else in `cmd_tray`.

**Nothing in `sample` may wait on a network.** Every source shares the collector's thread and
the first sample is taken during `start`, so a lookup that hangs holds up the launch.
`urlopen(timeout=...)` is no guard, since it does not cover name resolution. Fetch on a
separate thread started in `start`, and let `sample` serve what came back.

## Drawing

The badge draws at 90Hz vsync, so a page has to finish in under 14.7ms to hold 45fps and under
25.8ms for 30. Between those, milliseconds are free.

Anything repeated is baked and blitted; only what changes shape is drawn live. An
anti-aliased shape costs its edges and not its area, roughly 0.08ms of setup plus 8us an edge,
so the things to watch are the side count and the number of shapes. The raster path
(`screen.rectangle`, `hspan`, `vspan`) is 10ns a pixel, and a large axis-aligned fill belongs
there.

`screen.clip` is honoured by every drawing path except `blit`, and it narrows the work rather
than masking it. A page drawn through a transform has to clip to its band, or it reaches the
header and footer.

Every split page takes its geometry from `look`: `DIAL_C`, `DIAL_OUTER`, `READOUT_X`,
`READOUT_W` and rows from `look.readout_rows`. A badge module that picks its own centre or
margin moves under the reader when they press a button.

A theme is config, so the badge carries one. The palette travels in the layout and
`look.from_palette` builds a `Theme` from it. `derive.py` works in OKLCH, so "ink has to be
readable on the page" settles as arithmetic checked against WCAG 7 and 4.5.
[`themes.toml`](src/statsbadge/themes.toml) is the only place a palette is written down;
`themes.py` reads it. A theme names its colours or carries a `derived` spec built from the
accent chosen, and a special case cannot be written into a data file.

## The heap, the panel and the light sensor

Measured on the board. The constants they settle live in `badge_app/__init__.py`.

Left alone the collector runs only when an allocation fails, which on 8MB of PSRAM lets megabytes pile up and leaves the free list in pieces: 71KB largest contiguous run with 7MB free, from `tools/mem_probe.py`. A collect is 3.9ms. `GC_THRESHOLD` covers an animated page, where a frame allocates up to 15KB and a collect every seventeen frames amortises to 0.23ms. `COLLECT_EVERY_MS` sweeps a resting page, where the pause costs nothing.

`display.backlight` takes a 0-1 fraction and the firmware maps it onto the panel. `tools/backlight_floor.py` measures where a given fraction puts the bottom of that range.

The light sensor is a phototransistor a hand can shadow, read through the 12-bit ADC with a couple of counts of noise either way. With the room and the panel held still, 256 reads spanned 64-80 of the raw u16. That is nothing against the 4500 a lit room reads, but darkness reads 48 and `look.ambient_fraction` is logarithmic, so the noise costs most where the curve is steepest. `LIGHT_READS` of 16 is 256us and halves it.

## Extensions

A package advertising a `statsbadge.sources` entry point gets a group in the frame, which the
built-in page kinds can draw with no badge-side code at all. Set `badge_module` to a `.py` and
`statsbadge install` copies it into the app's `ext/` directory, where the app imports it at
startup and it registers itself in `pages.EXTRA`.

[`extensions/statsbadge-clock`](extensions/statsbadge-clock) is the worked example of all
three parts: a group the built-in kinds draw unaided, badge-side Python for a page they
cannot, and `store` for what the source worked out between runs.

**A group the model does not define has to be declared to be offered.** `groups` on the source
names it and its fields, for `collect.capabilities` to merge with the model tables. `prune`
reads the same list, so a page on an undeclared group is dropped before it reaches the badge.

**Declared settings are what the UI offers.** `settings` builds the fields under Extensions,
one answer per host. `page_settings` builds fields on each page of that extension's kinds, so
two pages can point at two places.

**`ext add` rebuilds a uv tool**, having no way to add to one: `uv tool install
--with-requirements` replaces the environment, hence the list in `extensions.txt`. An
extension requiring a newer statsbadge is therefore a constraint on the tool itself, and takes
it up with them.

For development, install editable or the running code stays the snapshot from the first
install:

```bash
uv pip install --python .venv/bin/python --no-deps -e ./extensions/statsbadge-clock
```

## Launch, and .mpy

The badge compiles from source at every launch, 763ms for the five modules. Precompiled they
import in **66ms**, and that is the default: CI runs `ci/build-mpy.sh` into `badge_app/mpy/`
before `uv build`, so a wheel carries both. `--source` forces the sources.

Bytecode only loads on the firmware it was built for, hence the sources shipping alongside.
The install compares `(flags << 8) | version` against `sys.implementation._mpy` and falls back
to the sources on a mismatch, because a wheel outlives a firmware release.

## Packaging

`src` layout, one project at the repo root, and the version comes from the tag. The badge app
lives *inside* the package at `src/statsbadge/badge_app/`, because an installed wheel has to
carry it or `statsbadge install` has nothing to push.

The backend is `hatchling` with `uv-dynamic-versioning`. Each extension sets `pattern-prefix`
so it reads the tags for its own package, which has to match the prefix its workflow fires on;
a test holds the two together. The precompiled app is gitignored, so `artifacts` names it
explicitly or the wheel quietly ships sources alone.

Several packages share this repository, and the release tag picks the one a release is for. A
plain `vN.N.N` tag is statsbadge; a prefixed tag such as `clock-vN.N.N` is that extension.
Every release fires every publish workflow, so each tests its tag prefix before doing any
work. Each package needs a workflow file to itself, because PyPI matches a publisher on the
filename that runs.

### The desktop apps

Briefcase builds the `.dmg` and the `.msi`, from `[tool.briefcase]` in `pyproject.toml`.

statsbadge goes in as a **wheel**, named in `requires`. Copied `sources` arrive stripped of
`.dist-info`, and both the extension mechanism and `version()` read installed metadata, so a
bundle built that way lists zero extensions and reports its version as unknown. What
`sources` points at is `packaging/statsbadge_tray/`, a shim whose one job is to call
`tray_main()`. It is named apart from the package because the app directory comes before
`app_packages` on `sys.path`, and a shim called `statsbadge` would shadow the real one.

The three extensions bundled with it are in `requires` too, as paths, and so are two things
a bundle turns out to need:

**certifi**, because a bundle arrives with an empty trust store. `get_default_verify_paths()` answers
None to both and every HTTPS request an extension makes cannot find an issuer, which reaches
the screen as a source that says it cannot be reached. `trust_store()` points `SSL_CERT_FILE`
at certifi where a machine offers it nothing, and leaves a stocked one alone. Telling the
two apart takes both halves: Windows loads 409 roots from the system store while naming a
file it lacks, Linux names a directory it searches on demand, and a bundle has each empty.

**pip**, because installing an extension takes an installer, and `-m pip` needs an
interpreter that a bundle leaves out: briefcase ships the Python library alone. So the app spawns
*itself* under `--be-pip`, a verb that runs pip's entry point through `runpy` instead of
starting a tray. In this process it would take over the root logger while a server is
running; as a child it is a windowed program, where `uv.exe` is a console one that flashes
a black box up mid-install. uv is still preferred wherever it is on the PATH.

`sys.executable` in a bundle is the app binary, which is why any of that matters.
`bundled()` in `__init__.py` is what notices, and a login entry names the app itself.

```bash
uv run python tools/icon.py                         # the .icns and .ico, from the same mark
uv run python tools/app_version.py                  # the tag, into the briefcase table
uv run --group packaging briefcase create macOS app --no-input      # or: windows
uv run --group packaging briefcase build macOS app --no-input
uv run --group packaging briefcase package macOS app --adhoc-sign --no-input
```

The version in `[tool.briefcase]` stays `0.0.0` in the repository, since the tag is the
version everywhere else and briefcase takes a static one. CI writes it in before packaging.

CI runs the built app before packaging it - `--check`, `ext`, `--be-pip --version` - since
all three faults above were invisible to every other job. `--check` reports the tray, the
roots it can verify against and what it installs with.

Ad-hoc signed, there being no Apple Developer account behind it, so the first launch needs
Open from the context menu. Notarising it later takes an account, a Developer ID certificate
and `briefcase package macOS app --identity`.

## Working on it

```bash
uv sync                                             # dev environment
uv pip install --no-deps ./extensions/statsbadge-clock
uv run statsbadge probe                             # what this host can measure
uv run pytest tests                                 # the whole suite
uv run python tools/check_app.py                    # the app parses and is whole
uv run ruff check --config ci/ruff.toml src tools tests extensions
npm ci && npm run lint                              # the config UI: js, css, html
uv build                                            # sdist + wheel
ci/build-mpy.sh                                     # precompile the badge app
```

### The badge app, without a badge

`tests/badge/wasm/` runs the app's own modules against the real firmware under the
badgeware WASM port: real picovector, real fonts, real `screen`. That is the only way
to test what draws - `pages.render` reaches `screen`, `image` and `tween`, so on a host
it can only be read as text, which is what the checks in `tests/badge/` still do.

Fetch a runtime once (a batteries-included build, which carries the badgeware package
and the fonts inside the wasm, so nothing else has to be staged):

```bash
gh release download v3.0.1 --repo pimoroni/badgeware-wasm \
  --pattern 'badgeware-tufty2350-batteries-jspi.zip'
unzip -q badgeware-tufty2350-batteries-jspi.zip -d build/badgeware-runtime
```

```bash
node tools/wasm/run.mjs                  # every module in tests/badge/wasm
node tools/wasm/run.mjs test_pages       # one of them
```

Needs node 25 or newer: a jspi build suspends through `WebAssembly.Suspending`.
`BADGEWARE_RUNTIME` points it at a build of your own. Not in CI yet, the release
being on a private repository.

On a badge, over a mounted checkout:

```bash
mpremote connect PORT mount . run tools/probe.py           # every page, timed
mpremote connect PORT mount . run tools/live.py            # against a real server
mpremote connect PORT mount . run tools/run.py             # the whole app
mpremote connect PORT mount . run tools/multihost_test.py  # pairing config
mpremote connect PORT mount . run tools/failover_test.py   # a changed host IP
uv run python tools/shots.py build/shots --publish         # PNGs, then the README's
uv run python tools/callgraph.py --open                    # the call graph, drawn
```

The config UI in [`src/statsbadge/web`](src/statsbadge/web) is three files the server hands
over as they are, linted separately by `npm run lint`. html-validate rejects an inline
`style`, and stylelint rejects an id selector, so colours and widths are set from `app.js` and
`app.css` reaches everything by element or by class.

[`tools/probe.py`](tools/probe.py) draws every page kind and theme against a canned frame and
needs no server, including a sparse frame, since "unknown" rendering as `0` is the easiest
thing here to break. [`tools/check_app.py`](tools/check_app.py) is the one CI runs: it walks
the AST for names that are neither defined, imported, nor badge builtins, these modules being
unimportable on the host.

[`tools/callgraph.py`](tools/callgraph.py) reads both sides into one graph and writes a
self-contained page that draws it. That finds the parts of this that no one file makes
obvious: the page renderers behind `pages._KINDS`, the extension renderers in `pages.EXTRA`,
the subcommands hung off argparse. Targets live in
[`tools/callgraph.toml`](tools/callgraph.toml).
