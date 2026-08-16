"""The firmware's world map, for any page that draws on one.

`/system/assets/world.geo.json` ships with badgeware: 177 countries in 288 polygons and
10,593 points. It is parsed once into a shape per polygon and then only re-aimed, a mat3
being the whole of placing one. It is held here and not in the page that asked, so two map
pages cost one parse. Measured on the badge: 1256ms and 184KB.

A page draws through a `View`, which is where the map sits on the screen and where it is
looking. What goes on top of it, and how the camera moves, are the page's business.
"""

import gc
import json
import math

import draw
import look

FILE = "/system/assets/world.geo.json"

# Latitude drawn taller than longitude, or an equirectangular map looks squashed.
ASPECT = 1.3

# How much of the ramp colour the land is given, under whatever a page draws on top.
LAND_ALPHA = 104
# Where the ramp is anchored, in degrees from the equator: tropics hot, ice caps cold.
LAND_SPAN = 90.0
# Steps of the ramp the land is drawn in. A pen assignment is 64 bytes, so one per
# polygon would be 18KB a frame. The polygons are sorted by band, and the pen is set
# once a band.
LAND_BANDS = 24

# How dark the night side goes: toward the page on a dark theme, toward the ink on a
# pale one, where the dark theme's weight would flatten the map to grey.
NIGHT_ALPHA = 150
NIGHT_PALE_ALPHA = 64
# How coarsely the edge is drawn. Three degrees is a 3px chord at whole-world zoom.
NIGHT_STEP = 3

# One entry per polygon: the shape in degrees, its middle, and the box it covers. Most
# of the world is off screen at any zoom, and the box is what makes skipping it cheap.
_shapes = None
# Where each band starts and stops in `_shapes`, which is held sorted by band.
_bands = ()
# Whether a frame has put the notice up, so the parse lands in the frame after it.
_asked = False
_pens = {}


def ready():
    """False the first time, arming the parse; True from then on.

    Reading 215KB and building 288 shapes from it is over a second, which is a frame that
    never arrives. A page draws a notice on the False, and parses on the next frame.
    """
    global _asked
    if _shapes is not None:
        return True
    if not _asked:
        _asked = True
        return False
    shapes()
    return True


def shapes():
    """Every polygon as a shape in degrees, parsed on first use.

    Points are (lon, -lat), so one mat3 both scales and places the map. The file is dropped
    as soon as the shapes are built: a list per point, 827KB of the heap.
    """
    global _shapes
    if _shapes is not None:
        return _shapes
    built = []
    try:
        with open(FILE) as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        print(f"worldmap: no map in {FILE}: {exc}")
        _shapes = ()
        return _shapes
    for country in data:
        for polygon in country.get("polygons") or ():
            if len(polygon) < 3:
                continue
            path = []
            lon_min = lat_min = 1000.0
            lon_max = lat_max = -1000.0
            for lon, lat in polygon:
                path.append(vec2(lon, -lat))
                lon_min = min(lon_min, lon)
                lon_max = max(lon_max, lon)
                lat_min = min(lat_min, lat)
                lat_max = max(lat_max, lat)
            built.append((shape.custom(path), (lon_min + lon_max) * 0.5,
                          (lat_min + lat_max) * 0.5, lon_min, lon_max, lat_min, lat_max))
    # Order within a band does not matter: filled land masses at one latitude, and they
    # do not overlap.
    built.sort(key=_band_of)
    _shapes = tuple(built)
    _find_bands()
    del data, built
    gc.collect()
    return _shapes


def _band_of(entry):
    """Which step of the ramp a polygon is drawn in, from the latitude of its middle."""
    fraction = 1.0 - min(1.0, abs(entry[2]) / LAND_SPAN)
    return min(LAND_BANDS - 1, int(fraction * LAND_BANDS))


def _find_bands():
    """Where each band starts and stops in the sorted shapes."""
    global _bands
    edges = []
    start = 0
    for band in range(LAND_BANDS):
        stop = start
        while stop < len(_shapes) and _band_of(_shapes[stop]) == band:
            stop += 1
        edges.append((start, stop))
        start = stop
    _bands = tuple(edges)


def pens(theme, alpha=LAND_ALPHA):
    """One pen per band of the ramp, the colour for that latitude over the page.

    Cached per theme, so a frame builds none of them. See LAND_BANDS.
    """
    key = (theme.key, alpha)
    found = _pens.get(key)
    if found is None:
        # Dropped rather than grown: a badge cycling themes would keep a set for each.
        if len(_pens) > 2:
            _pens.clear()
        found = _pens[key] = tuple(
            theme.at((band + 0.5) / LAND_BANDS).with_alpha(alpha).over(theme.bg)
            for band in range(LAND_BANDS))
    return found


@draw.clears
def forget():
    """Drop the pens on a theme change. The shapes stay: they hold no colour.

    The key is the theme's name, which two tints of one derived theme share, so a tint
    changed on its own would otherwise draw the map in the ramp it had before.
    """
    _pens.clear()


def shortest(degrees):
    """A difference in longitude taken the short way round, so nothing crosses the date line."""
    return degrees - 360.0 * math.floor(degrees / 360.0 + 0.5)


def terminator_at(lon, solar_lon, solar_lat):
    """The latitude the sun sets at, for one longitude.

    From the sun's altitude being zero: sin(alt) = sin(lat)sin(dec) + cos(lat)cos(dec)cos(H),
    which rearranges to tan(lat) = -cos(H)/tan(dec). At an equinox tan(dec) is nothing and the
    terminator is a meridian, so the divisor is held off zero and the answer saturates at a
    pole, which is the same line.
    """
    hour_angle = math.radians(lon - solar_lon)
    slope = math.tan(math.radians(solar_lat))
    if abs(slope) < 1e-6:
        slope = 1e-6 if slope >= 0 else -1e-6
    return math.degrees(math.atan(-math.cos(hour_angle) / slope))


def night_path(solar_lon, solar_lat):
    """The dark half of the world, as a closed path in map degrees.

    The terminator is a curve, and the wash is that curve closed off at a pole; which pole
    sets which half fills. The lit pole is the one the sun is over, so closing at a fixed
    one fills the day side for half the year.
    """
    dark_pole = -90.0 if solar_lat >= 0 else 90.0
    path = [vec2(-180.0, -dark_pole)]
    for step in range(-180, 181, NIGHT_STEP):
        path.append(vec2(step, -terminator_at(step, solar_lon, solar_lat)))
    path.append(vec2(180.0, -dark_pole))
    return path


class View:
    """Where a map is drawn, and where it is looking.

    `scale` is pixels per degree of longitude. A page holds one of these, moving it; two
    pages hold two, and share the shapes underneath.
    """

    def __init__(self, top, height, lon=0.0, lat=0.0, scale=1.0):
        self.top = top
        self.height = height
        self.box = rect(0, top, look.W, height)
        self.mid = (look.W // 2, top + height // 2)
        self.lon = lon
        self.lat = lat
        self.scale = scale
        self._night = None
        self._night_for = None
        # A transform per whole turn of longitude, refilled each frame by `land`.
        self._placed = {}

    def at(self, lon, lat):
        """Where a point in degrees lands on the screen."""
        return (self.mid[0] + shortest(lon - self.lon) * self.scale,
                self.mid[1] - (lat - self.lat) * self.scale * ASPECT)

    def holds(self, x, y):
        return 0 <= x < look.W and self.top <= y < self.top + self.height

    def look_at(self, lon, lat, scale, elapsed, ease_ms):
        """Move toward a place and a zoom, easing on a time constant.

        Against elapsed ms rather than a share of each frame, so the travel takes as long
        whatever the page costs to draw.
        """
        step = max(0.0, min(1.0, elapsed / ease_ms))
        self.lon = shortest(self.lon + shortest(lon - self.lon) * step)
        self.lat += (lat - self.lat) * step
        self.scale += (scale - self.scale) * step

    def jump_to(self, lon, lat, scale=None):
        self.lon = shortest(lon)
        self.lat = lat
        if scale is not None:
            self.scale = scale

    def land(self, theme, alpha=LAND_ALPHA):
        """Every polygon in view, drawn where this view is looking.

        Clipped here, because nothing about a polygon placed by a transform stops it at the
        edge of the page's band, and a clip left set is inherited by the next page.
        """
        entries = shapes()
        if not entries:
            return 0
        colours = pens(theme, alpha)
        scale = self.scale
        half_lon = (look.W * 0.5) / scale
        half_lat = (self.height * 0.5) / (scale * ASPECT)
        base_y = self.lat * scale * ASPECT + self.mid[1]
        drawn = 0
        was = screen.clip
        screen.clip = self.box
        # Looked up once: this runs 288 times a frame and the attribute lookup is not free.
        local_floor = math.floor
        # One transform per whole turn of longitude, of which a frame sees two or three.
        # One per polygon would be 288 allocations of 32 bytes.
        placed = self._placed
        placed.clear()
        for band, (first, last) in enumerate(_bands):
            # Set on the first polygon in view, so a band entirely off screen sets none.
            inked = False
            for index in range(first, last):
                entry = entries[index]
                outline, lon_mid, _lat_mid, lon_min, lon_max, lat_min, lat_max = entry
                # Nearest whole turn of longitude to the camera, which wraps the map instead of
                # ending it at the date line.
                turn = 360.0 * local_floor((self.lon - lon_mid) / 360.0 + 0.5)
                if (lon_min + turn - self.lon > half_lon
                        or self.lon - lon_max - turn > half_lon):
                    continue
                if lat_min - self.lat > half_lat or self.lat - lat_max > half_lat:
                    continue
                if not inked:
                    screen.pen = colours[band]
                    inked = True
                transform = placed.get(turn)
                if transform is None:
                    transform = placed[turn] = mat3().translate(
                        (turn - self.lon) * scale + self.mid[0],
                        base_y).scale(scale, scale * ASPECT)
                outline.transform = transform
                screen.shape(outline)
                drawn += 1
        screen.clip = was
        return drawn

    def night(self, theme, solar_lon, solar_lat, alpha=None):
        """Wash the half of the world the sun is not on.

        See NIGHT_ALPHA for which way the wash goes. The curve is in degrees like the land, so
        it is rebuilt only when the sun has moved, a quarter of a degree a minute.
        """
        key = (int(solar_lon), round(solar_lat, 1))
        if self._night_for != key:
            self._night = shape.custom(night_path(solar_lon, solar_lat))
            self._night_for = key
        if theme.pale:
            wash = theme.ink.with_alpha(NIGHT_PALE_ALPHA if alpha is None else alpha)
        else:
            wash = theme.bg.with_alpha(NIGHT_ALPHA if alpha is None else alpha)
        scale = self.scale
        base_y = self.lat * scale * ASPECT + self.mid[1]
        nearest = 360.0 * math.floor(self.lon / 360.0 + 0.5)
        half_lon = (look.W * 0.5) / scale
        was = screen.clip
        screen.clip = self.box
        screen.pen = wash
        # Up to three copies, for a view wide enough to see past a date line either side.
        # At whole-world zoom one or two cover it and the rest are 123 edges of nothing.
        for turn in (nearest - 360.0, nearest, nearest + 360.0):
            if turn - 180.0 > self.lon + half_lon or turn + 180.0 < self.lon - half_lon:
                continue
            self._night.transform = mat3().translate(
                (turn - self.lon) * scale + self.mid[0], base_y).scale(scale, scale * ASPECT)
            screen.shape(self._night)
        screen.clip = was
