# statsbadge

Your PC's vitals on a Badgeware badge. A compact, wireless and extensible hardware monitor for Windows, macOS and Linux.

![CPU](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/cpu.png) ![Load](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/gauges.png)

Runs on [Badgeware's Tufty](https://shop.pimoroni.com/products/tufty-2350), firmware v3.0.0 and above, with a 2.8" 320x240 colour IPS display.

Build your stats overview from a selection of pages - bar graphs and waterfalls to big, bold gauges - configured in a web UI on your computer and pushed to your badge at the press of a button. A server on the host measures things and serves them; the badge fetches and draws them, so rearranging the display needs no reinstall.

## Install

Uses [uv](https://docs.astral.sh/uv/). As a tool, so it lands on your PATH and keeps a separate environment:

```bash
uv tool install statsbadge
statsbadge tray
```

That puts an icon in your menu bar or notification area and serves from there, so there is
no terminal to leave open. The menu opens the config UI, approves a badge asking to pair,
and switches on **Start at login**. `statsbadge serve` is the same server without the icon.

On Linux the tray needs GTK bindings from your distribution, which pip cannot supply, and
GNOME hosts no tray at all without the [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).
Run `statsbadge tray --check` for what is missing on this machine. Without a tray it serves
anyway.

### An app, if you would rather not have a terminal at all

Every release carries a `.dmg` for macOS and an `.msi` for Windows. They hold the whole
thing - Python, the server, the tray, and the clock, ISS and quakes extensions - and need
nothing installed first. Linux keeps the uv path above.

Neither is signed. macOS blocks a double-click on the first run: right-click the app,
choose **Open**, and the warning does not come back. On Windows, SmartScreen hides the button behind **More info**, then
**Run anyway**.

One thing the app cannot do: install more extensions. That needs uv or pip on the machine,
and a packaged app has neither, so the **Extensions** tab lists what is there without offering to change it.
The three it ships with cover the pages most people want.

Working on a checkout instead:

```bash
uv sync
uv pip install --no-deps ./extensions/statsbadge-clock   # optional
uv run statsbadge tray
```

Into whatever environment you already have, with pip or uv:

```bash
uv pip install statsbadge      # or from a checkout: uv pip install .
pip install statsbadge         # plain pip works too
```

One extra, if you want it: `statsbadge[nvidia]` for NVIDIA cards via NVML. Pushing the app to a badge over USB needs nothing added.

With the badge on USB, in another terminal:

```bash
statsbadge install                       # copies the app, extensions and pairs it
statsbadge install --ssid "My Network"   # and sets up WiFi, from a brand new badge
```

That writes the pairing secret over the serial REPL and, if the app needs copying, offers to reset the badge into USB mass storage mode to do it. Answer no and you get credentials only.

Run it again whenever you have upgraded the package or installed an extension. It compares what is on the badge with what it would put there, copying only what differs and dropping what does not belong. When the badge already matches, it is left alone: about a second, no reset. `statsbadge update` is the same command under the name you probably reached for. Credentials already written are left as they are, so a repeat run is purely a code update.

`--ssid` sets the WiFi details in the badge's `secrets.py` while that volume is mounted, so a new badge goes from unboxed to showing stats in one command. It prompts for the password, so the password stays out of your shell history; `--pass` takes it directly and an empty string means an open network. `--region` and `--timezone` set those too. The region is the radio's country, and the firmware takes a fixed set of them that `statsbadge install --help` lists. One outside that set is refused here, since it leaves the badge unable to join a network at all - which reaches the screen as "could not reach the host". Details the badge already has are left alone unless you pass `--force-secrets`.

The config UI does the same job without a terminal. **Update badge** in the header pushes the app to whichever badge is plugged in and pairs it with this host, showing what it copies as it goes. WiFi details are only touched if you tick **Set the WiFi network**, so updating a badge does not cost it the network it is on. A badge last seen running an older app is marked in a line above the tabs; that is a guess from what this host last put there, and connecting the badge is what settles it.

No cable? Run `statsbadge pair`, or open the config UI and press **Pair a badge**. Launch **Stats** on the badge and press **B** to set up; it finds the host by itself and shows a six-character code. Check that code matches the one the host shows, and approve it there. Nothing is typed on the badge.

A server is not in pairing mode until you put it there, and the window closes after five minutes or when you press **Stop pairing**. Requests are rate limited and capped. One only pairs a badge once you approve it.

Then open <http://127.0.0.1:8420/> to pick screens, themes and button bindings. A button can run a host command, or do something on the badge itself: page back and forth, or cycle the brightness. The badge can also set that brightness from its light sensor to suit a dim room, and page through the screens on a timer once nobody has touched it for a while.

## Everything else you might want

```bash
statsbadge tray                        # the usual thing, with an icon to reach it by
statsbadge tray --check                # whether a tray works here, and what it needs
statsbadge serve                       # the same server, in a terminal
statsbadge autostart                   # whether it starts at login, and what runs
statsbadge autostart enable            # start the tray at login
statsbadge autostart disable
statsbadge status                      # what is on the badge, what this host knows
statsbadge ext                         # installed extensions, and whether they loaded
statsbadge ext add clock               # install one and remember it
statsbadge probe                       # what this host can measure at all
statsbadge badges                      # which badges are paired here
statsbadge badges --forget <badge-id>

statsbadge install --force-app         # copy the app whether or not it changed
statsbadge install --no-extensions     # leave extension modules off the badge
statsbadge install --without clock     # everything except that one extension
statsbadge install --new-secret        # re-key this badge
statsbadge install --ssid "Other" --force-secrets    # change the WiFi it uses

statsbadge --config-dir ./cfg serve    # global options come before the subcommand
```

Each badge is configured separately. The picker in the header of the config UI names the badge a page belongs to, and pages, theme, buttons and the rest belong to that badge. A badge that has just been paired draws the default until it is saved for the first time, the entry "Default, for any other badge" in the picker. Saving for one badge leaves the others where they are - a badge only refetches when its layout's revision moves. Forgetting a badge takes its layout with it. What an extension is *told* - a place, an API key - stays one answer per host, since that is what it is.

Configuration lives in `~/.config/statsbadge` on Linux, `~/Library/Application Support/statsbadge` on macOS and `%LOCALAPPDATA%\statsbadge` on Windows, or `$XDG_CONFIG_HOME/statsbadge` wherever that is set. `statsbadge status` prints the path it is using. Three files: `layout.json`, `server.json` and `badges.json`, the last holding pairing secrets and kept at mode 600. The tray writes its output to `logs/tray.log` beside them, since it has no terminal to print to; **Open the log** in the menu goes there.

`statsbadge autostart enable` writes a registry value under `HKCU\...\CurrentVersion\Run` on Windows, a LaunchAgent in `~/Library/LaunchAgents` on macOS, or a `.desktop` file in `~/.config/autostart` elsewhere. It records whatever `--config-dir` and `--port` you asked for. `statsbadge autostart` prints exactly what would run, and the tray's **Start at login** is the same switch.

## Changing IPs, and more than one computer

Credentials are keyed on a server id the host mints once, not on its address. So:

- **The host's IP changes.** The badge notices the polls failing, hears the host's beacon, recognises the id it is already paired with and follows it to the new address. Nothing to re-pair.
- **Two computers.** Pair with both - `statsbadge install` and `statsbadge pair` add hosts; neither replaces the one already there. Each gets its own secret and counter. The badge uses whichever it can reach, and switches by itself when the current one goes quiet.
- **Same computer, fresh install.** `install` reuses the existing secret unless you pass `--new-secret`, and folds an older single-host config in, leaving nothing orphaned.

`statsbadge badges` lists what a host has paired; `--forget` drops one.

## On the badge

| Button | What it does |
| ------ | ------------ |
| UP/DOWN | previous/next page |
| A B C | whatever the host has bound them to, if anything |
| HOME | open the hosts menu |
| HOME, held | leave the app |

The hosts menu is how you switch between machines - laptop, desktop, that Linux box - and how you add another one. It rescans every time it opens, so a server you start after the app is already running turns up without a restart.

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

A field the host cannot measure is `null`, and pages that need it are dropped, not shown empty. On macOS that means temperatures, fan speed and package power. Those need root, so they are opt-in with `--powermetrics`, which allows exactly one command:

```bash
sudo visudo -f /etc/sudoers.d/statsbadge
# then, with your own username:
you ALL=(root) NOPASSWD: /usr/bin/powermetrics --samplers cpu_power,gpu_power,thermal -i 1000 -f plist
```

Run with `--powermetrics` and no rule in place and it prints that line with your username already in it, then carries on without those fields. Windows needs [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running with its web server on for temperatures and fans. NVIDIA GPUs need `pip install statsbadge[nvidia]`.

## Page kinds and themes

Twelve kinds, and any field can go in any of them. Six show readings as they are. Those are `dial`, `dials`, `bars`, `graph`, `grid` and `text`. The other five go further than a single number:

| Kind | What it is for |
| ---- | -------------- |
| `rings` | up to four readings as concentric gauges, each coloured by its value |
| `spark` | six readings at once, name, current value and recent history a row each |
| `radar` | three to six readings as a polygon: the shape of the load rather than its size |
| `trend` | one big reading, which way it is going, and where it has been |
| `waterfall` | a list field as lanes over time, interpolated between polls |
| `badge` | the badge's own vitals, which need no field and no host |

`dial` has a page to itself, so it is the one gauge big enough to read a ramp off. Set **Dial gauge** to *The Whole Ramp* and it fills with a conical gradient carrying the theme's ramp end to end. Past the reading the same stops are drawn at alpha 32, so the scale still shows behind it. Costs 0.6ms a frame.

![The whole ramp](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/dial_ramp.png)

`badge` is the odd one out. Battery, memory, both filesystems and the ambient light show as levels. The clock, voltage, power source, uptime and screen show as figures. The board, firmware and uid are underneath. Nothing on it comes from the host, so it is the page to turn to when you are wondering whether the badge or the network is the problem.

![Badge](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/badge.png)

`waterfall` is the one that moves. Point it at `cpu.cores` and every core gets a lane, coloured by the theme's ramp and scrolling right to left at about 28fps. It interpolates between the once-a-second polls instead of stepping, so it reads as motion and not as data arriving. Precision is what that trades away; the numbers are on the other pages.

Sixteen themes, and forty-eight accents. The written-down ones are grouped light and dark in the picker: Default Dark and Default Light, `frost`, `vapor`, `sakura` and the three Eva units. Four more come as a pair for a lit room and a dark one: Mono, Watermelon, Shell and Luminescence.

Everything is drawn as vector shapes taking their colours from one table, so a theme is a palette and not a set of images. The palette travels to the badge with the layout, which makes it config: [`themes.toml`](https://github.com/pimoroni/statsbadge/blob/main/src/statsbadge/themes.toml) is the only place one is written down, and adding one needs no install.

Then four you can tune yourself, derived rather than written. Pick one of forty-eight accents: twelve hues in four families, Pastel, Normal, Saturated and Dark. Tinted Dark and Tinted Light hold every hue at one chroma and send the ramp to red unless the accent is already there. Tinted Bold Dark and Tinted Bold Light take each hue as far as sRGB allows and keep the ramp in it, sweeping lightness. The single-hue names red, green, cyan, amber and blueprint are that second pair with an accent, so they still resolve to what they always looked like.

A palette can also carry a second accent, used sparingly. A graph's second series takes it, and that is the one place the badge otherwise has to hunt through the ramp for a colour that will show. A derived theme picks it by rule: Same, Complementary, Triadic or Contrasting, the last being whichever offered hue lands furthest away once lightness and chroma are counted. Watermelon Light names its own, that page having nowhere else to put its green. The config page previews whichever theme is selected, derived or not.

![Cores](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/cores.png) ![Network](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/net.png) ![Disk](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/disk.png) ![Processor](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/gauges2.png) ![Host](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/host.png) ![Vapor](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_vapor.png) ![Sakura](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_sakura.png) ![Watermelon](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_watermelon.png) ![Shell](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_shell.png) ![Unit-01](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_eva01.png) ![Luminescence](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_luminescence.png)

![Waterfall](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/waterfall.png) ![Rings](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/rings.png) ![Sparklines](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/spark.png) ![Radar](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/radar.png) ![Trend](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/trend.png)

## Extensions

An extension is a pip install away. It adds data to the frame, and optionally badge-side code for a page the built-in kinds cannot draw.

The **Extensions** tab of the config UI lists the published ones with an Install button each, and a box that takes any other pip requirement. Installing one takes effect where it stands, with no restart: the server picks it up and its pages appear in the kind picker. One that draws its own page needs `statsbadge install` over USB as well, since `/v1` carries readings and a layout and never code. Which ones those are is on the list, before you install one.

The same thing from a terminal:

```bash
statsbadge ext add clock
statsbadge install
```

Badge-side modules go on by default, so adding an extension and then running `install` is all of it. `--no-extensions` leaves them off, and `--without NAME` drops one from both the frame and the badge.

Extensions install into `lib/` beside your config, not into the environment statsbadge itself runs from, and that directory goes on `sys.path` at startup. So this works the same from a `uv tool install`, a virtualenv, a pipx install or a checkout, and upgrading statsbadge no longer drops them.

`extensions.txt` beside your config is the record, and every change rebuilds from all of it, so removing one is a rebuild without that line. Each build lands in a new numbered directory and is renamed into place. Older ones are swept at the next start, once nothing is importing from them.

```bash
statsbadge ext                     # what is installed, and what the list asks for
statsbadge ext add iss quakes      # add, then rebuild
statsbadge ext remove clock        # take one out, and out of the library
statsbadge ext disable octopus     # leave it installed, and stop loading it
statsbadge ext enable octopus
statsbadge ext outdated            # ask the index which of them have moved on
statsbadge ext upgrade             # take newer releases of everything unpinned
statsbadge ext upgrade clock       # move that one, pin and all
statsbadge ext sync                # build the library again from the list
```

An extension the environment installed, an editable checkout for instance, cannot be uninstalled
from here: a build only writes the library. Those offer **Disable** in place of Remove,
which leaves them installed and stops loading them, recorded in `disabled.txt` beside your
config.

`ext outdated` and the **Update** button in the config UI both ask an index, so they want
the network. A bare `ext upgrade` leaves anything you pinned in `extensions.txt` where it
is; naming one is asking for it to move, which removes its pin and reports it. An extension
already running keeps running the code it imported, so a newer release of one wants a
restart, which the tab notes too.

The tab offers whatever [`catalogue.toml`](https://github.com/pimoroni/statsbadge/blob/main/src/statsbadge/catalogue.toml) names, which is every extension this project publishes. That list is a convenience. Any pip requirement works, whether from PyPI, a URL or a path.

Installing needs either `uv` or `pip` on the machine. Without one the tab still lists what is installed, but offers no way to change it.

Three extensions are vendored here: [statsbadge-clock](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-clock) for a clock and the weather, [statsbadge-iss](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-iss) for the space station, and [statsbadge-quakes](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-quakes) for recent earthquakes. The last two draw on the badge firmware's world map, so running both costs one copy of the coastlines and no geometry crosses the network.

An extension can declare settings that belong to *one page* and not to the extension, so two pages of the same kind can show different things. The clock uses it for a place and a face. Point one page at Tokyo, another at home, and each shows that city's weather and local time. Open-Meteo returns a location's UTC offset with its forecast, so a place settles the time too and there is no timezone to set. `latitude` and `longitude` are there per page as well, for a spot no name lands on. Settings that describe how the extension works, like units or an API key, stay under Extensions where there is one answer per machine, and the place set there is the default for any page that names none.

Settings are what an extension is told. What it works out goes in `self.store`, a small dict the host keeps between runs: `store.get(key)` and `store.set(key, value)`. It is namespaced by the extension's entry point name and written under the config directory, so an extension never picks a filename or manages a directory. It is in place by the time `start` runs.

The clock keeps the coordinates a place name resolved to, since a town does not move. The geocoder is asked once per name ever, not once per launch, so a badge coming up while the geocoder is rate limiting still draws the right place.

An extension can ship more than code. `badge_assets` lists further files to push, and the clock uses it for an icon font: its `icons.txt` names the Material Symbols to pack, and `tools/make_icon_font.py` packs them into an `.af` the badge loads with `font.load()`.

```bash
uv sync --group fonts
python3 tools/make_icon_font.py extensions/statsbadge-clock
```

That fetches Material Symbols, fits each glyph to the text font's metrics so icons sit on the same baseline as the words beside them, and writes `src/statsbadge_clock/badge/icons.af`. Any vendored extension with an `icons.txt` builds the same way.

[statsbadge-clock](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-clock) is a clock and the weather: five faces, including a Swiss railway station clock whose second hand sweeps at the badge's frame rate. Weather from Open-Meteo, no key needed.

![Railway](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/swiss_clock.png) ![Dots](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_dots.png) ![Squircle](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_squircle.png) ![Digital](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_digital.png) ![Digital LCD](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_lcd.png)

[statsbadge-quakes](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-quakes) puts recent earthquakes on a world map, cycling through them unprompted: the camera closes in on each, then pulls back out to cross an ocean, and the rings leaving an epicentre are coloured by magnitude. From USGS, no key needed.

![Quakes](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/quakes.png) ![Quakes pulled out](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/quakes_wide.png)

[statsbadge-iss](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-iss) tracks the space station across the same map, with an orbit of ground track either side of it and the day and night terminator washed over the half the sun is not on. The sub-solar point arrives with the position, so the terminator costs two numbers and no almanac. Both feeds are open, so there is no key and no account to set up.

![ISS](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/iss.png) ![ISS following](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/iss_follow.png)

See [DEVELOPMENT.md](https://github.com/pimoroni/statsbadge/blob/main/DEVELOPMENT.md) for how to write one.

## Security

Plain HTTP on the LAN, with every request signed HMAC-SHA256 against a shared secret from pairing, and a counter the host rejects on a repeat. So a command cannot be forged or replayed, and an unpaired device on the network learns nothing. TLS is affordable on this hardware but buys little without certificate validation - [DEVELOPMENT.md](https://github.com/pimoroni/statsbadge/blob/main/DEVELOPMENT.md) has the measurements. The config API is bound to loopback because it can mint secrets. Host commands only run if you have bound them to a button.

## Fonts

The badge draws with two typefaces, both under the SIL Open Font License, packed into `.af` by `tools/make_text_font.py`:

- [Lexend](https://github.com/googlefonts/lexend) for everything, and again as thirteen digits packed wide for the clock face that draws numbers the height of the band. [Licence](https://github.com/pimoroni/statsbadge/blob/main/licences/OFL-Lexend.txt).
- [DSEG](https://github.com/keshikan/DSEG) by keshikan, DSEG7 Classic Bold, for the LCD clock face's seven segments. [Licence](https://github.com/pimoroni/statsbadge/blob/main/licences/OFL-DSEG.txt).

## Names

The repository, the package, the module and the command are all `statsbadge`. Keeping them identical is deliberate: name the distribution and the module differently and uv_build needs its `module-name` setting, which older uv treats as a fatal parse error rather than a warning. Extensions follow it - `statsbadge-clock` on PyPI, `statsbadge_clock` to import, `clock` to `statsbadge ext add`.

## Contributing

```
src/statsbadge/            the host server, a normal Python package
src/statsbadge/badge_app/  the badge app - MicroPython, runs only on the badge
src/statsbadge/web/        the config UI
extensions/                one package per extension
tools/                     host and on-badge development tools
tests/                     server, auth and framing tests
```

Releases are cut by tagging: `vX.Y.Z` for statsbadge itself, `clock-vX.Y.Z` and the like for
a vendored extension. The tag *is* the version, so a release cannot disagree with what it
publishes.

[DEVELOPMENT.md](https://github.com/pimoroni/statsbadge/blob/main/DEVELOPMENT.md) is the
onboarding document: the wire contract, the constraints the badge imposes, how to write an
extension, and the commands for working on any of it.
