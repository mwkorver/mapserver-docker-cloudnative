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
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from osgeo import gdal, osr

# Mark all /vsicurl/ traffic from this script with X-Scanner so the
# in-container nginx range-cache (mapserver_proxy.conf) skips both
# reading from and writing to its cache for our requests.  Scanning has
# no spatial/temporal locality — one header read per file, then move on
# — and would otherwise blow the serving cache out of its LRU window.
# SigV4 signing via the local signer still applies.
os.environ.setdefault("GDAL_HTTP_HEADERS", "X-Scanner: 1")

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None         # OK in local mode; we only need it when DB_HOST is set.
    execute_values = None   # Keeps the module attribute consistent for testing.


NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
WEB_MERCATOR_EPSG = 3857
WGS84_EPSG = 4326


def load_signer_credentials(bucket, region):
    """Return credentials from the shared SigV4 helper, or None.

    The scanner lists signed buckets through the same helper used by the
    nginx proxy, but GDAL /vsis3 does not know about that helper or the
    container's /tmp/aws_credentials.json file. For signed/requester-pays
    scans, copy the resolved temporary credentials into GDAL config options
    before worker threads call gdal.OpenEx().
    """
    sys.path.append("/etc")
    try:
        import s3_sigv4_proxy
    except ImportError:
        return None

    s3_sigv4_proxy.S3_BUCKET = bucket
    s3_sigv4_proxy.S3_REGION = region
    return s3_sigv4_proxy.credentials()


def configure_gdal_s3_credentials(access_mode, requester_pays, bucket, region):
    gdal.SetConfigOption("AWS_REGION", region)
    if access_mode == "unsigned":
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        return

    if requester_pays:
        gdal.SetConfigOption("AWS_REQUEST_PAYER", "requester")

    creds = load_signer_credentials(bucket, region)
    if not creds:
        raise RuntimeError(
            "Signed S3 scan requested, but no AWS credentials are available. "
            "Run scripts/auto_refresh_credentials.sh or set AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN in the container."
        )

    gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "NO")
    gdal.SetConfigOption("AWS_ACCESS_KEY_ID", creds["access_key"])
    gdal.SetConfigOption("AWS_SECRET_ACCESS_KEY", creds["secret_key"])
    if creds.get("token"):
        gdal.SetConfigOption("AWS_SESSION_TOKEN", creds["token"])


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
    parser.add_argument("--min-zoom", type=int, default=None,
                        help="Minimum viewer zoom; default is detected native_zoom - 4")
    parser.add_argument("--max-zoom", type=int, default=None,
                        help="Maximum viewer zoom; default is detected native_zoom + 1")
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


def list_tiffs(bucket, prefix, region, limit, access_mode, requester_pays):
    endpoint = s3_endpoint(bucket, region)
    token = None
    keys = []

    s3_sigv4_proxy = None
    should_sign = access_mode in ("signed", "requester-pays")
    if should_sign:
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
        if should_sign and s3_sigv4_proxy:
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

    if out.exists():
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()
    if tmp.is_dir():
        inner_files = list(tmp.glob("*.fgb"))
        if len(inner_files) != 1:
            raise RuntimeError(f"Expected one FlatGeobuf file in {tmp}, found {len(inner_files)}")
        inner_files[0].replace(out)
        shutil.rmtree(tmp)
    else:
        tmp.replace(out)


def feature_with_geometry(feature, geometry):
    copied = {
        "type": "Feature",
        "properties": dict(feature["properties"]),
        "geometry": geometry,
    }
    return copied


def group_features_by_epsg(native_features, web_features):
    native_by_epsg = {}
    web_by_epsg = {}
    for feature in native_features:
        epsg = feature["properties"].get("epsg")
        native_by_epsg.setdefault(epsg, []).append(feature)
    for feature in web_features:
        epsg = feature["properties"].get("epsg")
        web_by_epsg.setdefault(epsg, []).append(feature)
    return native_by_epsg, web_by_epsg


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
        doc = {
            "type": "Catalog",
            "stac_version": "1.0.0",
            "id": "mapserver-catalog",
            "title": "Cloud-Native MapServer Catalog",
            "description": "Catalog of COG collections served by MapServer",
            "version": COLLECTIONS_SCHEMA_VERSION,
            "collections": []
        }
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


def load_into_postgis(collection_id, native_features, web_features, source_epsg, delete_first=True):
    """Bulk-insert one EPSG group's features into cog_index.

    ``source_epsg`` is used as the SRID for every row's geom_native; callers
    must pass only features whose native CRS matches that EPSG.  For
    multi-EPSG collections, call once per group with ``delete_first=True``
    only on the first group so a single DELETE covers the whole collection
    before inserting.

    Wraps the optional DELETE + INSERT in a single transaction so a partial
    failure leaves the previous index intact.  Returns the row count on
    success, or raises on connection / SQL error.

    Reads connection params from DB_HOST / DB_PORT / DB_NAME / DB_USER /
    DB_PASS.  Caller must ensure those are set; we don't fall back silently.
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed; cannot load PostGIS")

    web_by_key = {f["properties"]["key"]: f for f in web_features}

    rows = []
    for nf in native_features:
        key = nf["properties"]["key"]
        wf = web_by_key.get(key)
        if wf is None:
            # No web footprint (projection-edge drop). Native geom is still
            # usable for the raster TILEINDEX; skip only this web column.
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
                if delete_first:
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
    
    configure_gdal_s3_credentials(
        args.access_mode,
        requester_pays,
        args.bucket,
        args.region,
    )
        
    started = time.time()
    keys = list_tiffs(args.bucket, args.prefix, args.region, args.limit, args.access_mode, requester_pays)
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

    native_features.sort(key=lambda feature: feature["properties"]["key"])
    web_features.sort(key=lambda feature: feature["properties"]["key"])
    native_by_epsg, web_by_epsg = group_features_by_epsg(native_features, web_features)
    source_epsgs = sorted(epsg for epsg in native_by_epsg if epsg is not None)
    if not source_epsgs:
        raise SystemExit("No source EPSG values detected; pass --source-epsg <code> or inspect source CRS metadata.")

    output_native = Path(args.output_native)
    tileindexes = []
    # Only suffix the WMS-visible LAYER name with EPSG when a single
    # collection straddles multiple source CRSs. The common single-CRS case
    # keeps the friendly name unchanged so external clients with hardcoded
    # LAYERS=<id> URLs don't break.
    multi_epsg = len(source_epsgs) > 1
    base_layer_name = args.layer_name or slugify(args.collection)
    for source_epsg in source_epsgs:
        grouped_features = sorted(native_by_epsg[source_epsg], key=lambda feature: feature["properties"]["key"])
        if len(source_epsgs) == 1:
            tileindex_path = output_native
        else:
            tileindex_path = output_native.with_name(f"{output_native.stem}_{source_epsg}{output_native.suffix}")
        tileindex_layer_name = f"{args.collection}_tileindex_{source_epsg}"
        write_tileindex_fgb(tileindex_path, tileindex_layer_name, source_epsg, grouped_features)
        tileindexes.append({
            "epsg": int(source_epsg),
            "tileindex": os.path.abspath(tileindex_path),
            "tileindex_layer_name": tileindex_layer_name,
            "layer_name": f"{base_layer_name}-{source_epsg}" if multi_epsg else base_layer_name,
            "extent_native": round_bbox(features_bbox(grouped_features), 2),
            "cog_count": len(grouped_features),
        })

    elapsed = time.time() - started
    print(
        f"Wrote {len(native_features)} features across {len(source_epsgs)} source EPSG group(s): "
        f"{', '.join(str(epsg) for epsg in source_epsgs)}; elapsed={elapsed:.1f}s",
        flush=True,
    )

    # On AWS (DB_HOST set), push the freshly scanned features into PostGIS
    # and flag the collection so mapfile_generator emits POSTGIS layers.
    # On local (no DB_HOST), this block is skipped and the collection
    # continues to use the OGR/FlatGeobuf path.
    #
    # Multi-EPSG: DELETE once (first group) then INSERT per group so each
    # group uses the correct native SRID. Passing all features to a single
    # call with source_epsgs[0] would stamp the wrong SRID on secondary groups.
    postgis_loaded = False
    if os.environ.get("DB_HOST"):
        try:
            total_rows = 0
            for i, source_epsg in enumerate(source_epsgs):
                n = load_into_postgis(
                    args.collection,
                    native_by_epsg[source_epsg],
                    web_by_epsg.get(source_epsg, []),
                    source_epsg,
                    delete_first=(i == 0),
                )
                total_rows += n
            postgis_loaded = True
            print(f"PostGIS: loaded {total_rows} rows into cog_index for {args.collection}", flush=True)
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
        collection_min_zoom = args.min_zoom if args.min_zoom is not None else collection_native_zoom - 4
        collection_max_zoom = args.max_zoom if args.max_zoom is not None else collection_native_zoom + 1

        indexed_time = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        entry = {
            # --- STAC Collection Core Fields ---
            "type": "Collection",
            "stac_version": "1.0.0",
            "id": args.collection,
            "title": label,
            "description": f"COG collection '{args.collection}' scanned from S3",
            "license": "various",
            "extent": {
                "spatial": {
                    "bbox": [bbox_4326] if bbox_4326 else []
                },
                "temporal": {
                    "interval": [[indexed_time, indexed_time]]
                }
            },
            "links": [
                {
                    "rel": "self",
                    "href": f"./{args.collection}.json",
                    "type": "application/json"
                }
            ],
            "providers": [
                {
                    "name": args.attribution or "Unknown",
                    "roles": ["producer", "host"]
                }
            ] if args.attribution else [],

            # --- MapServer / Custom Pipeline Compatibility Fields ---
            "layer_name": layer_name,
            "layer_names": [item["layer_name"] for item in tileindexes],
            "map_name": map_name,
            "label": label,
            "attribution": args.attribution,
            "enabled": args.enabled == "true",
            "min_zoom": collection_min_zoom,
            "max_zoom": collection_max_zoom,
            "native_zoom": collection_native_zoom,
            "draw_order": args.draw_order,
            "source": {
                "bucket": args.bucket,
                "prefix": args.prefix,
                "region": args.region,
                "access_mode": args.access_mode,
                "requester_pays": bool(args.requester_pays),
            },
            "native_epsg": int(source_epsgs[0]),
            "native_epsgs": [int(epsg) for epsg in source_epsgs],
            "cog_count": len(native_features),
            "bbox_4326": bbox_4326,
            "extent_3857": extent_3857_int,
            "extent_native": extent_native,
            "tileindex": tileindexes[0]["tileindex"],
            "tileindex_layer_name": tileindexes[0]["tileindex_layer_name"],
            "tileindexes": tileindexes,
            "postgis": postgis_loaded,
            "indexed_at": indexed_time,
        }
        upsert_collection(args.collections_file, entry)


if __name__ == "__main__":
    main()
