# statsbadge-clock

A worked example of a [statsbadge](../../README.md) extension, showing both halves of the mechanism:

1. **Data in the frame.** A `clock` group and a `weather` group, which the badge's built-in page kinds can draw with no badge-side code at all - `clock.time` in a `text` page just works.
2. **Badge-side code**, for a page the built-in kinds cannot draw. `src/statsbadge_clock/badge/clockface.py` registers a `clockface` kind, and `statsbadge install --with-extensions` pushes it to the badge.

The second half is the point: the clock's second hand is carried forward from the badge's frame clock between polls, so it sweeps at 45fps off one reading a second. An image over the wire would tick once a second and cost a fetch each time.

```bash
uv pip install --no-deps ./extensions/statsbadge-clock
statsbadge install --with-extensions
```

Weather comes from [Open-Meteo](https://open-meteo.com), which needs no API key. Set a location to enable it:

```bash
statsbadge serve --extension clock.latitude=52.4 --extension clock.longitude=-1.9
```

Without a location the clock still works and the weather readouts read "no location set".
