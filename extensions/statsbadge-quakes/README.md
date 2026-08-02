# statsbadge-quakes

Recent earthquakes on a world map, as a [statsbadge](../../README.md) extension.

```bash
uv pip install --no-deps ./extensions/statsbadge-quakes
statsbadge install
```

Then add a **Quakes** page in the config UI. There is nothing to configure to get started: the feed is [USGS](https://earthquake.usgs.gov/fdsnws/event/1/), which needs no key and no account.

The page cycles through the set on its own, closing in on each event and pulling out to cross an ocean. It is ambient: there is nothing to press.

| Setting | What it does |
| ------- | ------------ |
| Smallest magnitude | Below about 4 the feed fills up with events nobody felt |
| How many | How many events the map cycles through |
| Show the | `recent` is the last few hours, `biggest` is the largest of the past month |
| Seconds each | Per page: how long the map holds on one event |

The map itself is the firmware's, `/system/assets/world.geo.json`, so nothing is shipped with this extension and no geometry goes over the wire: the host sends ten events and the badge knows where they are. 177 countries in 288 polygons, parsed once into a shape apiece on the first frame that needs one, and then only re-aimed by a `mat3` per frame.

Land takes its colour from the theme ramp by latitude, the tropics at the hot end and the ice caps at the cold one, thinned over the page so it stays background. The epicentre's rings take the ramp again, by magnitude, so the colour of the reticle is how big the quake was.

Scalars go in the frame beside the events, for anything that wants a number rather than a map: `quakes.biggest`, `quakes.latest` and `quakes.count` all work in a text page.

## Working on it

Install it editable, or an edit here does nothing: a plain `uv pip install` copies the package.

```bash
uv pip install --no-deps -e ./extensions/statsbadge-quakes
```
