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

**A `.py` beside a `.mpy` wins the import.** One stray source file silently undoes a precompiled build, and nothing errors. [`ci/build-mpy.sh`](ci/build-mpy.sh) refuses it.

**`/system` is read-only to MicroPython.** Credentials go to `/state` over the REPL; the app needs the USB volume, which means resetting the badge. Writing `/state` and *then* resetting into mass storage loses the write - the reset discards it and the volume commits what was there before - so `install` copies the app first, waits for the badge to come back, writes credentials last, and reads them back.

**The debug probe shares Raspberry Pi's USB vendor id** with the board it is attached to, so port detection filters on product id and product string. Talking MicroPython to a CMSIS-DAP interface just times out.

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
- **A graph** is one `shape.custom` per series, a polyline across the top and back along the bottom. The two series take opposite ends of the ramp - the accent and the ramp at 0.85 are the same orange.
- **Bars** are raster rectangles. Axis-aligned needs no anti-aliasing, and this is the page that can have thirty-two.

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
uv run python tools/shots.py shots                         # dumps to PNGs
```

[`tools/probe.py`](tools/probe.py) draws every page kind and theme against a canned frame and needs no server. It also draws a deliberately sparse frame, because "unknown" rendering as `0` is the easiest thing here to break, and every screen that is not a page: the splash, the setup steps, the hosts menu, the error banners. Those call the app's own drawing, so a screenshot cannot go stale against a reworded screen. [`tools/check_app.py`](tools/check_app.py) is what CI runs: the badge compiles these from source, where a syntax error is a crash dialog and nothing else, and it walks the AST for names that are neither defined, imported, nor badge builtins - these modules cannot be imported on the host to find that out.
