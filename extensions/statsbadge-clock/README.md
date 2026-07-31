# statsbadge-clock

A worked example of a [statsbadge](../../README.md) extension, showing both halves of the mechanism:

1. **Data in the frame.** A `clock` group and a `weather` group, which the badge's built-in page kinds can draw with no badge-side code at all - `clock.time` in a `text` page just works.
2. **Badge-side code**, for a page the built-in kinds cannot draw. `src/statsbadge_clock/badge/clockface.py` registers a `clockface` kind, and `statsbadge install --with-extensions` pushes it to the badge.

The second half is the point: the clock's second hand is carried forward from the badge's frame clock between polls, so it sweeps at 45fps off one reading a second. An image over the wire would tick once a second and cost a fetch each time.

```bash
uv pip install --no-deps ./extensions/statsbadge-clock
statsbadge install --with-extensions
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

## Working on it

Install it editable, or an edit here does nothing: a plain `uv pip install` copies the package, and installing again over an unchanged version is a no-op, so the code that runs stays the snapshot from the first install.

```bash
uv pip install --no-deps -e ./extensions/statsbadge-clock
```
