# statsbadge-iss

Where the space station is, for [statsbadge](https://github.com/pimoroni/statsbadge).

![ISS](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/iss.png) ![ISS following](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/iss_follow.png)

An orbit of ground track either side of it: forty-five minutes behind and fifty ahead, dimmed where the station is in the earth's shadow. The day and night terminator is washed over the half of the world the sun is not on. The marker takes the accent when the station is in sunlight and goes quiet when it is not.

Under the map: how high, where, how fast, whether it is sunlit, and how many people are aboard.

Positions come from [wheretheiss.at](https://wheretheiss.at) and the crew from [open-notify.org](http://open-notify.org). No key and no account.

## Install

```bash
statsbadge ext add iss
statsbadge install
```

Then add an **ISS** page in the config UI. It works with nothing configured.

## Settings

| Setting | What it does |
| ------- | ------------ |
| Distances in | Kilometres or miles, for the altitude and the speed |
| Show the crew | How many people are aboard |
| Camera | Per page: the whole world with the station crossing it, or closed in and travelling with it |

## Notes

The track is asked for rather than guessed: the position feed answers a list of timestamps, so twenty positions either side of now arrive together and there is an orbit to draw the moment you turn to the page. It is refetched every two minutes, and the station walks along it meanwhile.

The terminator costs nothing extra. The position reply carries the sub-solar point, so the curve is drawn from two numbers with no almanac and no clock to trust.

The map is the badge firmware's own, shared with [statsbadge-quakes](https://github.com/pimoroni/statsbadge/tree/main/extensions/statsbadge-quakes), so running both costs one copy of the coastlines.

`iss.lat`, `iss.lon`, `iss.altitude` and `iss.speed` are in the frame too, for a `text` or `dial` page that wants a number rather than a map.
