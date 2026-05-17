#!/usr/bin/env python3
"""
Generate /usr/src/mapfiles/mapfile.map from collections.json + env vars.

For each enabled collection in collections.json this emits three LAYER blocks:

  cog-extents-{id}     EPSG:3857 POLYGON for OGC API Features / WFS
  cog-tileindex-{id}   Native-CRS POLYGON, STATUS OFF (raster TILEINDEX source)
  {layer_name}         TYPE RASTER, TILEINDEX = cog-tileindex-{id}

Backend selection per collection:
  * DB_HOST + DB_USER set AND collection.postgis = true → POSTGIS connection
  * otherwise → OGR connection against the bundled GeoJSON files

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
VSI_CACHE_SIZE = os.environ.get("VSI_CACHE_SIZE", "33554432")

DEFAULT_EXTENT_3857 = [-14000000, 2500000, -7000000, 6000000]  # roughly continental US


def db_connection_string():
    """PostGIS connection string from env, or None if DB not configured."""
    host = os.environ.get("DB_HOST")
    user = os.environ.get("DB_USER")
    if not (host and user):
        return None
    return (
        f"host={host} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ.get('DB_NAME', 'mapserver')} "
        f"user={user} "
        f"password={os.environ.get('DB_PASS', '')}"
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
        '    NAME png24',
        '    DRIVER "AGG/PNG"',
        '    MIMETYPE "image/png"',
        '    IMAGEMODE RGB',
        '    EXTENSION "png"',
        '  END',
        '',
        '  WEB',
        '    METADATA',
        '      "ows_title"                      "Cloud-Native Imagery Server"',
        '      "ows_abstract"                   "Multi-collection COG service via MapServer"',
        f'      "ows_onlineresource"             "http://{PUBLIC_HOST}/mapserv"',
        f'      "ows_srs"                        "{srs_str}"',
        '      "ows_enable_request"             "*"',
        '      "F_enable_request"               "*"',
        '      "wms_allow_getmap_without_styles" "true"',
        '    END',
        '  END',
        '',
        '  CONFIG "MS_ERRORFILE" "stderr"',
    ]
    if DEBUG_LEVEL not in ("", "0"):
        lines.append(f'  CONFIG "MS_DEBUGLEVEL" "{DEBUG_LEVEL}"')
        lines.append('  CONFIG "CPL_DEBUG" "ON"')
    lines += [
        '  CONFIG "AWS_NO_SIGN_REQUEST" "YES"',
        f'  CONFIG "GDAL_CACHEMAX" "{GDAL_CACHEMAX}"',
        '  CONFIG "VSI_CACHE" "TRUE"',
        f'  CONFIG "VSI_CACHE_SIZE" "{VSI_CACHE_SIZE}"',
        '  CONFIG "GDAL_DISABLE_READDIR_ON_OPEN" "TRUE"',
        '  CONFIG "GDAL_HTTP_MULTIPLEX" "YES"',
        '  CONFIG "GDAL_HTTP_VERSION" "2"',
        '  CONFIG "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES" "YES"',
        '  CONFIG "CPL_VSIL_CURL_ALLOWED_EXTENSIONS" ".tif,.tiff"',
    ]
    return lines


def footprints_layer(c, db_conn):
    cid = c["id"]
    label = c["label"]
    extent = c.get("extent_3857") or DEFAULT_EXTENT_3857
    use_postgis = bool(db_conn) and c.get("postgis", False)
    if use_postgis:
        conn_block = [
            '    CONNECTIONTYPE POSTGIS',
            f'    CONNECTION "{db_conn}"',
            f'    DATA "geom FROM (SELECT id,file_name,location,geom FROM cog_index WHERE collection_id=\'{cid}\') AS sub USING UNIQUE id USING SRID=3857"',
        ]
    else:
        geojson = c["footprints_geojson"]
        layer_name = ogr_layer_name(geojson)
        conn_block = [
            '    CONNECTIONTYPE OGR',
            f'    CONNECTION "{geojson}"',
            f'    DATA "{layer_name}"',
        ]
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
        '      "init=epsg:3857"',
        '    END',
        '    METADATA',
        f'      "ows_title"          "{label} footprints"',
        '      "ows_srs"            "EPSG:3857 EPSG:4326"',
        '      "gml_featureid"      "file_name"',
        '      "gml_geometries"     "msGeometry"',
        '      "gml_include_items"  "all"',
        '      "ows_enable_request" "*"',
        '      "wfs_enable_request" "*"',
        '      "F_enable_request"   "*"',
        '      "ows_maxfeatures"    "10000"',
        '    END',
        '  END',
    ]


def tileindex_layer(c, db_conn):
    cid = c["id"]
    epsg = c["native_epsg"]
    extent = c.get("extent_native") or [0, 0, 1000000, 1000000]
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
        # Tile index file (FGB preferred; legacy field name kept for
        # forward compatibility with collections.json files written by
        # older scanners).
        tileindex = c.get("tileindex") or c.get("tileindex_geojson")
        layer_name = ogr_layer_name(tileindex)
        conn_block = [
            '    CONNECTIONTYPE OGR',
            f'    CONNECTION "{tileindex}"',
            f'    DATA "{layer_name}"',
        ]
    return [
        '',
        '  LAYER',
        f'    NAME "cog-tileindex-{cid}"',
        '    TYPE POLYGON',
        '    STATUS OFF',
        f'    EXTENT {extent[0]} {extent[1]} {extent[2]} {extent[3]}',
        *conn_block,
        '    PROJECTION',
        f'      "init=epsg:{epsg}"',
        '    END',
        '  END',
    ]


def raster_layer(c):
    return [
        '',
        '  LAYER',
        f'    NAME "{c["layer_name"]}"',
        '    TYPE RASTER',
        '    STATUS ON',
        f'    TILEINDEX "cog-tileindex-{c["id"]}"',
        '    TILEITEM "location"',
        '    PROCESSING "BANDS=1,2,3"',
        '    PROCESSING "RESAMPLE=AVERAGE"',
        '    PROJECTION',
        f'      "init=epsg:{c["native_epsg"]}"',
        '    END',
        '    METADATA',
        f'      "ows_title"       "{c["label"]}"',
        f'      "ows_attribution" "{c.get("attribution", "")}"',
        '    END',
        '  END',
    ]


def main():
    if not COLLECTIONS_PATH.exists():
        sys.exit(f"ERROR: collections file not found: {COLLECTIONS_PATH}")

    doc = json.loads(COLLECTIONS_PATH.read_text())
    enabled = [c for c in doc.get("collections", []) if c.get("enabled", True)]
    enabled.sort(key=lambda c: c.get("draw_order", 10))

    db_conn = db_connection_string()
    backend = "postgis+ogr" if db_conn else "ogr-only"

    extent = union_extent(enabled, "extent_3857", DEFAULT_EXTENT_3857)
    srs_set = sorted({3857, 4326} | {c["native_epsg"] for c in enabled})

    lines = header(extent, srs_set)
    for c in enabled:
        lines += footprints_layer(c, db_conn)
        lines += tileindex_layer(c, db_conn)
        lines += raster_layer(c)
    lines += ['', 'END', '']

    OUTPUT_PATH.write_text("\n".join(lines))
    print(
        f"mapfile_generator: wrote {OUTPUT_PATH} "
        f"({len(enabled)} collections, backend={backend})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
