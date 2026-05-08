#!/usr/bin/env python3
"""
Build a GDAL VRT mosaic from COGs in S3.
Reads file headers in parallel then builds VRT XML directly.
Run once — store the output VRT in S3 for container startup download.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from osgeo import gdal, osr

BUCKET = "nz-imagery"
PREFIX = "auckland/auckland_2024_0.075m/rgb/2193/"
VSIS3_BASE = f"/vsis3/{BUCKET}/{PREFIX}"
OUTPUT = "/output/auckland_2024.vrt"
EPSG = 2193
WORKERS = 32

gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
gdal.SetConfigOption("VSI_CACHE", "TRUE")
gdal.SetConfigOption("VSI_CACHE_SIZE", "50000000")
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "TRUE")
gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif")


def list_tiffs():
    files = gdal.ReadDir(VSIS3_BASE)
    return [f"{VSIS3_BASE}{f}" for f in files if f.endswith(".tiff")]


def read_header(path):
    ds = gdal.Open(path)
    if ds is None:
        return None
    gt = ds.GetGeoTransform()
    width = ds.RasterXSize
    height = ds.RasterYSize
    bands = ds.RasterCount
    ds = None
    return {"path": path, "gt": gt, "width": width, "height": height, "bands": bands}


def build_vrt(tiles):
    min_x = min(t["gt"][0] for t in tiles)
    max_y = max(t["gt"][3] for t in tiles)
    max_x = max(t["gt"][0] + t["gt"][1] * t["width"] for t in tiles)
    min_y = min(t["gt"][3] + t["gt"][5] * t["height"] for t in tiles)

    res = tiles[0]["gt"][1]
    vrt_width = round((max_x - min_x) / res)
    vrt_height = round((max_y - min_y) / res)
    n_bands = min(t["bands"] for t in tiles)
    n_bands = min(n_bands, 3)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(EPSG)
    wkt = srs.ExportToWkt()

    bands_xml = ""
    for b in range(1, n_bands + 1):
        sources = ""
        for t in tiles:
            dst_x = round((t["gt"][0] - min_x) / res)
            dst_y = round((max_y - t["gt"][3]) / res)
            sources += f"""
      <ComplexSource>
        <SourceFilename relativeToVRT="0">{t["path"]}</SourceFilename>
        <SourceBand>{b}</SourceBand>
        <SourceProperties RasterXSize="{t["width"]}" RasterYSize="{t["height"]}" DataType="Byte"/>
        <SrcRect xOff="0" yOff="0" xSize="{t["width"]}" ySize="{t["height"]}"/>
        <DstRect xOff="{dst_x}" yOff="{dst_y}" xSize="{t["width"]}" ySize="{t["height"]}"/>
      </ComplexSource>"""
        bands_xml += f"""
  <VRTRasterBand dataType="Byte" band="{b}">{sources}
  </VRTRasterBand>"""

    return f"""<VRTDataset rasterXSize="{vrt_width}" rasterYSize="{vrt_height}">
  <SRS>{wkt}</SRS>
  <GeoTransform>{min_x}, {res}, 0, {max_y}, 0, -{res}</GeoTransform>{bands_xml}
</VRTDataset>"""


def main():
    print("Listing TIFF files...", flush=True)
    paths = list_tiffs()
    print(f"Found {len(paths)} files. Reading headers with {WORKERS} workers...", flush=True)

    tiles = []
    errors = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(read_header, p): p for p in paths}
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            if result:
                tiles.append(result)
            else:
                errors += 1
            if (i + 1) % 1000 == 0:
                print(f"  Read {i + 1}/{len(paths)}...", flush=True)

    print(f"Read {len(tiles)} headers ({errors} errors). Building VRT...", flush=True)

    vrt = build_vrt(tiles)
    with open(OUTPUT, "w") as f:
        f.write(vrt)

    size = os.path.getsize(OUTPUT)
    print(f"Done. VRT written to {OUTPUT} ({size / 1024 / 1024:.1f} MB)", flush=True)


if __name__ == "__main__":
    main()
