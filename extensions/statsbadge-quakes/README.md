# statsbadge-quakes

Recent earthquakes on a world map, for [statsbadge](https://github.com/pimoroni/statsbadge).

![Quakes](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/quakes.png) ![Quakes pulled out](https://raw.githubusercontent.com/pimoroni/statsbadge/main/shots/quakes_wide.png)

The page cycles through the set on its own, closing in on each event and pulling back out to cross an ocean. Rings leave the epicentre in the magnitude's own colour off the theme ramp, so how big it was is something you can read at a glance from across the room. Under the map: the magnitude, where it was, how deep and how long ago.

Events come from [USGS](https://earthquake.usgs.gov/fdsnws/event/1/), which needs no key and no account.

## Install

```bash
statsbadge ext add quakes
statsbadge install
```

Then add a **Quakes** page in the config UI. It works with nothing configured.

## Settings

| Setting | What it does |
| ------- | ------------ |
| Smallest magnitude | Below about 4 the feed fills up with events nobody felt: there are several thousand a month |
| How many | How many events the map cycles through |
| Show the | `recent` is the last few hours, `biggest` is the largest of the past month |
| Seconds each | Per page: how long the map holds on one event before travelling to the next |

## Notes

The map is the badge firmware's, so nothing is downloaded and no geometry crosses the network: the host sends ten events with their coordinates. Land takes its colour from the theme ramp by latitude, tropics at the hot end and ice caps at the cold one, so the map is themed along with everything else.

The last set is kept, so a badge switched on before the network is up has something to draw.

**Largest magnitude**, **Latest magnitude** and **How many** are offered as readings too, under Earthquakes in the field pickers, for a `text` or `dial` page drawing a number and not a map.

The feed is asked for every five minutes and the badge polls every second, so events travel only on a change. An age is drawn to the minute and worked out against the minute just gone, which is what keeps a set that has not moved from being sent. Ten events are about 1.1KB, so that is 1.1KB a second saved for a page that changes twelve times an hour.
