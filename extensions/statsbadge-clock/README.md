# statsbadge-clock

A clock and the weather, for [statsbadge](https://github.com/pimoroni/stats-badge).

Five faces: a Swiss railway station clock whose second hand sweeps at the badge's frame rate, Koppel's dotted minute track, the badge's own squircle in whatever theme it is wearing, and two digital faces - one set in the app's own typeface, one in seven-segment digits over their unlit segments.

![Railway](https://raw.githubusercontent.com/pimoroni/stats-badge/main/shots/swiss_clock.png) ![Dots](https://raw.githubusercontent.com/pimoroni/stats-badge/main/shots/face_dots.png) ![Squircle](https://raw.githubusercontent.com/pimoroni/stats-badge/main/shots/face_squircle.png) ![Digital](https://raw.githubusercontent.com/pimoroni/stats-badge/main/shots/face_digital.png) ![Digital LCD](https://raw.githubusercontent.com/pimoroni/stats-badge/main/shots/face_lcd.png)

Weather comes from [Open-Meteo](https://open-meteo.com): the condition with a symbol for the sky, temperature, what it feels like, humidity and the wind. No key and no account.

## Install

```bash
statsbadge ext add clock
statsbadge install
```

Then add a **Clock** page in the config UI.

## Settings

Set a **Place** under Extensions - a town or city, and a country after a comma if the name is a common one:

```
Sheffield          the one most people mean, by how well known it is
Sheffield, US      Alabama
Paris, US          Texas
```

Which one you got comes back as `weather.place`, so the Live panel shows it. Latitude and longitude are there for a spot no name lands on, and win wherever both are set. Temperature reads in celsius or fahrenheit, wind in km/h, mph, m/s or knots.

Every clock page then has settings of its own, so two pages can show two cities. A place settles the time as well as the weather, because Open-Meteo returns a location's UTC offset with its forecast: point a page at Tokyo and it shows Tokyo's time, with no timezone to set anywhere. Each page picks its own face:

| Face | What it is |
| ---- | ---------- |
| `railway` | Hilfiker's station clock in the Mondaine colourway, keeping its own livery |
| `dots` | Koppel's dotted minute track, needle hands with a spike opposite each |
| `squircle` | The badge's own furniture, in the page theme |
| `digital` | No dial: date, place, the time the height of the band, weather under it |
| `lcd` | The same layout in seven-segment digits, over their own unlit segments |

Without a location the clock still keeps time, and the weather readouts read "no location set".

The same settings work from the command line, for a host with no browser near it. Anything stored by the UI wins over the flag.

```bash
statsbadge serve --extension clock.place=Sheffield
statsbadge serve --extension clock.latitude=53.38 --extension clock.longitude=-1.47
```

## Notes

The hands run off the badge's own clock, set once from the host, so they keep time when the host goes away and the second hand sweeps whether or not a reading has landed. A place name is looked up once ever rather than once a launch.

The seven segments are [DSEG](https://github.com/keshikan/DSEG) by keshikan, under the SIL Open Font License, whose text travels with the package.
