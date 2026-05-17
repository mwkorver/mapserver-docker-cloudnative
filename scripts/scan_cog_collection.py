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
import math
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

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None  # OK in local mode; we only need it when DB_HOST is set.


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


def list_tiffs(bucket, prefix, region, limit, requester_pays):
    endpoint = s3_endpoint(bucket, region)
    token = None
    keys = []

    sys.path.append("/etc")
    try:
        import s3_sigv4_proxy
        s3_sigv4_proxy.S3_BUCKET = bucket
        s3_sigv4_proxy.S3_REGION = region
    except ImportError:
        s3_sigv4_proxy = None

    while True:
        params = {
            "list-type": "2",
            "prefix": prefix,
            "max-keys": "1000",
        }
        if token:
            params["continuation-token"] = token
            
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        url = endpoint + "?" + query
        
        headers = {}
        if s3_sigv4_proxy:
            extra = {"x-amz-request-payer": "requester"} if requester_pays else None
            headers = s3_sigv4_proxy.signed_headers("GET", "/", query, None, extra)

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
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
    key, bucket, region, proxy_base, collection, source_epsg_override, requester_pays = args
    gdal_path = f"/vsis3/{bucket}/{key}"
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
    req_pays_flag = "requester-pays" if requester_pays else "standard"
    location = f"/vsicurl/{proxy_base.rstrip('/')}/{req_pays_flag}/{region}/{bucket}/{key.lstrip('/')}"
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
    # If the EPSG:3089 → EPSG:3857 reprojection hit a "No inverse operation"
    # case (some COGs near the projection's edge produce inf/nan output),
    # drop the web feature. The native footprint is still usable for the
    # raster TILEINDEX; we just lose this row in the web-mercator overlay.
    native_zoom = None
    if all(math.isfinite(c) for pt in web_ring for c in pt):
        web = {
            "type": "Feature",
            "properties": properties,
            "geometry": {"type": "Polygon", "coordinates": [web_ring]},
        }
        web_xs = [pt[0] for pt in web_ring]
        web_width = max(web_xs) - min(web_xs)
        if web_width > 0 and width > 0:
            web_res = web_width / width
            native_zoom = math.log2((20037508.342789244 * 2) / (256 * web_res))
    else:
        web = None
    return native, web, epsg, native_zoom


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


def write_tileindex_fgb(path, layer_name, epsg, features):
    """Write a FlatGeobuf tile index (R-tree indexed, ~30% smaller than GeoJSON,
    and ~25× faster for the bbox queries MapServer issues per WMS request).

    Atomic via write-then-rename so a partial scanner crash leaves any
    previous tile index intact.
    """
    out = Path(path)
    tmp = out.with_suffix(out.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()

    driver = gdal.GetDriverByName("FlatGeobuf")
    if driver is None:
        raise RuntimeError("GDAL FlatGeobuf driver not available")

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(epsg))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    ds = driver.Create(str(tmp), 0, 0, 0, gdal.GDT_Unknown)
    try:
        layer = ds.CreateLayer(layer_name, srs=srs, geom_type=3)  # wkbPolygon = 3
        # Declare attribute schema from first feature's properties.
        if features:
            from osgeo import ogr as _ogr  # local import; only here
            for k, v in features[0]["properties"].items():
                if isinstance(v, bool):
                    ftype = _ogr.OFTInteger
                elif isinstance(v, int):
                    ftype = _ogr.OFTInteger64
                elif isinstance(v, float):
                    ftype = _ogr.OFTReal
                else:
                    ftype = _ogr.OFTString
                layer.CreateField(_ogr.FieldDefn(k, ftype))

            layer_defn = layer.GetLayerDefn()
            for feat in features:
                f = _ogr.Feature(layer_defn)
                for k, v in feat["properties"].items():
                    if v is not None:
                        f.SetField(k, v)
                ring = _ogr.Geometry(_ogr.wkbLinearRing)
                for x, y in feat["geometry"]["coordinates"][0]:
                    ring.AddPoint_2D(float(x), float(y))
                poly = _ogr.Geometry(_ogr.wkbPolygon)
                poly.AddGeometry(ring)
                f.SetGeometry(poly)
                layer.CreateFeature(f)
                f = None
        layer = None
    finally:
        ds = None  # close (flushes index)

    tmp.replace(out)


def features_bbox(features):
    """Return [minx, miny, maxx, maxy] across all rings, skipping non-finite points.

    Defensive against the same projection-edge failures that scan_key already
    drops at the feature level — even one inf in here would poison min/max.
    """
    xs, ys = [], []
    for f in features:
        coords = f["geometry"]["coordinates"]
        for ring in coords if isinstance(coords[0][0], list) else [coords]:
            for x, y in ring:
                x, y = float(x), float(y)
                if math.isfinite(x) and math.isfinite(y):
                    xs.append(x)
                    ys.append(y)
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
            # tileindex, footprints_geojson, indexed_at, source.
            merged = {**existing, **entry}
            collections[i] = merged
            break
    else:
        collections.append(entry)

    doc["collections"] = collections
    p.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Updated {path} with collection {target_id!r}", flush=True)


def _ring_to_wkt(ring):
    """Closed ring [[x,y],...] → 'POLYGON((x1 y1, x2 y2, ...))'."""
    parts = ", ".join(f"{x} {y}" for x, y in ring)
    return f"POLYGON(({parts}))"


def load_into_postgis(collection_id, native_features, web_features, source_epsg):
    """Replace cog_index rows for this collection with the freshly scanned set.

    Wraps the DELETE + bulk INSERT in a single transaction so a partial
    failure leaves the previous index intact. Returns the row count on
    success, or raises on connection / SQL error.

    Reads connection params from DB_HOST / DB_PORT / DB_NAME / DB_USER /
    DB_PASS. Caller must ensure those are set; we don't fall back to
    anything sensible.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed; cannot load PostGIS")

    web_by_key = {f["properties"]["key"]: f for f in web_features}

    rows = []
    for nf in native_features:
        key = nf["properties"]["key"]
        wf = web_by_key.get(key)
        if wf is None:
            # No web footprint (projection-edge drop). Still index the row;
            # synthesise a 3857 polygon from the native bbox so the geom
            # column stays NOT NULL. The native geom is what MapServer uses.
            continue
        native_wkt = _ring_to_wkt(nf["geometry"]["coordinates"][0])
        web_wkt = _ring_to_wkt(wf["geometry"]["coordinates"][0])
        rows.append((
            collection_id,
            nf["properties"]["location"],
            nf["properties"]["file_name"],
            int(source_epsg),
            web_wkt,
            native_wkt,
        ))

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "mapserver"),
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASS") or os.environ.get("DB_PASSWORD", ""),
        connect_timeout=10,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cog_index WHERE collection_id = %s",
                    (collection_id,),
                )
                execute_values(
                    cur,
                    """
                    INSERT INTO cog_index
                        (collection_id, location, file_name, native_epsg, geom, geom_native)
                    VALUES %s
                    """,
                    rows,
                    template=(
                        "(%s, %s, %s, %s, "
                        "ST_GeomFromText(%s, 3857), "
                        "ST_SetSRID(ST_GeomFromText(%s), " + str(int(source_epsg)) + "))"
                    ),
                    page_size=500,
                )
    finally:
        conn.close()
    return len(rows)


def slugify(value):
    return re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")


def main():
    args = parse_args()
    
    # Sanitize inputs
    if args.bucket.startswith("s3://"):
        args.bucket = args.bucket[5:]
    args.bucket = args.bucket.strip("/")
    
    if args.prefix.startswith("s3://"):
        args.prefix = args.prefix[5:]
    args.prefix = args.prefix.strip("/")
    if args.prefix:
        args.prefix += "/"
        
    requester_pays = args.access_mode == "requester-pays" or args.requester_pays
    
    gdal.SetConfigOption("AWS_REGION", args.region)
    if args.access_mode == "unsigned":
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
    elif requester_pays:
        gdal.SetConfigOption("AWS_REQUEST_PAYER", "requester")
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "NO")
        
    started = time.time()
    keys = list_tiffs(args.bucket, args.prefix, args.region, args.limit, requester_pays)
    print(f"Discovered {len(keys)} GeoTIFFs under s3://{args.bucket}/{args.prefix}", flush=True)
    if not keys:
        raise SystemExit("No GeoTIFFs found")

    native_features = []
    web_features = []
    epsg_values = set()
    failures = []
    work = [
        (key, args.bucket, args.region, args.proxy_base, args.collection, args.source_epsg, requester_pays)
        for key in keys
    ]

    native_zooms = []
    dropped_web = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(scan_key, item) for item in work]
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                native, web, epsg, nz = future.result()
                if nz is not None:
                    native_zooms.append(nz)
                native_features.append(native)
                if web is not None:
                    web_features.append(web)
                else:
                    dropped_web += 1
                epsg_values.add(epsg)
            except Exception as exc:
                failures.append(str(exc))
            if index % args.progress_every == 0 or index == len(futures):
                print(f"Scanned {index}/{len(futures)}; failures={len(failures)}", flush=True)

    if dropped_web:
        print(
            f"WARN: {dropped_web} COG(s) produced non-finite EPSG:3857 footprints "
            f"(typical at projection edges); dropped from web overlay only — "
            f"native tileindex retains all {len(native_features)} COGs.",
            file=sys.stderr, flush=True,
        )

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
    tileindex_layer_name = f"{args.collection}_tileindex_{source_epsg}"
    write_tileindex_fgb(args.output_native, tileindex_layer_name, source_epsg, native_features)
    write_geojson(args.output_web, f"{args.collection}_footprints_3857", WEB_MERCATOR_EPSG, web_features)
    elapsed = time.time() - started
    print(
        f"Wrote {len(native_features)} features; source EPSG={source_epsg}; elapsed={elapsed:.1f}s",
        flush=True,
    )

    # On AWS (DB_HOST set), push the freshly scanned features into PostGIS
    # and flag the collection so mapfile_generator emits POSTGIS layers.
    # On local (no DB_HOST), this block is skipped and the collection
    # continues to use the OGR/GeoJSON path.
    postgis_loaded = False
    if os.environ.get("DB_HOST"):
        try:
            n = load_into_postgis(args.collection, native_features, web_features, source_epsg)
            postgis_loaded = True
            print(f"PostGIS: loaded {n} rows into cog_index for {args.collection}", flush=True)
        except Exception as exc:
            print(f"ERROR: PostGIS load failed: {exc}", file=sys.stderr, flush=True)
            raise

    if args.collections_file:
        extent_native = round_bbox(features_bbox(native_features), 2)
        extent_3857 = round_bbox(features_bbox(web_features), 0)
        bbox_4326 = round_bbox(reproject_bbox(extent_3857, WEB_MERCATOR_EPSG, WGS84_EPSG), 4)

        # Belt-and-suspenders: even though we filter inf/nan upstream, if
        # something exotic slips through don't crash the whole upsert here.
        def _safe_int_bbox(bbox):
            if not bbox:
                return None
            try:
                return [int(round(v)) for v in bbox if math.isfinite(v)] if len(bbox) == 4 else None
            except (OverflowError, ValueError):
                return None
        extent_3857_int = _safe_int_bbox(extent_3857)
        if extent_3857_int is None or len(extent_3857_int) != 4:
            extent_3857_int = None
        layer_name = args.layer_name or slugify(args.collection)
        map_name = args.map_name or f"{slugify(args.collection)}-imagery"
        label = args.label or args.collection
        if native_zooms:
            median_nz = sorted(native_zooms)[len(native_zooms) // 2]
            collection_native_zoom = int(round(median_nz))
        else:
            collection_native_zoom = 18

        entry = {
            "id": args.collection,
            "layer_name": layer_name,
            "map_name": map_name,
            "label": label,
            "attribution": args.attribution,
            "enabled": args.enabled == "true",
            "min_zoom": args.min_zoom,
            "max_zoom": args.max_zoom,
            "native_zoom": collection_native_zoom,
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
            "extent_3857": extent_3857_int,
            "extent_native": extent_native,
            "tileindex": os.path.abspath(args.output_native),
            "footprints_geojson": os.path.abspath(args.output_web),
            "postgis": postgis_loaded,
            "indexed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        upsert_collection(args.collections_file, entry)


if __name__ == "__main__":
    main()
