#!/usr/bin/env python3
"""
Generate the local Kentucky 20x20 EPSG:3089 raster tile index.

The viewer footprint file is EPSG:3857 for browser display. MapServer's raster
TILEINDEX needs source-native EPSG:3089 bounds so it can select the COGs that
intersect a GetMap request after reprojection.
"""

import json
import re
from pathlib import Path

SOURCE = Path("mapfiles/ky_20x20_tileindex.geojson")
TARGET = Path("mapfiles/ky_20x20_tileindex_3089.geojson")

TILE_SIZE_FT = 5000
X_INTERCEPT_FT = 3775000
Y_INTERCEPT_FT = 4365000
NAME_RE = re.compile(r"^N(?P<n>\d{3})E(?P<e>\d{3})_")


def native_bounds(file_name):
    match = NAME_RE.match(file_name)
    if not match:
        raise ValueError(f"Cannot parse Kentucky tile name: {file_name}")

    northing = int(match.group("n"))
    easting = int(match.group("e"))

    min_x = X_INTERCEPT_FT + easting * TILE_SIZE_FT
    max_x = min_x + TILE_SIZE_FT
    max_y = Y_INTERCEPT_FT - northing * TILE_SIZE_FT
    min_y = max_y - TILE_SIZE_FT

    return northing, easting, min_x, min_y, max_x, max_y


def polygon(min_x, min_y, max_x, max_y):
    return [
        [
            [float(min_x), float(min_y)],
            [float(max_x), float(min_y)],
            [float(max_x), float(max_y)],
            [float(min_x), float(max_y)],
            [float(min_x), float(min_y)],
        ]
    ]


def main():
    source = json.loads(SOURCE.read_text())
    features = []

    for source_feature in source["features"]:
        props = dict(source_feature["properties"])
        file_name = props["file_name"]
        northing, easting, min_x, min_y, max_x, max_y = native_bounds(file_name)
        props["n"] = northing
        props["e"] = easting

        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": polygon(min_x, min_y, max_x, max_y),
                },
            }
        )

    tileindex = {
        "type": "FeatureCollection",
        "name": "ky_20x20_tileindex_3089",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::3089"},
        },
        "features": features,
    }

    TARGET.write_text(json.dumps(tileindex, separators=(",", ":")) + "\n")
    print(f"Wrote {TARGET} with {len(features)} features")


if __name__ == "__main__":
    main()
