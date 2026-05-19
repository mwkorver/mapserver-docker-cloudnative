import concurrent.futures
import math
import random
import shutil
import time
import urllib.request
from pathlib import Path

try:
    import psycopg2
except ImportError:
    psycopg2 = None

WEB_MERCATOR_EXTENT = 20037508.342789244
COVERAGE_THRESHOLD = 0.999


def lonlat_to_tile_xy(lon, lat, zoom):
    n = 2.0**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_bbox_3857(x, y, zoom):
    resolution = (WEB_MERCATOR_EXTENT * 2) / (2**zoom)
    minx = -WEB_MERCATOR_EXTENT + x * resolution
    miny = WEB_MERCATOR_EXTENT - (y + 1) * resolution
    maxx = -WEB_MERCATOR_EXTENT + (x + 1) * resolution
    maxy = WEB_MERCATOR_EXTENT - y * resolution
    return minx, miny, maxx, maxy


def tile_bbox_3857_string(x, y, zoom):
    return ",".join(str(value) for value in tile_bbox_3857(x, y, zoom))


def collection_tile_range(bounds, zoom):
    min_lat = min(bounds[0][0], bounds[1][0])
    max_lat = max(bounds[0][0], bounds[1][0])
    min_lon = min(bounds[0][1], bounds[1][1])
    max_lon = max(bounds[0][1], bounds[1][1])

    min_x, max_y = lonlat_to_tile_xy(min_lon, min_lat, zoom)
    max_x, min_y = lonlat_to_tile_xy(max_lon, max_lat, zoom)

    if min_x > max_x:
        min_x, max_x = max_x, min_x
    if min_y > max_y:
        min_y, max_y = max_y, min_y
    return min_x, max_x, min_y, max_y


def rectangle_geometry(ogr, bbox):
    minx, miny, maxx, maxy = bbox
    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint(minx, miny)
    ring.AddPoint(maxx, miny)
    ring.AddPoint(maxx, maxy)
    ring.AddPoint(minx, maxy)
    ring.AddPoint(minx, miny)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


class FgbCandidateProvider:
    backend = "FlatGeobuf"

    def __init__(self, collection):
        self.ogr, self.dataset, self.layer, self.transform = open_fgb_layer(collection)

    def classify_tile(self, x, y, zoom):
        return classify_fgb_tile(self.layer, self.ogr, self.transform, x, y, zoom)

    def close(self):
        self.dataset = None


class PostgisCandidateProvider:
    backend = "PostGIS"

    def __init__(self, collection, db_connection_string):
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required for PostGIS benchmark candidate generation")
        self.collection_id = collection["id"]
        self.connection = psycopg2.connect(db_connection_string)

    def classify_tile(self, x, y, zoom):
        bbox = tile_bbox_3857(x, y, zoom)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                WITH tile AS (
                    SELECT ST_MakeEnvelope(%s, %s, %s, %s, 3857) AS geom
                ),
                hits AS (
                    SELECT c.geom
                    FROM cog_index c
                    CROSS JOIN tile t
                    WHERE c.collection_id = %s
                      AND c.geom && t.geom
                      AND ST_Intersects(c.geom, t.geom)
                ),
                counted AS (
                    SELECT
                        COUNT(*)::int AS cog_count,
                        COALESCE(BOOL_OR(ST_Contains(h.geom, t.geom) OR ST_Within(t.geom, h.geom)), false) AS inside_one
                    FROM hits h
                    CROSS JOIN tile t
                )
                SELECT cog_count, inside_one
                FROM counted
                """,
                (*bbox, self.collection_id),
            )
            cog_count, inside_one = cursor.fetchone()

            if cog_count == 0:
                return "empty", cog_count
            if cog_count > 4:
                return "too_many", cog_count
            if inside_one:
                return "interior", cog_count

            cursor.execute(
                """
                WITH tile AS (
                    SELECT ST_MakeEnvelope(%s, %s, %s, %s, 3857) AS geom
                ),
                hits AS (
                    SELECT c.geom
                    FROM cog_index c
                    CROSS JOIN tile t
                    WHERE c.collection_id = %s
                      AND c.geom && t.geom
                      AND ST_Intersects(c.geom, t.geom)
                ),
                unioned AS (
                    SELECT ST_UnaryUnion(ST_Collect(h.geom)) AS geom
                    FROM hits h
                )
                SELECT COALESCE(
                    ST_Area(ST_Intersection(u.geom, t.geom)) / NULLIF(ST_Area(t.geom), 0),
                    0
                )
                FROM unioned u
                CROSS JOIN tile t
                """,
                (*bbox, self.collection_id),
            )
            coverage = float(cursor.fetchone()[0] or 0)

        if coverage >= COVERAGE_THRESHOLD:
            return "seam", cog_count
        return "partial", cog_count

    def close(self):
        self.connection.close()


def open_candidate_provider(collection, db_connection_string=None):
    if db_connection_string and collection.get("postgis", False):
        return PostgisCandidateProvider(collection, db_connection_string)
    return FgbCandidateProvider(collection)


def open_fgb_layer(collection):
    try:
        from osgeo import ogr, osr
    except ImportError as exc:
        raise RuntimeError("GDAL/OGR is required for FGB benchmark candidate generation") from exc

    fgb_path = collection.get("tileindex")
    if not fgb_path:
        raise RuntimeError("active collection does not define a FlatGeobuf tileindex")

    dataset = ogr.Open(fgb_path)
    if dataset is None:
        raise RuntimeError(f"could not open FlatGeobuf tileindex: {fgb_path}")

    layer_name = collection.get("tileindex_layer_name")
    layer = dataset.GetLayerByName(layer_name) if layer_name else dataset.GetLayer(0)
    if layer is None:
        raise RuntimeError(f"could not open FlatGeobuf layer: {layer_name or 0}")

    layer_srs = layer.GetSpatialRef()
    web_srs = osr.SpatialReference()
    web_srs.ImportFromEPSG(3857)
    transform = osr.CoordinateTransformation(web_srs, layer_srs) if layer_srs and not layer_srs.IsSame(web_srs) else None
    return ogr, dataset, layer, transform


def classify_fgb_tile(layer, ogr, transform, x, y, zoom):
    tile = rectangle_geometry(ogr, tile_bbox_3857(x, y, zoom))
    if transform:
        tile.Transform(transform)

    layer.SetSpatialFilter(tile)
    geoms = []
    try:
        for feature in layer:
            geom = feature.GetGeometryRef()
            if geom and geom.Intersects(tile):
                geoms.append(geom.Clone())
    finally:
        layer.ResetReading()
        layer.SetSpatialFilter(None)

    cog_count = len(geoms)
    if cog_count == 0:
        return "empty", cog_count
    if cog_count > 4:
        return "too_many", cog_count

    if any(geom.Contains(tile) or tile.Within(geom) for geom in geoms):
        return "interior", cog_count

    union = geoms[0].Clone()
    for geom in geoms[1:]:
        union = union.Union(geom)
    intersection = union.Intersection(tile)
    coverage = intersection.Area() / tile.Area() if tile.Area() else 0
    if coverage >= COVERAGE_THRESHOLD:
        return "seam", cog_count
    return "partial", cog_count


def generate_valid_tiles(collection, viewer_config, zoom, total_requests, db_connection_string=None, max_attempt_factor=200):
    bounds = viewer_config.get("bounds") or [[37.0, -90.0], [42.0, -73.0]]
    min_x, max_x, min_y, max_y = collection_tile_range(bounds, zoom)
    provider = open_candidate_provider(collection, db_connection_string)

    max_attempts = max(total_requests * max_attempt_factor, total_requests + 1000)
    stats = {
        "empty": 0,
        "interior": 0,
        "seam": 0,
        "partial": 0,
        "too_many": 0,
        "duplicates": 0,
    }
    valid_tiles = []
    seen = set()
    attempts = 0

    try:
        while len(valid_tiles) < total_requests and attempts < max_attempts:
            attempts += 1
            x = random.randint(min_x, max_x)
            y = random.randint(min_y, max_y)
            tile_key = (zoom, x, y)
            if tile_key in seen:
                stats["duplicates"] += 1
                continue
            seen.add(tile_key)

            classification, cog_count = provider.classify_tile(x, y, zoom)
            stats[classification] += 1
            if classification in {"interior", "seam"}:
                valid_tiles.append({
                    "z": zoom,
                    "x": x,
                    "y": y,
                    "classification": classification,
                    "cogCount": cog_count,
                })
    finally:
        provider.close()

    if not valid_tiles:
        raise RuntimeError("could not generate any valid benchmark tiles from the active collection")

    return valid_tiles, {
        "attempts": attempts,
        "requested": total_requests,
        "generated": len(valid_tiles),
        "backend": provider.backend,
        "tileRange": {"x": [min_x, max_x], "y": [min_y, max_y]},
        **stats,
    }


def build_wms_url(layer, tile):
    bbox = tile_bbox_3857_string(tile["x"], tile["y"], tile["z"])
    return (
        "http://127.0.0.1/mapserv?"
        "SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
        f"&LAYERS={layer}&CRS=EPSG:3857&BBOX={bbox}"
        "&WIDTH=256&HEIGHT=256&FORMAT=image/png"
    )


def prepare_benchmark(payload, collection, viewer_config, db_connection_string=None):
    total_requests = int(payload.get("requests", 50))
    zoom = int(payload["zoom"]) if "zoom" in payload else int(viewer_config.get("imageryMinZoom", 14))
    tiles, candidate_stats = generate_valid_tiles(collection, viewer_config, zoom, total_requests, db_connection_string)
    return {
        "tiles": tiles,
        "candidateStats": candidate_stats,
    }


def fetch_url(url):
    start = time.time()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MapServerBenchmark"})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
            return time.time() - start, True
    except Exception:
        return time.time() - start, False


def run_benchmark(payload, collection, viewer_config, nginx_cache_path, db_connection_string=None):
    concurrency = int(payload.get("concurrency", 10))
    total_requests = int(payload.get("requests", 50))
    clear_cache = bool(payload.get("clear_cache", False))
    layer = viewer_config.get("layerName", "imagery")

    if clear_cache:
        cache_path = Path(nginx_cache_path)
        shutil.rmtree(str(cache_path), ignore_errors=True)
        cache_path.mkdir(parents=True, exist_ok=True)

    tiles = payload.get("preparedTiles")
    candidate_stats = payload.get("candidateStats")
    if not isinstance(tiles, list) or not tiles:
        prepared = prepare_benchmark(payload, collection, viewer_config, db_connection_string)
        tiles = prepared["tiles"]
        candidate_stats = prepared["candidateStats"]
    total_requests = len(tiles)
    urls = [build_wms_url(layer, tile) for tile in tiles]

    start_total = time.time()
    results = []
    success_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        for duration, success in executor.map(fetch_url, urls):
            results.append(duration)
            if success:
                success_count += 1

    total_duration = time.time() - start_total
    results.sort()
    avg = sum(results) / len(results) if results else 0
    p50 = results[int(len(results) * 0.5)] if results else 0
    p95 = results[int(len(results) * 0.95)] if results else 0

    return {
        "concurrency": concurrency,
        "requests": total_requests,
        "success": success_count,
        "failed": total_requests - success_count,
        "total_time_ms": int(total_duration * 1000),
        "avg_ms": int(avg * 1000),
        "p50_ms": int(p50 * 1000),
        "p95_ms": int(p95 * 1000),
        "candidateStats": candidate_stats,
    }
