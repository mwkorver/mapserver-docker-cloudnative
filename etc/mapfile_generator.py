#!/usr/bin/env python3
"""
Generate /usr/src/mapfiles/mapfile.map from collections.json + env vars.

For each enabled collection in collections.json this emits three LAYER blocks:

  cog-extents-{id}     EPSG:3857 POLYGON for OGC API Features / WFS
  cog-tileindex-{id}   Native-CRS POLYGON, STATUS OFF (raster TILEINDEX source)
  {layer_name}         TYPE RASTER, TILEINDEX = cog-tileindex-{id}

Backend selection per collection:
  * DB_HOST + DB_USER set AND collection.postgis = true → POSTGIS connection
  * collection.parquet = true → OGR connection against staged GeoParquet
  * otherwise → OGR connection against bundled FlatGeobuf/GeoJSON files

Driven entirely by env + collections.json; no hand-edited mapfile needed.
"""
import json
import os
import sys
from pathlib import Path


COLLECTIONS_PATH = Path(os.environ.get("COLLECTIONS_FILE", "/usr/src/mapfiles/collections.json"))
OUTPUT_PATH = Path(os.environ.get("MAPFILE_OUTPUT", "/usr/src/mapfiles/mapfile.map"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost")
DEBUG_LEVEL = os.environ.get("MS_DEBUGLEVEL", "0")
GDAL_CACHEMAX = os.environ.get("GDAL_CACHEMAX", "128")
# Per-worker /vsicurl/ byte-range cache.  Disabled by default because the
# in-container nginx proxy_cache (4 GB on disk, shared across all FastCGI
# workers) already serves repeat byte-range reads — a second per-worker
# RAM cache is redundant and consumes (numprocs * VSI_CACHE_SIZE) of RAM
# that could go to GDAL_CACHEMAX instead.  Set VSI_CACHE=TRUE if running
# the image without the nginx cache layer in front.
VSI_CACHE = os.environ.get("VSI_CACHE", "FALSE")
VSI_CACHE_SIZE = os.environ.get("VSI_CACHE_SIZE", "33554432")

DEFAULT_EXTENT_3857 = [-14000000, 2500000, -7000000, 6000000]  # roughly continental US


def db_connection_string():
    """PostGIS connection string from env, or None if DB not configured.

    Accepts either DB_PASS or DB_PASSWORD. The CDK stack passes DB_PASSWORD
    (matching the AWS Secrets Manager schema), while local dev / earlier
    versions used DB_PASS — honor both to avoid silent unauthenticated
    connections.

    Intentional copy of admin_api.db_connection_string — both files run as
    separate processes. Keep in sync if connection-string logic changes.
    """
    host = os.environ.get("DB_HOST")
    user = os.environ.get("DB_USER")
    if not (host and user):
        return None
    return (
        f"host={host} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', 'mapserver')} "
        f"user={user} "
        f"password={os.environ.get('DB_PASS') or os.environ.get('DB_PASSWORD', '')}"
    )


def ogr_layer_name(path):
    """Return the layer name used as MapServer DATA value.

    For GeoJSON we read the FeatureCollection.name field if set, since
    OGR exposes it as the layer name. For binary OGR formats (FlatGeobuf
    etc.) the layer name is the file stem by convention — and trying to
    json.load() a binary file would crash with UnicodeDecodeError.
    """
    ext = Path(path).suffix.lower()
    if ext in (".geojson", ".json"):
        try:
            with open(path) as fh:
                doc = json.load(fh)
            return doc.get("name") or Path(path).stem
        except (OSError, json.JSONDecodeError):
            return Path(path).stem
    return Path(path).stem


def ogr_connection(path, parquet=False):
    """Return an explicit GDAL dataset name for plugin-backed formats."""
    return f"PARQUET:{path}" if parquet else path


def union_extent(collections, key, fallback):
    boxes = [c.get(key) for c in collections if c.get(key)]
    if not boxes:
        return list(fallback)
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def header(extent, srs_set):
    srs_str = " ".join(f"EPSG:{s}" for s in srs_set)
    lines = [
        'MAP',
        '  NAME "imagery"',
        '  STATUS ON',
        '  SIZE 256 256',
        f'  EXTENT {extent[0]} {extent[1]} {extent[2]} {extent[3]}',
        '  UNITS METERS',
        '  IMAGETYPE png24',
        '  IMAGECOLOR 255 255 255',
        '',
        '  PROJECTION',
        '    "init=epsg:3857"',
        '  END',
        '',
        '  OUTPUTFORMAT',
        '    NAME "png24"',
        '    DRIVER "AGG/PNG"',
        '    MIMETYPE "image/png"',
        # IMAGEMODE RGBA so MapServer can return transparent pixels where no
        # COG covers the request bbox.  With RGB the empty area would be
        # filled with IMAGECOLOR (white), hiding the OSM basemap underneath
        # the imagery layer in the viewer.
        '    IMAGEMODE RGBA',
        '    TRANSPARENT ON',
        '    EXTENSION "png"',
        '  END',
        '',
        '  OUTPUTFORMAT',
        '    NAME "geojson"',
        '    DRIVER "OGR/GEOJSON"',
        '    MIMETYPE "application/json; subtype=geojson"',
        '    FORMATOPTION "STORAGE=stream"',
        '    FORMATOPTION "FORM=SIMPLE"',
        '  END',
        '',
        '  WEB',
        '    METADATA',
        '      "ows_title"                      "Cloud-Native Imagery Server"',
        '      "ows_abstract"                   "Multi-collection COG service via MapServer"',
        f'      "ows_onlineresource"             "http://{PUBLIC_HOST}/mapserv"',
        f'      "ows_srs"                        "{srs_str}"',
        '      "ows_enable_request"             "*"',
        '      "oga_enable_request"             "*"',
        '      "wms_allow_getmap_without_styles" "true"',
        '    END',
        '  END',
        '',
        '  CONFIG "MS_ERRORFILE" "stderr"',
    ]
    if DEBUG_LEVEL not in ("", "0"):
        lines.append(f'  CONFIG "MS_DEBUGLEVEL" "{DEBUG_LEVEL}"')
        lines.append('  CONFIG "CPL_DEBUG" "ON"')
    # NOTE: GDAL_HTTP_MULTIPLEX / GDAL_HTTP_VERSION are intentionally NOT set.
    # GDAL's /vsicurl/ talks to the loopback nginx range-cache
    # (http://localhost:8001), which listens plaintext HTTP/1.1 — no `http2`
    # flag, h2c upgrade is declined, verified with `curl --http2`.  Those two
    # knobs would just negotiate back down to 1.1.  And on a loopback hop the
    # HTTP/2 multiplexing win (amortising connection setup) is ~zero anyway.
    # The latency-sensitive hop is signer→S3, governed by the signer's own
    # HTTP client, not by anything here.
    lines += [
        '  CONFIG "AWS_NO_SIGN_REQUEST" "YES"',
        f'  CONFIG "GDAL_CACHEMAX" "{GDAL_CACHEMAX}"',
        f'  CONFIG "VSI_CACHE" "{VSI_CACHE}"',
        f'  CONFIG "VSI_CACHE_SIZE" "{VSI_CACHE_SIZE}"',
        '  CONFIG "GDAL_DISABLE_READDIR_ON_OPEN" "TRUE"',
        '  CONFIG "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES" "YES"',
        '  CONFIG "CPL_VSIL_CURL_ALLOWED_EXTENSIONS" ".tif,.tiff,.parquet"',
    ]
    return lines


def footprints_layer(c, db_conn):
    """WFS/OGC-API Features polygon layer for COG footprints.

    PostGIS mode  → uses geom (EPSG:3857) from cog_index table.
    OGR/local mode → uses the native-CRS FlatGeobuf tileindex directly;
                     MapServer reprojects to the requested SRSNAME on the fly.
    """
    cid = c["id"]
    label = c["label"]
    use_postgis = bool(db_conn) and c.get("postgis", False)
    if use_postgis:
        extent = c.get("extent_3857") or DEFAULT_EXTENT_3857
        conn_block = [
            '    CONNECTIONTYPE POSTGIS',
            f'    CONNECTION "{db_conn}"',
            f'    DATA "geom FROM (SELECT id,file_name,location,geom FROM cog_index WHERE collection_id=\'{cid}\') AS sub USING UNIQUE id USING SRID=3857"',
        ]
        proj_epsg = 3857
    else:
        # Use the FGB tileindex (native CRS) — no separate footprints GeoJSON needed.
        tileindex = c.get("tileindex") or c.get("tileindex_geojson")
        layer_name = c.get("tileindex_layer_name") or ogr_layer_name(tileindex)
        extent = c.get("extent_native") or c.get("extent_3857") or DEFAULT_EXTENT_3857
        conn_block = [
            '    CONNECTIONTYPE OGR',
            f'    CONNECTION "{ogr_connection(tileindex, c.get("parquet", False))}"',
            f'    DATA "{layer_name}"',
        ]
        proj_epsg = c.get("native_epsg", 3857)
    return [
        '',
        '  LAYER',
        f'    NAME "cog-extents-{cid}"',
        '    TYPE POLYGON',
        '    STATUS ON',
        '    TEMPLATE "unused"',
        f'    EXTENT {extent[0]} {extent[1]} {extent[2]} {extent[3]}',
        *conn_block,
        '    PROJECTION',
        f'      "init=epsg:{proj_epsg}"',
        '    END',
        '    METADATA',
        f'      "ows_title"          "{label} footprints"',
        '      "ows_srs"            "EPSG:3857 EPSG:4326"',
        '      "gml_featureid"      "file_name"',
        '      "gml_geometries"     "msGeometry"',
        '      "gml_include_items"  "all"',
        '      "ows_enable_request"             "*"',
        '      "wfs_enable_request"             "*"',
        '      "oga_enable_request"             "*"',
        '      "wfs_getfeature_formatlist"      "geojson,gml3,gml2"',
        # Must exceed the largest collection's cog_count so a wide-bbox
        # query (e.g. world view) can return every feature in one go.
        '      "ows_maxfeatures"                "50000"',
        '    END',
        '  END',
    ]


def tileindex_groups(c):
    groups = c.get("tileindexes")
    if groups:
        return groups
    return [{
        "epsg": c["native_epsg"],
        "tileindex": c.get("tileindex") or c.get("tileindex_geojson"),
        "tileindex_layer_name": c.get("tileindex_layer_name"),
        "layer_name": c.get("layer_name") or c.get("id"),
        "extent_native": c.get("extent_native"),
    }]


def tileindex_map_layer_name(c, group):
    # Only suffix the internal polygon-layer name with EPSG when a single
    # collection has multiple source-CRS groups. Keeps the common
    # single-CRS case stable for any debugging that references the
    # cog-tileindex-* layer by name.
    if group is None:
        return f'cog-tileindex-{c["id"]}'
    if len(tileindex_groups(c)) > 1:
        return f'cog-tileindex-{c["id"]}-{group["epsg"]}'
    return f'cog-tileindex-{c["id"]}'


def tileindex_layer_for_group(c, db_conn, group):
    cid = c["id"]
    group = group or tileindex_groups(c)[0]
    epsg = group["epsg"]
    extent = group.get("extent_native") or c.get("extent_native") or [0, 0, 1000000, 1000000]
    use_postgis = bool(db_conn) and c.get("postgis", False)
    if use_postgis:
        # cog_index.geom_native is generic GEOMETRY; per-row SRID is set by
        # the scanner from collection.native_epsg. The USING SRID hint tells
        # MapServer what to declare to clients.
        conn_block = [
            '    CONNECTIONTYPE POSTGIS',
            f'    CONNECTION "{db_conn}"',
            f'    DATA "geom_native FROM (SELECT id,location,geom_native FROM cog_index WHERE collection_id=\'{cid}\') AS sub USING UNIQUE id USING SRID={epsg}"',
        ]
    else:
        tileindex = group.get("tileindex") or c.get("tileindex") or c.get("tileindex_geojson")
        layer_name = group.get("tileindex_layer_name") or c.get("tileindex_layer_name") or ogr_layer_name(tileindex)
        conn_block = [
            '    CONNECTIONTYPE OGR',
            f'    CONNECTION "{ogr_connection(tileindex, c.get("parquet", False))}"',
            f'    DATA "{layer_name}"',
        ]
    return [
        '',
        '  LAYER',
        f'    NAME "{tileindex_map_layer_name(c, group)}"',
        '    TYPE POLYGON',
        '    STATUS OFF',
        f'    EXTENT {extent[0]} {extent[1]} {extent[2]} {extent[3]}',
        *conn_block,
        '    PROJECTION',
        f'      "init=epsg:{epsg}"',
        '    END',
        '  END',
    ]


def raster_layer_for_group(c, group):
    processing = c.get("raster_processing") or [
        "BANDS=1,2,3",
        "RESAMPLE=AVERAGE",
    ]
    processing_lines = [f'    PROCESSING "{item}"' for item in processing]
    layer_name = group.get("layer_name") or c.get("layer_name") or c["id"]
    lines = [
        '',
        '  LAYER',
        f'    NAME "{layer_name}"',
        '    TYPE RASTER',
        '    STATUS ON',
        f'    TILEINDEX "{tileindex_map_layer_name(c, group)}"',
        f'    TILEITEM "{group.get("tileitem", "location")}"',
    ]
    if c.get("group"):
        lines.append(f'    GROUP "{c["group"]}"')
    if group.get("tilesrs"):
        lines.append(f'    TILESRS "{group["tilesrs"]}"')
    lines += [
        *processing_lines,
        '    PROJECTION',
        '      AUTO' if group.get("mixed_srs") else f'      "init=epsg:{group["epsg"]}"',
        '    END',
        '    METADATA',
        f'      "ows_title"       "{c["label"]}"',
        f'      "ows_attribution" "{c.get("attribution", "")}"',
        '    END',
        '  END',
    ]
    return lines


def main():
    if not COLLECTIONS_PATH.exists():
        sys.exit(f"ERROR: collections file not found: {COLLECTIONS_PATH}")

    doc = json.loads(COLLECTIONS_PATH.read_text())
    enabled = [c for c in doc.get("collections", []) if c.get("enabled", True)]
    enabled.sort(key=lambda c: c.get("draw_order", 10))

    db_conn = db_connection_string()
    backend = os.environ.get("DB_BACKEND") or ("postgis" if db_conn else "ogr")

    extent = union_extent(enabled, "extent_3857", DEFAULT_EXTENT_3857)
    native_srs = {group["epsg"] for c in enabled for group in tileindex_groups(c)}
    srs_set = sorted({3857, 4326} | native_srs)

    lines = header(extent, srs_set)
    for c in enabled:
        lines += footprints_layer(c, db_conn)
        for group in tileindex_groups(c):
            lines += tileindex_layer_for_group(c, db_conn, group)
            lines += raster_layer_for_group(c, group)
    lines += ['', 'END', '']

    OUTPUT_PATH.write_text("\n".join(lines))
    print(
        f"mapfile_generator: wrote {OUTPUT_PATH} "
        f"({len(enabled)} collections, backend={backend})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
