# statsbadge

Your PC's vitals on a [Badgeware Tufty 2350](https://shop.pimoroni.com/products/tufty-2350). A compact, wireless and extensible hardware monitor for Windows, macOS and Linux.

![CPU](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/cpu.png) ![Load](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/gauges.png)

Runs on [Badgeware's Tufty](https://shop.pimoroni.com/products/tufty-2350), firmware v3.0.0 and above, with a 2.8" 320x240 colour IPS display.

A server on your PC measures your local system stats, fetches and transforms data from APIs, and feeds your badge with the data it needs over WiFi. Fully customise the badge layout in a local web UI and apply your choice of theme or colour accent.

About 12 hours on battery with the backlight low, so a badge that sits on your desk ideally wants a power supply.

## Install

A `.dmg` for macOS and an `.msi` for Windows, on the [latest release](https://github.com/pimoroni/statsbadge/releases/latest). On Windows you'll need to choose **More info** and then **Run anyway**.

On Linux, or to have it on your PATH:

```bash
uv tool install statsbadge     # or: pip install statsbadge
statsbadge tray
```

## Setting up a badge

Plug a Tufty 2350 running [firmware v3.0.0 or above](https://github.com/pimoroni/tufty2350/releases/tag/v3.0.2) in and press **Update badge** in the config UI, from the tray menu. That copies the app across, sets up WiFi if you tick the box, and pairs the badge with this computer.

Without a cable, press **Pair a badge**, then launch **Stats** on the badge and press **B**. It finds the computer by itself and shows a six-character code to check and approve.

A badge can be paired with several computers, and draws whichever one it can reach.

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

See the **Help** tab for help setting up advanced local sensor readings.

## Pages

Twelve ways to lay out your data:

| Page | Layout |
| ---- | ------ |
| `dial` | one reading as a big sweep gauge, with three smaller readouts beside it |
| `dials` | up to four readings as gauges side by side |
| `bars` | a list of readings as horizontal bars, good for per-core |
| `graph` | one or two readings over time |
| `grid` | up to six readings as big numbers |
| `text` | labelled lines, for names and versions |
| `rings` | up to four readings as concentric gauges, each coloured by its value |
| `spark` | six readings at once, name, current value and recent history a row each |
| `radar` | three to six readings as a polygon: the shape of the load rather than its size |
| `trend` | one big reading, which way it is going, and where it has been |
| `waterfall` | a list field as lanes over time, interpolated between polls |
| `badge` | the badge's own vitals, which need no reading from the host |

**Quick add** offers ready-made pages - CPU Overview, Network Graph, System Overview - with their fields already chosen.

UP and DOWN page back and forth, HOME opens the list of computers, and A, B and C run whatever you bind them to: a command on the host, or paging and brightness on the badge.

![Cores](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/cores.png) ![Network](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/net.png) ![Disk](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/disk.png) ![Processor](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/gauges2.png) ![Host](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/host.png) ![Badge](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/badge.png)

![Waterfall](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/waterfall.png) ![Rings](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/rings.png) ![Sparklines](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/spark.png) ![Radar](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/radar.png) ![Trend](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/trend.png) ![The whole ramp](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/dial_ramp.png)

## Themes

Sixteen themes, previewed in the config UI as you pick them. Four of them are built from an accent of your own, in twelve hues and four families.

![Vapor](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_vapor.png) ![Sakura](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_sakura.png) ![Watermelon](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_watermelon.png) ![Shell](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_shell.png) ![Unit-01](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_eva01.png) ![Luminescence](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/theme_luminescence.png)

## Extensions

Go to the **Extensions** tab to see and install available extensions, adding readings and even new badge layouts.

- [statsbadge-clock](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-clock), five clock faces and the weather, for as many places as you have pages.
- [statsbadge-quakes](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-quakes), recent earthquakes on a world map, from USGS.
- [statsbadge-iss](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-iss), the space station across the same map, with its ground track and the day and night terminator.

Mastodon, Bluesky, Cloudflare, Octopus Energy and global wildfires plugins are also installable from the config UI.

![Railway](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/swiss_clock.png) ![Dots](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_dots.png) ![Digital LCD](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/face_lcd.png) ![Quakes](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/quakes.png) ![ISS](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/iss.png)

## Security

Everything a badge and a computer exchange is signed against a secret they agreed at pairing. The config UI is bound to 127.0.0.1 (not available over LAN) and host commands only run if you've bound one to a button.

