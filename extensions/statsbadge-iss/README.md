# statsbadge-iss

Where the space station is, as a [statsbadge](https://github.com/pimoroni/stats-badge) extension.

```bash
uv pip install --no-deps ./extensions/statsbadge-iss
statsbadge install
```

Then add an **ISS** page in the config UI. Nothing to configure to get started: positions come from [wheretheiss.at](https://wheretheiss.at) and the crew from [open-notify.org](http://open-notify.org), neither of which needs a key or an account.

| Setting | What it does |
| ------- | ------------ |
| Distances in | Kilometres or miles, for the altitude and the speed |
| Show the crew | How many people are aboard |
| Camera | Per page: the whole world with the station crossing it, or closed in and travelling with it |

The map is the app's `worldmap`, shared with [statsbadge-quakes](https://github.com/pimoroni/stats-badge/tree/main/extensions/statsbadge-quakes), so two map pages cost one parse of the firmware's coastlines.

Three things are drawn on it:

- **The ground track**, an orbit of it: forty-five minutes behind in the second accent and fifty ahead in the first, dimmed where the station is in the earth's shadow. It is asked for rather than integrated, because the same endpoint answers a list of timestamps: twenty positions either side of now, refetched every two minutes. Keeping a trail of observed positions instead would have drawn nothing until the badge had been up for most of an orbit.
- **The day and night terminator**, which comes free: the position reply carries the sub-solar point, so the badge builds the curve from two numbers and nothing has to know what day it is. The wash goes toward the page on a dark theme and toward the ink on a pale one.
- **The station**, in the accent when it is in sunlight and the dim colour when it is not, over a soft halo.

Positions are fetched every five seconds, which is a pixel of movement on a whole-world map and nothing like the one request a second the API allows.

The page is drawn when a reading lands rather than at the badge's frame rate, unlike the quake map: with the whole world in view a frame is 78ms, and nothing on it moves between readings - the station covers 0.06 pixels a second.

`iss.lat`, `iss.lon`, `iss.altitude` and `iss.speed` are in the frame too, for anything wanting a number rather than a map.

## Working on it

Install it editable, or an edit here does nothing: a plain `uv pip install` copies the package.

```bash
uv pip install --no-deps -e ./extensions/statsbadge-iss
```
