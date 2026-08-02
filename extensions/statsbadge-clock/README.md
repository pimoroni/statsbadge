# statsbadge-clock

A worked example of a [statsbadge](https://github.com/pimoroni/stats-badge) extension, showing all three parts of the mechanism:

1. **Data in the frame.** A `clock` group and a `weather` group, which the badge's built-in page kinds can draw with no badge-side code at all - `clock.time` in a `text` page just works.
2. **Badge-side code**, for a page the built-in kinds cannot draw. `src/statsbadge_clock/badge/clockface.py` registers a `clockface` kind, and `statsbadge install` pushes it to the badge.
3. **Keeping what it worked out.** `self.store` is a namespaced dict the host persists, and the coordinates a place name resolved to go in it: a town does not move, so the geocoder is asked once per name rather than once per launch.

The second is the point: the clock's second hand is carried forward from the badge's frame clock between polls, so it sweeps at 45fps off one reading a second. An image over the wire would tick once a second and cost a fetch each time.

```bash
uv pip install --no-deps ./extensions/statsbadge-clock
statsbadge install
```

Weather comes from [Open-Meteo](https://open-meteo.com), which needs no API key. Set a **Place** under Extensions in the config UI - a town or city, and a country after a comma if the name is a common one:

```
Sheffield          the one most people mean, by how well known it is
Sheffield, US      Alabama
Paris, US          Texas
```

Names are resolved through Open-Meteo's own geocoder, which also needs no key, once per name rather than once per forecast. What it resolved to comes back as `weather.place`, so the Live panel shows which Sheffield you got.

Latitude and longitude are still there for a spot no name lands on, and win where they are set. The same settings work on the command line, for a host with no browser near it:

```bash
statsbadge serve --extension clock.place=Sheffield
statsbadge serve --extension clock.latitude=53.38 --extension clock.longitude=-1.47
```

A setting stored by the UI wins over the flag, since the UI is the live editor and the flag is for a first run.

Without a location the clock still works and the weather readouts read "no location set".

The settings themselves are declared on the source as `settings`, which is what the UI builds its fields from. An extension that declares none gets no section.

## Faces

Each clock page picks one, under its own settings in the config UI:

| Face | What it is |
| ---- | ---------- |
| `railway` | Hilfiker's station clock in the Mondaine colourway, keeping its own livery |
| `dots` | Koppel's dotted minute track, needle hands with a spike opposite each |
| `squircle` | The badge's own furniture, in the page theme |
| `digital` | No dial: date, place, the time the height of the band, weather under it |
| `lcd` | The same layout in seven-segment digits, over their own unlit segments |

The seven segments are [DSEG](https://github.com/keshikan/DSEG) by keshikan - DSEG7 Classic Bold, under the SIL Open Font License, packed into `badge/lcd.af` and pushed to the badge as an asset beside the module:

```bash
python3 tools/make_text_font.py build/fonts/DSEG7Classic-Bold.ttf \
        --chars "0123456789: " --cap 122 --cap-from 8 \
        --out extensions/statsbadge-clock/src/statsbadge_clock/badge/lcd.af
```

`--cap-from 8` because a face that only draws numbers has no `H` to measure a cap height from, and `--cap 122` packs it at the finest grid a signed byte holds. Its licence is in [licences/OFL-DSEG.txt](https://github.com/pimoroni/stats-badge/blob/main/licences/OFL-DSEG.txt).

## Working on it

Install it editable, or an edit here does nothing: a plain `uv pip install` copies the package, and installing again over an unchanged version is a no-op, so the code that runs stays the snapshot from the first install.

```bash
uv pip install --no-deps -e ./extensions/statsbadge-clock
```
