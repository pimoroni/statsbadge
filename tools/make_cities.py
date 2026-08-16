#!/usr/bin/env python3
"""Pack the settlement table `geocode.nearest` names a coordinate from.

    python3 tools/make_cities.py
    python3 tools/make_cities.py --source cities5000 --list

Writes `src/statsbadge/cities.tsv.gz`, which ships in the wheel and is committed, the way
the .af fonts are: a contributor gets the table with the checkout and needs no network.

The source is GeoNames, whose `cities15000` is every settlement over 15,000 people. That is
the granularity a fire or an earthquake is named at - the nearest town somebody would
recognise, not the nearest hamlet - and it is what USGS names its quakes against.

Names are taken from the ASCII column. The badge draws these with lexend, which packs no
Cyrillic or CJK, so a name it cannot draw is worse than a transliterated one.

GeoNames is CC BY 4.0. The attribution is written into the file header and repeated in
README.md, and has to stay on both.
"""

import argparse
import gzip
import io
import pathlib
import sys
import urllib.request
import zipfile

BASE = "https://download.geonames.org/export/dump/"
CACHE = pathlib.Path("build/geonames")
PACKED = pathlib.Path("src/statsbadge/cities.tsv.gz")

CREDIT = ("# GeoNames {source}, CC BY 4.0, https://www.geonames.org/\n"
          "# tools/make_cities.py: name\tcountry\tlatitude\tlongitude\tthousands\n")

# Columns of the GeoNames dump this reads. The file has nineteen; the rest are admin
# codes, alternate names and timezones that no caller here asks for.
ASCII_NAME = 2
LATITUDE = 4
LONGITUDE = 5
COUNTRY = 8
POPULATION = 14

# Decimal places kept. Three is about 110m, far finer than a distance printed in whole
# kilometres needs, and it holds the table under half a megabyte.
PLACES = 3


def fetch(source):
    """The GeoNames dump, from the cache where a previous run left one."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{source}.zip"
    if not cached.exists():
        print(f"fetching {source} into {cached}")
        partial = cached.with_suffix(".zip.part")
        try:
            urllib.request.urlretrieve(f"{BASE}{source}.zip", partial)
            partial.replace(cached)
        finally:
            partial.unlink(missing_ok=True)
    with zipfile.ZipFile(cached) as archive:
        return archive.read(f"{source}.txt").decode("utf-8")


def rows(dump):
    """(name, country, latitude, longitude, thousands), by country and then by name.

    That order is for the packer: grouping the country codes into runs and the names by
    language takes 75KB off the gzip against sorting by population. Nothing reads the file
    in order.

    Population is kept in thousands, which `nearest` needs only to tell a city from the
    suburb next to it. Rows without a name, a country or a position are dropped.
    """
    found = []
    for line in dump.splitlines():
        fields = line.split("\t")
        if len(fields) <= POPULATION:
            continue
        name = fields[ASCII_NAME].strip()
        country = fields[COUNTRY].strip()
        if not name or not country:
            continue
        try:
            latitude = round(float(fields[LATITUDE]), PLACES)
            longitude = round(float(fields[LONGITUDE]), PLACES)
            thousands = int(fields[POPULATION] or 0) // 1000
        except ValueError:
            continue
        found.append((name, country, latitude, longitude, thousands))
    found.sort(key=lambda row: (row[1], row[0]))
    return found


def pack(found, source, into):
    body = io.StringIO()
    body.write(CREDIT.format(source=source))
    for name, country, latitude, longitude, thousands in found:
        body.write(f"{name}\t{country}\t{latitude:g}\t{longitude:g}\t{thousands}\n")
    into.parent.mkdir(parents=True, exist_ok=True)
    # mtime zeroed, so rebuilding an unchanged table is not a diff.
    with gzip.GzipFile(into, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(body.getvalue().encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="cities15000",
                        help="GeoNames dump to pack (cities15000, cities5000, cities1000)")
    parser.add_argument("--out", type=pathlib.Path, default=PACKED)
    parser.add_argument("--list", action="store_true", help="print the ten largest")
    args = parser.parse_args(argv)

    found = rows(fetch(args.source))
    if not found:
        print(f"{args.source} carried no usable rows", file=sys.stderr)
        return 1
    pack(found, args.source, args.out)
    if args.list:
        for name, country, latitude, longitude, thousands in sorted(
                found, key=lambda row: -row[4])[:10]:
            print(f"  {name}, {country}  {latitude:g} {longitude:g}  {thousands}k")
    print(f"{len(found)} settlements into {args.out} "
          f"({args.out.stat().st_size / 1024:.0f}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
