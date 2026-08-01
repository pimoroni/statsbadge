# How it is put together

Two halves sharing one small contract: the host decides *what* to show, the badge decides *how*, and the only things crossing the wire are a flat JSON frame and a page list.

| File | What is in it |
| ---- | ------------- |
| [`badge_app/__init__.py`](src/statsbadge/badge_app/__init__.py) | the app: paging, buttons, the poll loop, exit |
| [`badge_app/net.py`](src/statsbadge/badge_app/net.py) | the HTTP client, request signing, discovery, pairing |
| [`badge_app/draw.py`](src/statsbadge/badge_app/draw.py) | every widget, the text cache, the band cache |
| [`badge_app/pages.py`](src/statsbadge/badge_app/pages.py) | a page descriptor to a drawn page |
| [`badge_app/look.py`](src/statsbadge/badge_app/look.py) | the five themes and the 320x240 layout |
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

## Rules you cannot break

**One `write()` per response, with `TCP_NODELAY`.** The same 515-byte reply costs a badge 247ms when the server flushes headers and body separately, and 7ms when it writes once: Nagle holds the second small segment until lwIP gets round to acknowledging the first, ~200ms later. A fixed cost per response that does not shrink with the payload. Hence [`server.py`](src/statsbadge/server.py) builds the whole response before writing, and a test asserts the body arrives in the first segment. Always send `Content-Length`, never chunked.

**A `.py` beside a `.mpy` wins the import.** One stray source file silently undoes a precompiled build, and nothing errors. [`ci/build-mpy.sh`](ci/build-mpy.sh) refuses it, and `install` prunes: copying alone would leave the sources behind when someone switches from `--source` to bytecode. Only `*.py`, `*.mpy` and `*.png` are the installer's to delete, so anything else in the app directory survives an update.

**The badge can hash its own app directory in 45ms.** `hashlib.sha256` over all nine files, against a 0.6s REPL round trip, so `install` compares that with hashes of what it would copy and skips the mass storage reset when they match. No manifest is written to the badge, so there is nothing to go stale.

**`/system` is read-only to MicroPython.** Credentials go to `/state` over the REPL; the app needs the USB volume, which means resetting the badge. Writing `/state` and *then* resetting into mass storage loses the write - the reset discards it and the volume commits what was there before - so `install` copies the app first, waits for the badge to come back, writes credentials last, and reads them back.

**Talking to the REPL stops whatever the badge was running.** mpremote interrupts to get a prompt, and holds the board in raw mode so `main.py` never runs again, which leaves the badge on a blank screen. So every command that touches it hard resets on the way out, in a `finally` and using whichever port the badge ended up on: the port changes when mass storage is ejected. The wait after that reset watches enumeration only - confirming the REPL answers would interrupt what has just started - and what the badge then shows is `main.py`'s business, not the installer's. It cannot be observed over the same cable anyway, since reading the framebuffer interrupts it again.

**A stale reading is not an authority on the time.** The clock's hands run off the badge's own RTC, which is set from the host's - but a frame is drawn forty-five times a second and carries the time it was polled at throughout, so reconsidering it every frame compares a clock that is running with a reading that is not. With the host away that disagreement reaches `RESYNC_S` in half a minute and the hands are dragged back to where the last poll left them, and again every thirty seconds after. Measured on the badge: back 30s at 31.0s, repeating. A request times out in 6s and failures back off, so five failed polls is all it takes - a WiFi reassociation, or the host restarting. So a sync is only considered against a *new* reading, keyed on the frame's `seq`. Setting the clock also lands the sub-second at zero, restarting the sweep part-way through a second, which is why an unnecessary sync shows as a stumble even when it corrects almost nothing.

**One clock, synced from the host and offset per page.** A page showing another place must not sync the RTC to that place: there is one hardware clock and two pages in two zones would each set it to their own as you turned to them. The hands are drawn from the badge's clock plus the difference between the host's reading and the page's, which are both in the frame. A place carries a time only once its forecast has landed, that being where its UTC offset comes from, so until then the page shows the host's clock rather than "no time".

**The light sensor's useful range is the bottom two percent of it.** `badge.light_level()` is a raw u16 off the Tufty's phototransistor, 16us a read; a curtained room measures 96 to 176 and steps in sixteens, which is one count of the 12-bit conversion behind it. So `look.ambient_fraction` is logarithmic from 96, and the top - 4000, where the panel gets everything the config asked for - is raised whenever the sensor reads past it, which means a badge in daylight calibrates its own ceiling instead of pegging at a guess. Ambient only ever takes brightness away, from a floor of 0.45, because `brightness` is the level someone chose and a dark screen is what the setting being *off* should look like. The follower takes a fifth of the gap every 250ms: read directly, a hand's shadow flickers the panel.

**A button can do something without the host.** A binding starting `badge.` is answered on the badge - paging, and cycling the panel brightness - and never reaches the wire: a round trip to change the page would be slower than the press and would not work with the host away. The host carries the list only so the config UI can offer them beside the commands, and `send_command` never sees them. The brightness button is a local override of the configured level rather than a config edit, because someone reaching for it wants *this badge* dimmer now.

**The ramp runs calm to alarming, so a battery is read backwards.** Nearly every field is a load or a temperature where high is bad, and `power.battery_pct` is not: at 100% it was drawn in the ramp's red. `pages.severity_of` inverts the fraction for those fields and it colours only - a gauge's sweep and a bar's length are still the reading itself.

**A theme is config, so the badge carries one.** The palette for the chosen theme travels in the layout - 213 bytes, on a payload only refetched when `rev` moves - and `look.from_palette` builds a `Theme` from it, checking anything that would reach `color.rgb` because a bad palette off the network would otherwise be a crash on every frame instead of a page in the theme it booted with. So [`themes.py`](src/statsbadge/themes.py) is the only place a palette is written down: the app keeps `dark` for its first frame, `layout.THEMES` is the names from the same data, and the UI's swatches come from the host over `/api/capabilities` where they used to be a table in `app.js` with a comment asking for it to be kept in step.

**The debug probe shares Raspberry Pi's USB vendor id** with the board it is attached to, so port detection filters on product id and product string. Talking MicroPython to a CMSIS-DAP interface just times out.

**Text drawn large wants a wide font.** A narrow `.af` packs the whole em into a signed byte, so a coordinate is `size / 128` pixels: fine for body text, and at the 104pt a clock draws its digits every vertex on a curve is snapped most of a pixel and the bowls come out lumpy. None of the other suspects account for it - the antialiasing resolves an edge in a single pixel over ~150 coverage levels, and a font built at a fifteenth of the usual simplification tolerance renders identically, the tolerance already being half the grid step. `tools/make_text_font.py --wide` packs points as 16-bit and records the em in the header, keeping the cap at the same fraction of it (648 of 1024), so a wide font is a drop-in for the narrow one it replaces at eight times the precision. Measured on the badge: same widths to a pixel, and it loads in 75ms against the narrow font's 107ms. It costs twice the point storage, 78KB against 39KB. The icon fonts stay narrow, being drawn at 32pt where the grid is a quarter of a pixel.

**A capital stands 81 units of a 128 unit em**, so `draw.CAP` is 0.633 of the size asked for, and an icon's box - 100 of the same - is 0.781. Both hold for a wide font, whose em is the same em at a finer grid, because the decoder scales whichever em to the size. `draw.icon_baseline` centres a symbol's ink on the cap band from those two numbers, which is what stops it floating: an icon's box is a quarter taller than a capital and its ink sits in the middle of the box, so sharing a baseline puts the symbol 4.5px high at 32pt beside 26pt. Measured off the framebuffer, all three symbols tried centre 13.0px above the baseline at 32pt against 9.0px for a capital at 26pt.

**A .af advance over 127 is read as negative.** It is a signed byte, so an icon font that
fills the -128..127 coordinate range draws every glyph of a run on the same spot, and
`measure_text` returns a width of zero. Nothing in the badge's own font exceeds 120, and a
capital stands 81 units, so `tools/make_icon_font.py` fits icons to a box of 100 and they
line up with text. `../BADGEWARE.md` has the rest of the format, all of it measured off
`MonaSans-Medium.af` rather than taken from alright-fonts' loader, which is a version
behind what afinate writes.

**Scroll by blitting two windows, not by copying the image onto itself.** A waterfall keeps
a ring buffer the width of the plot, writes one column per frame with `vspan` and shows it
as `window(cursor..end)` then `window(0..cursor)`. Measured: two windowed blits 7ms, a
self-blit of the same image 11.3ms, a column as `vspan` per lane against 30 rectangles
4.4ms. The whole page runs at 28fps.

**The firmware's image effects are too slow to animate.** On a 320x120 band: `dither` 13ms,
`onebit` 12ms, `bloom` 21ms, `synthwave` 40ms, `edgeglow` 99ms. Fine for a page that
redraws once a second, not for one that moves.

**A throughput has no full scale, so it is scaled by what it has reached.** 12.5MB/s was
assumed, which reads as pegged on a gigabit link and as idle on a slow one, and no platform
reliably reports a link speed - `psutil.net_if_stats()` gives 0 for every interface on
macOS. The collector keeps a high-water mark per rate instead, decaying by half every ten
minutes so it follows the machine rather than remembering one busy night, floored at 64KB/s
so a trickle on a quiet link is not a full ring. It travels as `peaks` in the frame, which
is scale and not a reading, so it is not a model group and never offered as a field. A
gauge drawn from a rate states its peak, because otherwise nothing says what full means.

**A byte figure carries its prefix on the number and its base in the unit.** `fmt` scales to
the largest prefix the value fills - 512, 800K, 11.4M - and `short_unit` returns `B/s` or `B`
after it, so one unit serves a reading whatever size it grows to and a slot with a unit of
its own gets the pair for free. A gauge too small for the unit shows the reading alone, since
the prefix is the part that says which 11 it is.

**Each field slot draws from a pool, not from everything.** A gauge needs a top end, so it
is offered percentages and the fields in `model.FULL_SCALE` and nothing else - uptime is a
number and a ring drawn from it is empty whatever the machine is doing. A graph is offered
what the collector keeps a ring for, or it plots the live value twice and draws a flat line.
A list field goes only to the kinds that draw one lane or bar per element, since `fmt` has
nothing to do with a list but print it. `capabilities` carries `full_scale`, `list_fields`
and `graphed` so the UI can tell these apart, and every kind in `SHAPE` names its pool.

**A page can only carry what its kind declared.** `page_settings` on a source is the same
shape as `settings`, keyed into the UI and the validator by page kind; `validate` keeps those
keys and drops the rest, so a page cannot smuggle anything to the badge. A source that needs
to do different work per page implements `pages(instances)` and is handed its own pages at
startup and on every save. The badge finds the settings in the page dict it is already given,
so that side needed nothing.

**On macOS "/" is not the disk anyone means.** It is a sealed, read-only system volume
sharing an APFS container with the data volume, so it reports the system's own 12G against
the container's size: 9% on a disk that is 86% full. Both volumes report the container's
free space, so the data volume is the one whose `used` is the answer, and
`sources/portable.py` defaults there.

**`screen.raw` is R G B A, premultiplied, no byte swap.** Get it wrong and red and blue swap, which reads as a drawing bug rather than a converter one.

## Transport and signing

Plain HTTP. TLS is affordable here - ~2ms a request once connected, 165ms for the handshake, worst non-blocking step 1.5ms against 0.6ms - but both ends use `CERT_NONE`, so it buys confidentiality on the LAN and no server authentication. Signing is what stops a command being forged, and that works over either transport.

Every `/v1/*` request carries `X-Badge-Id`, `X-Badge-Seq` and an HMAC-SHA256 over method, path, counter and a digest of the body. The path includes the query string, because the query changes the response. MicroPython has `hashlib` but no `hmac`, so [`net.py`](src/statsbadge/badge_app/net.py) has the eight lines of it.

The counter only goes up and the host refuses anything it has already seen. The badge cannot write flash per request, so it persists in batches of 64 and jumps forward on boot, which means the two ends can be far apart. Guessing fails either way - too low is a replay, too high is out of window - so a refusal carries `next_seq` and the badge resyncs in one request. That is safe because the signature is verified *before* the counter is checked. The host persists its counter too, or a restart would rewind it and make older captured requests replayable.

Pairing is off until asked for. A badge then asks at `/v1/enrol` and is given a short code to show; approving that request at the host is what pairs it, and until then nothing is stored. The code is minted per request by the host, never derived from `badge.uid` - that travels as `X-Badge-Id` over plain HTTP, so a badge that has talked to any server on the network has leaked it, and an attacker could show a matching code and be approved by mistake.

Requests are rate limited on a doubling backoff and capped at `MAX_PENDING`, so a flood cannot bury the real one. A badge re-asking gets its existing request back rather than a throttle, because it retries after a dropped reply. The secret is handed over exactly once, to whoever holds the request id.

The app ships its own HTTP client because the firmware's `fetch.py` did not work on this build. [pimoroni/fetch](https://github.com/pimoroni/fetch) is the better choice for anything talking to arbitrary hosts - it spreads the TLS handshake across calls, handles chunked bodies and verifies keep-alive - but this app talks only to its own server, over plain HTTP, always with `Content-Length`.

## Which host, and where it went

Credentials are keyed on a server id from [`identity.py`](src/statsbadge/identity.py), not an address, so a host that changes address is still the same host. The badge stores several, each with its own secret and counter, and after three failed polls it listens for beacons and follows one it knows - at most every 20s, because listening costs a frame. A beacon's *source address* is trusted over its payload. The older flat `{host, port, secret}` state file still loads, under a placeholder id until a beacon or a reinstall reveals the real one.

## Drawing

Costs on this board, with more in `../BADGEWARE.md`: an anti-aliased shape is ~0.25ms whatever its size, a line of live text ~1ms, the same text blitted from a sprite 0.08ms, a full-screen 1:1 blit 14ms.

So anything repeated is baked and blitted, and only what changes shape is drawn live:

- **Labels** become sprites. Values churn, so the cache is dropped wholesale past 220 entries rather than aged.
- **Header and footer** are baked per page as two band images and blitted over a raster fill of the body. A whole-screen bake made a page turn cost 90ms; bands are cheap enough to keep a dozen. A `window()` view also takes the unscaled blit path, where equal-size source and destination rects go through the sampler.
- **The dial** is two `shape.arc` calls. Angles start at the top and run clockwise, so a 270-degree gauge with its gap at the bottom is 225 to 495. The fill is a solid ramp colour, not a gradient: a linear gradient across the arc's box does not follow the curve, so the hue would not track the reading.
- **A graph** is one `shape.custom` per series, a polyline across the top and back along the bottom. The two series take opposite ends of the ramp - the accent and the ramp at 0.85 are the same orange - except on a theme built out of one hue, where the page *is* one end of its own ramp and an area drawn in that is not there at all: those fall through to the dim colour, and on a pale page the second series is drawn as solid as the first, a translucent area over one washing out towards it. `draw.curve` resamples the samples as a Catmull-Rom spline first, which passes *through* each one, so the peak drawn is the peak measured; overshoot is held to the range of the data, or an area fill would run under its own baseline. Only the values are interpolated, the samples being evenly spaced, and the step count follows the plot's width because a segment shorter than a pixel costs the same as one that shows. The weights are worked out once per step count: evaluating the polynomial per point cost 265us a point, 50ms for one series, against 2.7ms from the table. It is a setting, `smooth`, since it is a drawing choice rather than a property of a page - a graph costs 31ms a redraw against 18, and a poll is a second apart.
- **Bars** are raster rectangles. Axis-aligned needs no anti-aliasing, and this is the page that can have thirty-two.
- **A text column is measured, not fixed.** A page of names down one side and readings down the other cannot know either width in advance: the names are whatever the chosen fields are called and a reading is whatever its unit makes it. `draw.column_width` measures both and the plot takes the rest, so a sparkline page reflows instead of leaving a gap after the names and running the readings over the plots.
- **Every split page draws in the same place.** `look.DIAL_GAP` is the space at the screen edge, between the round half and the column, and at the right edge, and `DIAL_C`, `DIAL_OUTER`, `READOUT_X` and `READOUT_W` are worked out from it. The single dial, the ring stack and the clock face all use those, and all their rows come from `look.readout_rows`, which hangs the stack off the top of the gauge and lifts it only when there are too many rows to fit the band. The ring bands are as thin as it takes for four of them to fit the gauge's own radius, because the alternative was a stack wider than the dial with a legend jammed against it.

A page changes when a poll lands, once a second, so frames in between draw nothing at all: `badge.default_clear = None` leaves the framebuffer standing. A page that animates adds its kind to `pages.ANIMATED`, which is how the clock's second hand sweeps.

| Page | Draw |
| ---- | ---- |
| CPU dial | 17.7ms |
| Cores, twelve bars | 16.9ms |
| Network graph | 20.0ms |
| Disk grid | 22.8ms |
| Host, text | 11.2ms |
| Swiss clock | 14.7ms |
| page turn, cold caches | 46.3ms |
| nothing changed | 0ms |

`display.update()` blocks on vsync, so everything above is two 90Hz periods: a steady 45fps. A signed `/v1/stats` round trip is 14ms, worst single step 2.85ms - the step carrying the HMAC.

Where a screen takes A/B/C for its own input rather than passing them to the host, they are used in the order they sit in: **A back, B select, C next**.

HOME opens the hosts menu and holding it leaves, so the launcher's exit irq is taken off it and it is polled instead - the idiom `../BADGEWARE.md` describes. A press has to do something useful and a hold has to remain a way out, because nothing else can reach the menu: UP/DOWN page and A/B/C belong to the host. The menu rescans on open with a window longer than the 2s beacon interval, or a server that has just broadcast is missed and the list silently comes back short.

`--ssid` edits `secrets.py` on the mounted volume, read-modify-write, keeping the other settings and the comment listing the regions. It does not write `/secrets.py` on the internal filesystem: the frozen `secrets` module prefers that one, so it would take precedence over the file the badge's own error message tells people to edit, and a later edit in disk mode would appear to do nothing.

## Launch, and .mpy

The badge compiles from source at every launch: 763ms for the five modules, timed off its own flash, since a mounted checkout streams over serial. Two things come off that without precompiling: `setup.py` is imported on demand, and the mark goes up after `look` alone, from shapes only. `font.load` is a further 107ms, paid once.

Precompiled, the five import in **66ms**, and that is the default: CI runs [`ci/build-mpy.sh`](ci/build-mpy.sh) into `badge_app/mpy/` before `uv build`, so a wheel carries both the sources and bytecode, and `statsbadge install` picks the bytecode with no flags. `--mpy DIR` overrides with a build of your own; `--source` forces the sources.

The build uses an `mpy-cross` from whatever MicroPython the board repo's `ci/micropython.sh` pins, so the version cannot drift from the firmware, and compiles with `-s` so a traceback names the module rather than the build path.

Bytecode only loads on the firmware it was built for, which is why the sources ship too. `(flags << 8) | version` from the header equals `sys.implementation._mpy`, so the install compares the two and **falls back to the sources** on a mismatch rather than refusing - a wheel outlives a firmware release. The firmware would refuse it anyway, with `ValueError: incompatible .mpy file`. `-march` is irrelevant to bytecode-only modules: `armv6m`, `x64` and the default emit an identical header.

Staleness is checked by content, not mtime: `ci/build-mpy.sh` records a hash per source in `BUILD_INFO` and the install warns if any has changed since. Mtimes would be meaningless here, because everything in an extracted wheel carries the same install-time stamp.

## The frame, and "unknown"

One shape across every platform, with anything the host cannot answer set to `None` - never zero, never absent - so the badge can tell an idle GPU from an unmeasurable one and draw `--`. That distinction is load-bearing twice: the collector derives what a host *can* report from the live frame rather than from what a source claims, and pages whose fields do not exist are pruned before the layout is sent. Sampling runs on a thread on an interval, so requests serve the last frame and ten badges cost the same as one.

macOS is the stingiest host. GPU utilisation and VRAM come from IOAccelerator via `ioreg` with no privileges; temperatures, fan RPM and package power need root, so `--powermetrics` is opt-in and the default install asks for no password.

## Extensions

A package advertising a `statsbadge.sources` entry point gets two things: a group in the frame, which the built-in page kinds can draw with no badge-side code at all, and optionally badge-side Python. Set `badge_module` to a `.py` and `statsbadge install --with-extensions` copies it into the app's `ext/` directory, where the app imports it at startup and it registers itself in `pages.EXTRA`.

[`extensions/statsbadge-clock`](extensions/statsbadge-clock) is a worked example of both halves, and of why the second exists: its second hand is carried forward from the badge's frame clock between polls, so it sweeps at 45fps off one reading a second.

A badge module draws with the same `draw` and `look` the app uses, and should take its layout from them rather than choosing its own. Pages are paged between, so anything that picks its own centre or margin moves under the reader when they press a button. For a page that splits into something round and a column of text - the single dial, the ring stack, the clock face - that means `look.DIAL_C` and `look.DIAL_OUTER` for the round half, `look.READOUT_X` and `look.READOUT_W` for the column, and rows from `look.readout_rows` drawn with `draw.readout` or, where they are not readings, `draw.column_lines`. `draw.column_width` fits a column to the strings going in it, and `draw.icon_baseline` puts a symbol on the cap band of the words beside it.

## Packaging

`src` layout, `uv_build`, one project at the repo root. The badge app lives *inside* the package at `src/statsbadge/badge_app/`, because an installed wheel has to carry it or `statsbadge install` has nothing to push; uv_build ships every file under the module directory, icon included, so one path serves a checkout and an installed wheel. The distribution, the module and the command are all `statsbadge`: name them differently and uv_build needs `module-name`, which older uv treats as a fatal parse error rather than a warning.

CI installs the built wheel into a throwaway environment and asserts the app and the web UI are in it, because "the wheel builds" and "the wheel works" are different claims. Publishing is trusted publishing over OIDC, and refuses if the tag and the version disagree.

## Working on it

```bash
uv sync                                             # dev environment
uv pip install --no-deps ./extensions/statsbadge-clock
uv run statsbadge probe                             # what this host can measure
uv run python tests/test_core.py                    # server, auth, framing
uv run python tools/check_app.py                    # the app parses and is whole
uv run ruff check --config ci/ruff.toml src tools tests extensions
uv build                                            # sdist + wheel
ci/build-mpy.sh                                     # precompile the badge app

mpremote connect PORT mount . run tools/probe.py           # every page, timed
mpremote connect PORT mount . run tools/live.py            # against a real server
mpremote connect PORT mount . run tools/run.py             # the whole app
mpremote connect PORT mount . run tools/multihost_test.py  # pairing config
mpremote connect PORT mount . run tools/failover_test.py   # a changed host IP
uv run python tools/shots.py build/shots --publish          # PNGs, then the README's
```

[`tools/probe.py`](tools/probe.py) draws every page kind and theme against a canned frame and needs no server. It also draws a deliberately sparse frame, because "unknown" rendering as `0` is the easiest thing here to break, and every screen that is not a page: the splash, the setup steps, the hosts menu, the error banners. Those call the app's own drawing, so a screenshot cannot go stale against a reworded screen. [`tools/check_app.py`](tools/check_app.py) is what CI runs: the badge compiles these from source, where a syntax error is a crash dialog and nothing else, and it walks the AST for names that are neither defined, imported, nor badge builtins - these modules cannot be imported on the host to find that out.
