#!/usr/bin/env python3
"""
Generate a GeoJSON file of COG tile footprints from the VRT.
Reads DstRect from band 1 only, converts EPSG:2193 → WGS84.
Output is suitable for loading directly in Leaflet.
"""

import json
import xml.etree.ElementTree as ET
from osgeo import osr

VRT = "/data/auckland_2024.vrt"
OUTPUT = "/data/tile_extents.geojson"


def main():
    print("Parsing VRT...", flush=True)
    tree = ET.parse(VRT)
    root = tree.getroot()

    # Parse GeoTransform: min_x, res, 0, max_y, 0, -res
    gt_text = root.find("GeoTransform").text
    gt = [float(v.strip()) for v in gt_text.split(",")]
    min_x, res, _, max_y = gt[0], gt[1], gt[2], gt[3]

    # Coordinate transform EPSG:2193 → WGS84
    src = osr.SpatialReference()
    src.ImportFromEPSG(2193)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)

    # Collect sources from band 1 only (same DstRect repeated per band)
    band1 = root.find(".//VRTRasterBand[@band='1']")
    features = []
    for source in band1.findall("ComplexSource"):
        filename = source.find("SourceFilename").text
        dst_rect = source.find("DstRect")
        x_off = float(dst_rect.get("xOff"))
        y_off = float(dst_rect.get("yOff"))
        x_size = float(dst_rect.get("xSize"))
        y_size = float(dst_rect.get("ySize"))

        # EPSG:2193 bounds
        tile_min_x = min_x + x_off * res
        tile_max_y = max_y - y_off * res
        tile_max_x = tile_min_x + x_size * res
        tile_min_y = tile_max_y - y_size * res

        # Convert corners to WGS84
        def to_wgs84(x, y):
            lon, lat, _ = transform.TransformPoint(x, y)
            return [round(lon, 7), round(lat, 7)]

        coords = [
            to_wgs84(tile_min_x, tile_max_y),
            to_wgs84(tile_max_x, tile_max_y),
            to_wgs84(tile_max_x, tile_min_y),
            to_wgs84(tile_min_x, tile_min_y),
            to_wgs84(tile_min_x, tile_max_y),
        ]

        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {"file": filename.split("/")[-1]}
        })

    geojson = {"type": "FeatureCollection", "features": features}
    with open(OUTPUT, "w") as f:
        json.dump(geojson, f, separators=(",", ":"))

    import os
    size = os.path.getsize(OUTPUT)
    print(f"Done. {len(features)} features written to {OUTPUT} ({size / 1024 / 1024:.1f} MB)", flush=True)


if __name__ == "__main__":
    main()
