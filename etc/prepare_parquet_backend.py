#!/usr/bin/env python3
"""Stage selected GeoParquet indexes and generate collections.json.

PARQUET_SELECTION_JSON accepts either:

    {"tx": 2020, "ct": 2021}

or:

    [{"state": "tx", "year": 2020, "uri": "s3://.../data_0.parquet"}]

The source indexes contain WGS84 footprints plus source_bucket/source_key and
proj_epsg fields. Each staged index adds:

  location  /vsicurl URL routed through the local signed range-cache
  tile_srs  EPSG:<code>, consumed by MapServer TILESRS
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

try:
    from osgeo import gdal, osr
except ImportError:
    gdal = None
    osr = None


SELECTION_ENV = "PARQUET_SELECTION_JSON"
DEFAULT_URI_TEMPLATE = (
    "s3://cog-stac-viewer-495811053987-us-west-2/lake/"
    "collection=naip/region={state}/year={year}/data_0.parquet"
)
DEFAULT_OUTPUT = "/usr/src/mapfiles/collections.json"
DEFAULT_INDEX_DIR = "/usr/src/mapfiles/parquet"


def parse_selections(raw):
    if not raw:
        raise ValueError(f"{SELECTION_ENV} is required for the parquet backend")
    doc = json.loads(raw)
    if isinstance(doc, dict):
        entries = [{"state": state, "year": year} for state, year in doc.items()]
    elif isinstance(doc, list):
        entries = doc
    else:
        raise ValueError(f"{SELECTION_ENV} must be a JSON object or array")
    if not entries:
        raise ValueError(f"{SELECTION_ENV} must contain at least one state/year selection")

    normalized = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each parquet selection must be an object")
        state = str(entry.get("state", "")).strip().lower()
        year = entry.get("year")
        if not re.fullmatch(r"[a-z]{2}", state):
            raise ValueError(f"invalid state code: {state!r}")
        try:
            year = int(year)
        except (TypeError, ValueError):
            raise ValueError(f"invalid year for {state}: {year!r}") from None
        if not 1900 <= year <= 2100:
            raise ValueError(f"year out of range for {state}: {year}")
        key = (state, year)
        if key in seen:
            raise ValueError(f"duplicate parquet selection: {state}:{year}")
        seen.add(key)
        normalized.append({
            "state": state,
            "year": year,
            "uri": entry.get("uri"),
            "label": entry.get("label"),
        })
    return sorted(normalized, key=lambda item: (item["year"], item["state"]))


def split_s3_uri(uri):
    if not uri.startswith("s3://"):
        raise ValueError(f"parquet index URI must use s3://: {uri}")
    bucket, separator, key = uri[5:].partition("/")
    if not bucket or not separator or not key:
        raise ValueError(f"invalid S3 URI: {uri}")
    return bucket, key


def download_s3(uri, destination):
    import boto3

    bucket, key = split_s3_uri(uri)
    destination.parent.mkdir(parents=True, exist_ok=True)
    boto3.client("s3").download_file(bucket, key, str(destination))


def sql_quote(value):
    return str(value).replace("'", "''")


def sql_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def transform_extent_3857(extent):
    min_x, max_x, min_y, max_y = extent
    source = osr.SpatialReference()
    source.ImportFromEPSG(4326)
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = osr.SpatialReference()
    target.ImportFromEPSG(3857)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source, target)
    points = [
        transform.TransformPoint(x, y)
        for x, y in (
            (min_x, min_y),
            (min_x, max_y),
            (max_x, min_y),
            (max_x, max_y),
        )
    ]
    return [
        int(min(point[0] for point in points)),
        int(min(point[1] for point in points)),
        int(max(point[0] for point in points)),
        int(max(point[1] for point in points)),
    ]


def distinct_epsgs(layer):
    values = set()
    for feature in layer:
        value = feature.GetField("proj_epsg")
        if value is not None:
            values.add(int(value))
    layer.ResetReading()
    return sorted(values)


def stage_index(selection, source_path, output_path, proxy_base, cog_region, access_mode):
    source = gdal.OpenEx(
        f"PARQUET:{source_path}",
        gdal.OF_VECTOR | gdal.OF_READONLY,
        allowed_drivers=["Parquet"],
    )
    if source is None or source.GetLayerCount() != 1:
        raise RuntimeError(f"expected one GeoParquet layer in {source_path}")
    layer = source.GetLayer(0)
    layer_name = layer.GetName()
    extent = layer.GetExtent(force=1)
    count = layer.GetFeatureCount(force=1)
    epsgs = distinct_epsgs(layer)

    proxy_prefix = (
        f"{proxy_base.rstrip('/')}/{access_mode}/{cog_region}/"
    )
    sql = (
        "SELECT *, "
        f"'{sql_quote(proxy_prefix)}' || source_bucket || '/' || source_key AS location, "
        "'EPSG:' || CAST(proj_epsg AS TEXT) AS tile_srs "
        f"FROM {sql_identifier(layer_name)}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    result = gdal.VectorTranslate(
        str(output_path),
        source,
        format="Parquet",
        SQLStatement=sql,
        SQLDialect="SQLite",
        layerName=output_path.stem,
        layerCreationOptions=[
            "COMPRESSION=ZSTD",
            "ROW_GROUP_SIZE=2048",
            "WRITE_COVERING_BBOX=YES",
            "SORT_BY_BBOX=YES",
        ],
    )
    if result is None:
        raise RuntimeError(f"failed to stage GeoParquet index {source_path}")
    result = None
    source = None

    min_x, max_x, min_y, max_y = extent
    bbox_4326 = [min_x, min_y, max_x, max_y]
    state = selection["state"]
    year = selection["year"]
    collection_id = f"naip-{state}-{year}"
    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": collection_id,
        "title": selection.get("label") or f"NAIP {state.upper()} {year}",
        "description": f"NAIP imagery for {state.upper()} {year} from GeoParquet",
        "license": "various",
        "layer_name": collection_id,
        "layer_names": [collection_id],
        "map_name": "naip-imagery",
        "label": selection.get("label") or f"NAIP {state.upper()} {year}",
        "attribution": "USDA NAIP",
        "enabled": True,
        "group": os.environ.get("PARQUET_LAYER_GROUP", "naip"),
        "min_zoom": 12,
        "max_zoom": 20,
        "native_zoom": 18,
        "draw_order": year,
        "backend": "parquet",
        "parquet": True,
        "source": {
            "uri": selection["resolved_uri"],
            "region": cog_region,
            "access_mode": access_mode,
            "requester_pays": access_mode == "requester-pays",
            "state": state,
            "year": year,
        },
        "native_epsg": 4326,
        "native_epsgs": epsgs,
        "cog_count": count,
        "bbox_4326": bbox_4326,
        "extent_3857": transform_extent_3857(extent),
        "extent_native": bbox_4326,
        "tileindex": str(output_path),
        "tileindex_layer_name": output_path.stem,
        "tileindexes": [{
            "epsg": 4326,
            "tileindex": str(output_path),
            "tileindex_layer_name": output_path.stem,
            "layer_name": collection_id,
            "extent_native": bbox_4326,
            "cog_count": count,
            "tileitem": "location",
            "tilesrs": "tile_srs",
            "mixed_srs": True,
        }],
        "indexed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "raster_processing": ["BANDS=1,2,3", "RESAMPLE=AVERAGE"],
    }


def main():
    if gdal is None:
        raise RuntimeError("GDAL Python bindings are required for the parquet backend")
    gdal.UseExceptions()
    selections = parse_selections(os.environ.get(SELECTION_ENV))
    uri_template = os.environ.get("PARQUET_INDEX_URI_TEMPLATE", DEFAULT_URI_TEMPLATE)
    index_dir = Path(os.environ.get("PARQUET_INDEX_DIR", DEFAULT_INDEX_DIR))
    output_path = Path(os.environ.get("COLLECTIONS_FILE", DEFAULT_OUTPUT))
    proxy_base = os.environ.get("PARQUET_PROXY_BASE", "http://localhost:8001")
    cog_region = os.environ.get("PARQUET_COG_REGION", "us-west-2")
    access_mode = os.environ.get("PARQUET_COG_ACCESS_MODE", "requester-pays")
    if access_mode not in ("requester-pays", "standard"):
        raise ValueError("PARQUET_COG_ACCESS_MODE must be requester-pays or standard")

    collections = []
    for selection in selections:
        uri = selection["uri"] or uri_template.format(**selection)
        selection["resolved_uri"] = uri
        stem = f"naip-{selection['state']}-{selection['year']}"
        source_path = index_dir / "source" / f"{stem}.parquet"
        staged_path = index_dir / f"{stem}.parquet"
        print(f"GeoParquet: downloading {uri}", flush=True)
        download_s3(uri, source_path)
        collection = stage_index(
            selection,
            source_path,
            staged_path,
            proxy_base,
            cog_region,
            access_mode,
        )
        collections.append(collection)
        source_path.unlink(missing_ok=True)
        print(
            f"GeoParquet: staged {collection['cog_count']} COGs for "
            f"{selection['state']}:{selection['year']}",
            flush=True,
        )

    catalog = {
        "type": "Catalog",
        "stac_version": "1.0.0",
        "id": "mapserver-parquet-catalog",
        "title": "Configured GeoParquet imagery catalog",
        "description": "Generated at container startup from PARQUET_SELECTION_JSON",
        "version": 1,
        "collections": collections,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"GeoParquet: wrote {output_path} with {len(collections)} selections", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: GeoParquet backend preparation failed: {exc}", file=sys.stderr)
        raise
