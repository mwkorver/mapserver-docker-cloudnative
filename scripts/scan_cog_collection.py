#!/usr/bin/env python3
"""
Scan a bucket/prefix containing COGs and write MapServer tile indexes.

The scanner is intentionally generic: it discovers GeoTIFF objects by S3
ListBucketV2 XML, opens each COG through GDAL /vsicurl, and writes both a
source-native tile index for MapServer raster TILEINDEX selection and a Web
Mercator footprint index for browser/admin display.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from osgeo import gdal, osr


NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
WEB_MERCATOR_EPSG = 3857
WGS84_EPSG = 4326
COLLECTIONS_SCHEMA_VERSION = 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--collection", required=True,
                        help="Stable collection id (kebab-case), e.g. ky-2024-3in")
    parser.add_argument("--proxy-base", default="http://localhost:8001")
    parser.add_argument("--output-native", required=True)
    parser.add_argument("--output-web", required=True)
    parser.add_argument("--source-epsg", type=int, default=0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=100)

    # collections.json upsert metadata (only used when --collections-file is set).
    parser.add_argument("--collections-file",
                        help="Path to collections.json; if set, the collection's "
                             "entry is created or updated after scan completes")
    parser.add_argument("--label", default=None,
                        help="Human-readable label (default: derived from --collection)")
    parser.add_argument("--attribution", default="",
                        help="Attribution string for the layer (e.g. data provider)")
    parser.add_argument("--layer-name", default=None,
                        help="MapServer LAYER name (default: derived from --collection)")
    parser.add_argument("--map-name", default=None,
                        help="MapServer MAP/alias name for OGC API (default: '<id>-imagery')")
    parser.add_argument("--min-zoom", type=int, default=14)
    parser.add_argument("--max-zoom", type=int, default=22)
    parser.add_argument("--draw-order", type=int, default=10)
    parser.add_argument("--enabled", default="true",
                        choices=["true", "false"])
    parser.add_argument("--access-mode", default="unsigned",
                        choices=["unsigned", "signed", "requester-pays"])
    parser.add_argument("--requester-pays", action="store_true",
                        help="Set requester_pays flag on the source record")
    return parser.parse_args()


def s3_endpoint(bucket, region):
    return f"https://{bucket}.s3.{region}.amazonaws.com/"


def list_tiffs(bucket, prefix, region, limit):
    endpoint = s3_endpoint(bucket, region)
    token = None
    keys = []

    while True:
        params = {
            "list-type": "2",
            "prefix": prefix,
            "max-keys": "1000",
        }
        if token:
            params["continuation-token"] = token
        url = endpoint + "?" + urllib.parse.urlencode(params)

        with urllib.request.urlopen(url, timeout=60) as response:
            root = ET.fromstring(response.read())

        for item in root.findall("s3:Contents", NS):
            key = item.findtext("s3:Key", default="", namespaces=NS)
            lower = key.lower()
            if lower.endswith((".tif", ".tiff")):
                keys.append(key)
                if limit and len(keys) >= limit:
                    return keys

        truncated = root.findtext("s3:IsTruncated", default="false", namespaces=NS) == "true"
        if not truncated:
            return keys
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)
        if not token:
            return keys


def polygon_from_bounds(min_x, min_y, max_x, max_y):
    return [
        [
            [min_x, min_y],
            [max_x, min_y],
            [max_x, max_y],
            [min_x, max_y],
            [min_x, min_y],
        ]
    ]


def transform_ring(ring, source_srs, target_srs):
    transform = osr.CoordinateTransformation(source_srs, target_srs)
    transformed = []
    for x, y in ring:
        tx, ty, _ = transform.TransformPoint(float(x), float(y))
        transformed.append([tx, ty])
    return transformed


def scan_key(args):
    key, bucket, region, proxy_base, collection, source_epsg_override = args
    s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{urllib.parse.quote(key)}"
    gdal_path = f"/vsicurl/{s3_url}"
    dataset = gdal.OpenEx(gdal_path, gdal.OF_RASTER)
    if dataset is None:
        raise RuntimeError(f"GDAL failed to open {gdal_path}")

    projection = dataset.GetProjection()
    source_srs = osr.SpatialReference()
    source_srs.ImportFromWkt(projection)
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    epsg = source_srs.GetAuthorityCode(None) or source_srs.GetAuthorityCode("PROJCS")
    # Some COGs have valid WKT but no AUTHORITY tag — AutoIdentifyEPSG
    # asks GDAL's EPSG database to match the WKT parameters back to an
    # EPSG code. Returns 0 on success (OGRERR_NONE).
    if epsg is None and source_srs.AutoIdentifyEPSG() == 0:
        epsg = source_srs.GetAuthorityCode(None) or source_srs.GetAuthorityCode("PROJCS")
    if epsg is not None:
        epsg = int(epsg)
    elif source_epsg_override:
        epsg = source_epsg_override

    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(WEB_MERCATOR_EPSG)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    gt = dataset.GetGeoTransform()
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    min_x = gt[0]
    max_y = gt[3]
    max_x = gt[0] + gt[1] * width + gt[2] * height
    min_y = gt[3] + gt[4] * width + gt[5] * height
    min_x, max_x = sorted((min_x, max_x))
    min_y, max_y = sorted((min_y, max_y))

    native_ring = polygon_from_bounds(min_x, min_y, max_x, max_y)[0]
    web_ring = transform_ring(native_ring, source_srs, target_srs)
    file_name = key.rsplit("/", 1)[-1]
    location = f"/vsicurl/{proxy_base.rstrip('/')}/{key.lstrip('/')}"
    bands = dataset.RasterCount
    data_type = gdal.GetDataTypeName(dataset.GetRasterBand(1).DataType) if bands else "unknown"
    dataset = None

    properties = {
        "collection": collection,
        "location": location,
        "bucket": bucket,
        "key": key,
        "file_name": file_name,
        "epsg": epsg,
        "width": width,
        "height": height,
        "bands": bands,
        "data_type": data_type,
    }
    native = {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Polygon", "coordinates": [native_ring]},
    }
    web = {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Polygon", "coordinates": [web_ring]},
    }
    return native, web, epsg


def write_geojson(path, name, epsg, features):
    payload = {
        "type": "FeatureCollection",
        "name": name,
        "crs": {
            "type": "name",
            "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"},
        },
        "features": features,
    }
    Path(path).write_text(json.dumps(payload, separators=(",", ":")) + "\n")


def features_bbox(features):
    """Return [minx, miny, maxx, maxy] across all rings."""
    xs, ys = [], []
    for f in features:
        coords = f["geometry"]["coordinates"]
        for ring in coords if isinstance(coords[0][0], list) else [coords]:
            for x, y in ring:
                xs.append(float(x))
                ys.append(float(y))
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def reproject_bbox(bbox, source_epsg, target_epsg):
    """Cheap bbox reprojection by transforming the 4 corners (good enough for metadata)."""
    if bbox is None:
        return None
    src = osr.SpatialReference()
    src.ImportFromEPSG(int(source_epsg))
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(int(target_epsg))
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)
    corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]),
               (bbox[2], bbox[3]), (bbox[0], bbox[3])]
    xs, ys = [], []
    for x, y in corners:
        tx, ty, _ = transform.TransformPoint(x, y)
        xs.append(tx)
        ys.append(ty)
    return [min(xs), min(ys), max(xs), max(ys)]


def round_bbox(bbox, places):
    if bbox is None:
        return None
    return [round(v, places) for v in bbox]


def upsert_collection(path, entry):
    """Insert or update an entry by id in a collections.json file."""
    p = Path(path)
    if p.exists():
        doc = json.loads(p.read_text())
        if doc.get("version") != COLLECTIONS_SCHEMA_VERSION:
            print(f"WARN: {path} has version {doc.get('version')}, expected {COLLECTIONS_SCHEMA_VERSION}",
                  file=sys.stderr)
        collections = doc.get("collections", [])
    else:
        doc = {"version": COLLECTIONS_SCHEMA_VERSION, "collections": []}
        collections = doc["collections"]

    target_id = entry["id"]
    for i, existing in enumerate(collections):
        if existing.get("id") == target_id:
            # Preserve user-edited fields (enabled, min_zoom, etc.) unless scanner
            # is the authority. Scanner owns: cog_count, bbox_*, extent_*, native_epsg,
            # tileindex_geojson, footprints_geojson, indexed_at, source.
            merged = {**existing, **entry}
            collections[i] = merged
            break
    else:
        collections.append(entry)

    doc["collections"] = collections
    p.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Updated {path} with collection {target_id!r}", flush=True)


def slugify(value):
    return re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")


def main():
    args = parse_args()
    started = time.time()
    keys = list_tiffs(args.bucket, args.prefix, args.region, args.limit)
    print(f"Discovered {len(keys)} GeoTIFFs under s3://{args.bucket}/{args.prefix}", flush=True)
    if not keys:
        raise SystemExit("No GeoTIFFs found")

    native_features = []
    web_features = []
    epsg_values = set()
    failures = []
    work = [
        (key, args.bucket, args.region, args.proxy_base, args.collection, args.source_epsg)
        for key in keys
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(scan_key, item) for item in work]
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                native, web, epsg = future.result()
                native_features.append(native)
                web_features.append(web)
                epsg_values.add(epsg)
            except Exception as exc:
                failures.append(str(exc))
            if index % args.progress_every == 0 or index == len(futures):
                print(f"Scanned {index}/{len(futures)}; failures={len(failures)}", flush=True)

    if failures:
        print("Failures:", file=sys.stderr)
        for failure in failures[:20]:
            print(f"  {failure}", file=sys.stderr)
        raise SystemExit(f"Failed to scan {len(failures)} COGs")

    none_count = sum(1 for f in native_features if f["properties"].get("epsg") is None)
    resolved = sorted({e for e in epsg_values if e is not None})

    if args.source_epsg and None in epsg_values:
        # Explicit override — patch unidentified features and accept.
        epsg_values.discard(None)
        for f in native_features:
            if f["properties"].get("epsg") is None:
                f["properties"]["epsg"] = args.source_epsg
        for f in web_features:
            if f["properties"].get("epsg") is None:
                f["properties"]["epsg"] = args.source_epsg
        if not epsg_values:
            epsg_values.add(args.source_epsg)

    elif len(resolved) == 1 and None in epsg_values:
        # Only one real EPSG present + some COGs that lacked an AUTHORITY
        # tag in their WKT. Geometric transformation already used the WKT,
        # so the missing label is purely cosmetic — fill it in.
        inferred = resolved[0]
        print(
            f"WARN: {none_count} COG(s) lacked an EPSG authority tag; "
            f"inferred EPSG:{inferred} from the {len(native_features) - none_count} "
            f"that did. Use --source-epsg to override.",
            file=sys.stderr,
            flush=True,
        )
        for f in native_features:
            if f["properties"].get("epsg") is None:
                f["properties"]["epsg"] = inferred
        for f in web_features:
            if f["properties"].get("epsg") is None:
                f["properties"]["epsg"] = inferred
        epsg_values = {inferred}

    if len(epsg_values) != 1:
        counts = {}
        for f in native_features:
            e = f["properties"].get("epsg")
            counts[e] = counts.get(e, 0) + 1
        breakdown = ", ".join(f"EPSG:{k or 'unknown'}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
        raise SystemExit(
            f"Expected one source EPSG but found multiple: {breakdown}. "
            f"Pass --source-epsg <code> to force a single value, or split into separate collections."
        )
    source_epsg = epsg_values.pop()

    native_features.sort(key=lambda feature: feature["properties"]["key"])
    web_features.sort(key=lambda feature: feature["properties"]["key"])
    write_geojson(args.output_native, f"{args.collection}_tileindex_{source_epsg}", source_epsg, native_features)
    write_geojson(args.output_web, f"{args.collection}_footprints_3857", WEB_MERCATOR_EPSG, web_features)
    elapsed = time.time() - started
    print(
        f"Wrote {len(native_features)} features; source EPSG={source_epsg}; elapsed={elapsed:.1f}s",
        flush=True,
    )

    if args.collections_file:
        extent_native = round_bbox(features_bbox(native_features), 2)
        extent_3857 = round_bbox(features_bbox(web_features), 0)
        bbox_4326 = round_bbox(reproject_bbox(extent_3857, WEB_MERCATOR_EPSG, WGS84_EPSG), 4)
        layer_name = args.layer_name or slugify(args.collection)
        map_name = args.map_name or f"{slugify(args.collection)}-imagery"
        label = args.label or args.collection
        entry = {
            "id": args.collection,
            "layer_name": layer_name,
            "map_name": map_name,
            "label": label,
            "attribution": args.attribution,
            "enabled": args.enabled == "true",
            "min_zoom": args.min_zoom,
            "max_zoom": args.max_zoom,
            "draw_order": args.draw_order,
            "source": {
                "bucket": args.bucket,
                "prefix": args.prefix,
                "region": args.region,
                "access_mode": args.access_mode,
                "requester_pays": bool(args.requester_pays),
            },
            "native_epsg": int(source_epsg),
            "cog_count": len(native_features),
            "bbox_4326": bbox_4326,
            "extent_3857": [int(v) for v in extent_3857] if extent_3857 else None,
            "extent_native": extent_native,
            "tileindex_geojson": os.path.abspath(args.output_native),
            "footprints_geojson": os.path.abspath(args.output_web),
            "postgis": False,
            "indexed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        upsert_collection(args.collections_file, entry)


if __name__ == "__main__":
    main()
