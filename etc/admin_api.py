#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import psycopg2
except ImportError:
    psycopg2 = None


SUPERVISOR_CONF = Path("/etc/supervisor/conf.d/supervisord.conf")
ADMIN_CONFIG = Path("/usr/src/admin/config.json")
RUNTIME_CONFIG = Path("/usr/src/admin/runtime_config.json")
MAPFILE = Path("/usr/src/mapfiles/mapfile.map")
COLLECTIONS_FILE = Path("/usr/src/mapfiles/collections.json")
MAPFILES_DIR = Path("/usr/src/mapfiles")
SCAN_SCRIPT = Path("/usr/src/scripts/scan_cog_collection.py")
MAPFILE_GENERATOR = Path("/etc/mapfile_generator.py")
PARQUET_REFRESH = Path("/etc/parquet_refresh.py")
PARQUET_REFRESH_STATUS = Path("/usr/src/mapfiles/parquet-refresh.json")
NGINX_CONF = Path("/etc/nginx/sites-available/mapserver_proxy.conf")
NGINX_CACHE_CONF = Path("/etc/nginx/conf.d/cog_cache.conf")
NGINX_CACHE = Path("/var/cache/nginx/cog")
# Slice-mode toggle: currently a no-op (the nginx slice module breaks
# MapServer's parallel HTTP/2 byte-range reads).  The field is accepted
# and persisted so the admin UI can keep its picker visible; flipping
# it has no effect on the live cache until the upstream behavior is
# fixed.  See README "What's new here" → caching notes.
NGINX_SLICE_MODES = ("off", "1m", "4m")
MAX_WORKERS = int(os.environ.get("MAPSERVER_MAX_NUMPROCS", "32"))
DEFAULT_RUNTIME = {
    "activeCollection": "",
    "gdalCacheMaxMb": 128,
    # 0 disables the per-worker /vsicurl/ byte-range cache.  The shared
    # nginx proxy_cache (4 GB on disk, 1 entry covers all workers) makes
    # the per-worker RAM cache redundant — measured slight latency win
    # and frees (numprocs * VSI_CACHE_SIZE) of RAM.  Set > 0 if running
    # the image without the nginx range-cache layer.
    "vsiCacheSizeMb": 0,
    "mapserverDebug": False,
    "nginxCacheMaxSize": "20g",
    "nginxCacheTtl": "24h",
    "nginxCache404Ttl": "1m",
    # Slice-mode field — accepted and persisted, currently no-op.  Enabling
    # nginx slice in front of the SigV4 signer broke MapServer's parallel
    # HTTP/2 byte-range reads (sub-requests hung).  The admin UI keeps the
    # picker visible (disabled) as scaffolding for a future fix.  Always
    # validate to one of NGINX_SLICE_MODES so storage stays consistent.
    "nginxSliceMode": "off",
}
STATIC_NGINX_CACHE_SETTINGS = {
    "cacheKey": "$request_method:$request_uri:$http_range",
    "proxyCacheConvertHead": "off",
    "proxyCacheLock": "on",
    "cacheStatuses": "200 206",
    "headAndGetSeparate": True,
    "upstream": "http://127.0.0.1:9000",
}
# Fallback shape if collections.json is missing or empty. Keeps the admin
# UI and viewer renderable on a fresh build before anyone has scanned.
FALLBACK_VIEWER_CONFIG = {
    "collectionId":  "",
    "collectionName": "—",
    "mapName": "imagery",
    "layerName": "—",
    "bounds": [[37.0, -90.0], [42.0, -73.0]],
    "center": [39.5, -82.0],
    "imageryMinZoom": 14,
    "attribution": "",
}

for module_path in ("/usr/src", str(Path(__file__).resolve().parent.parent)):
    if module_path not in sys.path:
        sys.path.insert(0, module_path)

from benchmark.tile_benchmark import prepare_benchmark as prepare_tile_benchmark
from benchmark.tile_benchmark import run_benchmark as run_tile_benchmark


def _read_collections():
    """Read collections.json. Returns {} on any error (empty/missing/parse)."""
    try:
        return json.loads(COLLECTIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _humanize_indexed_at(iso_string):
    """Return e.g. '2h ago' from an ISO-8601 timestamp; '' if unparseable."""
    if not iso_string:
        return ""
    try:
        normalized = iso_string.replace("Z", "+00:00")
        t = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    delta = dt.datetime.now(dt.timezone.utc) - t
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _collection_status(collection):
    """Build the "Status" column text from collection.json metadata."""
    parts = []
    cog_count = collection.get("cog_count")
    if cog_count is not None:
        parts.append(f"{cog_count:,} COGs")
    indexed_at = collection.get("indexed_at")
    if indexed_at:
        rel = _humanize_indexed_at(indexed_at)
        parts.append(f"indexed {rel}" if rel else f"indexed {indexed_at}")
    if collection.get("postgis"):
        parts.append("postgis")
    elif collection.get("parquet"):
        parts.append("geoparquet")
    return " · ".join(parts) if parts else "configured"


def _collection_to_ui(collection):
    """Convert a collections.json entry to the admin UI's expected shape."""
    source = collection.get("source") or {}
    return {
        "collectionName": collection.get("id"),
        "layerName": collection.get("layer_name") or collection.get("id"),
        "layerNames": collection.get("layer_names") or [collection.get("layer_name") or collection.get("id")],
        "enabled": bool(collection.get("enabled", True)),
        "minZoom": collection.get("min_zoom"),
        "maxZoom": collection.get("max_zoom"),
        "drawOrder": collection.get("draw_order", 10),
        "status": _collection_status(collection),
        "source": {
            "label": collection.get("label") or collection.get("id"),
            "bucket": source.get("bucket"),
            "prefix": source.get("prefix"),
            "region": source.get("region", "us-west-2"),
            "accessMode": source.get("access_mode", "unsigned"),
            "requesterPays": bool(source.get("requester_pays")),
        },
    }


def _bounds_from_bbox_4326(bbox):
    """[minLon, minLat, maxLon, maxLat] -> [[minLat, minLon], [maxLat, maxLon]]."""
    if not bbox or len(bbox) != 4:
        return None
    return [[bbox[1], bbox[0]], [bbox[3], bbox[2]]]


def _center_from_bbox_4326(bbox):
    """Midpoint as [lat, lon]."""
    if not bbox or len(bbox) != 4:
        return None
    return [(bbox[1] + bbox[3]) / 2.0, (bbox[0] + bbox[2]) / 2.0]



def _collection_artifact_paths(collection):
    """Generated local files owned by a collection scan.

    Deletes are intentionally limited to /usr/src/mapfiles artifacts so an
    accidental absolute path in collections.json cannot remove arbitrary
    container files.
    """
    paths = []
    for key in ["tileindex", "tileindex_geojson"]:
        value = collection.get(key)
        if value:
            paths.append(Path(value))
    for item in collection.get("tileindexes") or []:
        value = item.get("tileindex")
        if value:
            paths.append(Path(value))

    seen = set()
    safe_paths = []
    mapfiles_root = MAPFILES_DIR.resolve()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved == mapfiles_root or mapfiles_root not in resolved.parents:
            print(f"WARN: refusing to delete collection artifact outside {mapfiles_root}: {resolved}", file=sys.stderr)
            continue
        safe_paths.append(resolved)
    return safe_paths


def _collection_to_viewer(collection):
    """Convert a collections.json entry to the viewer's expected shape.

    collectionId drives the OpenLayers viewer's WFS footprints URL:
      /mapserv?SERVICE=WFS&TYPENAMES=cog-extents-{collectionId}&...
    """
    bbox = collection.get("bbox_4326")
    cid  = collection.get("id")
    return {
        "collectionId":  cid,
        "collectionName": cid,
        "mapName":  collection.get("map_name") or "imagery",
        "layerName": collection.get("layer_name") or cid,
        "layerNames": collection.get("layer_names") or [collection.get("layer_name") or cid],
        "bounds": _bounds_from_bbox_4326(bbox) or FALLBACK_VIEWER_CONFIG["bounds"],
        "center": _center_from_bbox_4326(bbox) or FALLBACK_VIEWER_CONFIG["center"],
        "imageryMinZoom": collection.get("min_zoom") or 14,
        "imageryMaxZoom": collection.get("max_zoom"),
        "nativeZoom": collection.get("native_zoom", 18),
        "attribution": collection.get("attribution", ""),
    }



def write_json(handler, status, payload):
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_error(handler, status, message):
    write_json(handler, status, {"error": message})


def write_enabled():
    return os.environ.get("ADMIN_WRITE_ENABLED", "false").lower() in ("1", "true", "yes")


def collection_write_enabled():
    value = os.environ.get("ADMIN_COLLECTION_WRITE_ENABLED")
    if value is None:
        return write_enabled()
    return value.lower() in ("1", "true", "yes")


def parquet_refresh_enabled():
    return (
        os.environ.get("DB_BACKEND") == "parquet"
        and bool(os.environ.get("PARQUET_SELECTION_S3_URI"))
        and write_enabled()
    )


def current_numprocs():
    conf = SUPERVISOR_CONF.read_text()
    match = re.search(r"^numprocs=(\d+)$", conf, re.MULTILINE)
    if not match:
        raise RuntimeError("numprocs not found in supervisor config")
    return int(match.group(1))


def observed_process_counts():
    try:
        ps = subprocess.run(
            ["ps", "-eo", "comm,args"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return {"mapserverWorkers": None, "nginxWorkers": None}

    mapserver_workers = 0
    nginx_workers = 0
    for line in ps.stdout.splitlines():
        if "mapserv" in line:
            mapserver_workers += 1
        if "nginx: worker process" in line:
            nginx_workers += 1
    return {"mapserverWorkers": mapserver_workers, "nginxWorkers": nginx_workers}


def active_backend_summary():
    doc = _read_collections()
    active = _active_collection(doc.get("collections", []))
    if active is None:
        return {
            "activeCollection": "",
            "backend": "none",
            "cogCount": 0,
            "nativeEpsgs": [],
            "indexSource": "",
        }
    db_enabled = bool(db_connection_string()) and bool(active.get("postgis", False))
    parquet_enabled = bool(active.get("parquet", False))
    tileindexes = active.get("tileindexes") or []
    index_source = "cog_index" if db_enabled else (
        ", ".join(Path(item.get("tileindex", "")).name for item in tileindexes if item.get("tileindex"))
        or Path(active.get("tileindex") or active.get("tileindex_geojson") or "").name
    )
    return {
        "activeCollection": active.get("id", ""),
        "backend": "PostGIS" if db_enabled else ("GeoParquet" if parquet_enabled else "FlatGeobuf"),
        "cogCount": active.get("cog_count", 0),
        "nativeEpsgs": active.get("native_epsgs") or ([active.get("native_epsg")] if active.get("native_epsg") else []),
        "indexSource": index_source,
    }


def ecs_task_metadata():
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return {}
    try:
        with urllib.request.urlopen(uri.rstrip("/") + "/task", timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def environment_info():
    metadata = ecs_task_metadata()
    processes = observed_process_counts()
    backend = active_backend_summary()
    fargate_cpu = os.environ.get("FARGATE_CPU", "4096")
    fargate_memory = os.environ.get("FARGATE_MEMORY", "8192")
    return {
        "mode": "AWS Fargate" if metadata or os.environ.get("DB_SECRET_ARN") else "local Docker",
        "dbBackend": os.environ.get("DB_BACKEND", "postgis" if os.environ.get("DB_SECRET_ARN") else "local"),
        "awsRegion": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2")),
        "publicHost": os.environ.get("PUBLIC_HOST", "localhost"),
        "s3Signing": os.environ.get("S3_SIGNING", "auto"),
        "fargateCpu": fargate_cpu,
        "fargateCpuVcpu": round(int(fargate_cpu) / 1024, 2) if str(fargate_cpu).isdigit() else None,
        "fargateMemory": fargate_memory,
        "fargateEphemeralStorageGib": os.environ.get("FARGATE_EPHEMERAL_STORAGE_GIB", "21"),
        "ecsCluster": metadata.get("Cluster", ""),
        "ecsTaskArn": metadata.get("TaskARN", ""),
        "ecsTaskFamily": metadata.get("Family", ""),
        "ecsTaskRevision": metadata.get("Revision", ""),
        "availabilityZone": metadata.get("AvailabilityZone", ""),
        "mapserverNumprocs": current_numprocs(),
        **processes,
        "effectiveWmsWorkers": processes.get("mapserverWorkers") or current_numprocs(),
        "indexBackend": backend,
    }


def runtime_config():
    config = dict(DEFAULT_RUNTIME)
    if RUNTIME_CONFIG.exists():
        try:
            loaded = json.loads(RUNTIME_CONFIG.read_text())
            config.update({k: loaded[k] for k in DEFAULT_RUNTIME.keys() if k in loaded})
        except Exception:
            pass
    return validate_runtime(config)


def validate_runtime(config):
    result = dict(DEFAULT_RUNTIME)
    result.update(config)
    result["activeCollection"] = str(result.get("activeCollection") or "").strip()
    result["gdalCacheMaxMb"] = int(result["gdalCacheMaxMb"])
    result["vsiCacheSizeMb"] = int(result["vsiCacheSizeMb"])
    result["mapserverDebug"] = bool(result["mapserverDebug"])
    result["nginxCacheMaxSize"] = validate_nginx_size(result["nginxCacheMaxSize"], "nginxCacheMaxSize")
    result["nginxCacheTtl"] = validate_nginx_ttl(result["nginxCacheTtl"], "nginxCacheTtl")
    result["nginxCache404Ttl"] = validate_nginx_ttl(result["nginxCache404Ttl"], "nginxCache404Ttl")
    result["nginxSliceMode"] = str(result.get("nginxSliceMode") or "off").strip().lower()
    if result["nginxSliceMode"] not in NGINX_SLICE_MODES:
        raise ValueError(f"nginxSliceMode must be one of: {', '.join(NGINX_SLICE_MODES)}")
    if result["gdalCacheMaxMb"] < 0 or result["gdalCacheMaxMb"] > 65536:
        raise ValueError("gdalCacheMaxMb must be between 0 and 65536")
    if result["vsiCacheSizeMb"] < 0 or result["vsiCacheSizeMb"] > 65536:
        raise ValueError("vsiCacheSizeMb must be between 0 and 65536")
    return result


def validate_nginx_size(value, name):
    if isinstance(value, int):
        if value < 1 or value > 1000:
            raise ValueError(f"{name} must be between 1 and 1000 GB")
        return f"{value}g"
    value = str(value).strip().lower()
    if re.fullmatch(r"[1-9][0-9]*", value):
        numeric_value = int(value)
        if numeric_value < 1 or numeric_value > 1000:
            raise ValueError(f"{name} must be between 1 and 1000 GB")
        return f"{numeric_value}g"
    if not re.fullmatch(r"[1-9][0-9]*[kmg]", value):
        raise ValueError(f"{name} must look like 512m, 5g, or 20g")
    return value


def validate_nginx_ttl(value, name):
    value = str(value).strip().lower()
    if not re.fullmatch(r"[1-9][0-9]*[smhd]", value):
        raise ValueError(f"{name} must look like 1m, 24h, or 7d")
    return value


def save_runtime_config(config):
    config = validate_runtime(config)
    RUNTIME_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    # viewer/config.json is proxied by nginx to the /viewer-config API endpoint,
    # so the static file is never served directly. No write needed here.
    return config


def replace_or_insert_config(map_text, key, value):
    line = f'  CONFIG "{key}" "{value}"'
    pattern = rf'^\s*CONFIG\s+"{re.escape(key)}"\s+"[^"]*"\s*$'
    updated, count = re.subn(pattern, line, map_text, count=1, flags=re.MULTILINE)
    if count:
        return updated
    return re.sub(r"^MAP\s*$", "MAP\n" + line, map_text, count=1, flags=re.MULTILINE)


def apply_mapfile_runtime(config):
    if not MAPFILE.exists():
        return
    map_text = MAPFILE.read_text()
    map_text = replace_or_insert_config(map_text, "GDAL_CACHEMAX", str(config["gdalCacheMaxMb"]))
    vsi_enabled = "FALSE" if config["vsiCacheSizeMb"] == 0 else "TRUE"
    map_text = replace_or_insert_config(map_text, "VSI_CACHE", vsi_enabled)
    map_text = replace_or_insert_config(
        map_text,
        "VSI_CACHE_SIZE",
        str(config["vsiCacheSizeMb"] * 1024 * 1024),
    )
    map_text = replace_or_insert_config(map_text, "CPL_DEBUG", "ON" if config["mapserverDebug"] else "OFF")
    map_text = replace_or_insert_config(map_text, "MS_DEBUGLEVEL", "5" if config["mapserverDebug"] else "0")
    MAPFILE.write_text(map_text)


def replace_nginx_directive(conf, pattern, replacement):
    updated, count = re.subn(pattern, replacement, conf, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"failed to update nginx directive matching {pattern}")
    return updated


def apply_nginx_runtime(config):
    cache_conf = f"""# Disk-backed cache for COG byte-range fetches. Lives in http context so a
# single shared cache is used by all FastCGI workers — fixes per-worker
# VSI cache fragmentation.
# Generated at container startup by /etc/admin_api.py.
proxy_cache_path /var/cache/nginx/cog
    levels=1:2
    keys_zone=cog:128m
    max_size={config['nginxCacheMaxSize']}
    inactive={config['nginxCacheTtl']}
    use_temp_path=off;
"""
    NGINX_CACHE_CONF.write_text(cache_conf)

    conf = NGINX_CONF.read_text()
    conf = replace_nginx_directive(
        conf,
        r"^\s*proxy_cache_valid\s+200\s+206\s+\S+;",
        f"        proxy_cache_valid 200 206 {config['nginxCacheTtl']};",
    )
    conf = replace_nginx_directive(
        conf,
        r"^\s*proxy_cache_valid\s+404\s+\S+;",
        f"        proxy_cache_valid 404 {config['nginxCache404Ttl']};",
    )
    NGINX_CONF.write_text(conf)


def reload_nginx():
    test = subprocess.run(["nginx", "-t"], check=False, text=True, capture_output=True)
    if test.returncode != 0:
        raise RuntimeError("nginx config test failed: " + test.stdout + test.stderr)
    reload_result = subprocess.run(["nginx", "-s", "reload"], check=False, text=True, capture_output=True)
    if reload_result.returncode != 0:
        raise RuntimeError("nginx reload failed: " + reload_result.stdout + reload_result.stderr)
    return {"test": test.stderr.strip() or test.stdout.strip(), "reload": reload_result.stderr.strip() or reload_result.stdout.strip()}


def restart_mapserver_group():
    stop = subprocess.run(
        ["supervisorctl", "stop", "mapserver:"],
        check=False,
        text=True,
        capture_output=True,
    )
    start = subprocess.run(
        ["supervisorctl", "start", "mapserver:"],
        check=False,
        text=True,
        capture_output=True,
    )
    if start.returncode != 0:
        raise RuntimeError("mapserver restart failed: " + stop.stdout + stop.stderr + start.stdout + start.stderr)
    return {"stop": stop.stdout.strip(), "start": start.stdout.strip()}


def nginx_cache_stats():
    files = 0
    bytes_used = 0
    if NGINX_CACHE.exists():
        for path in NGINX_CACHE.rglob("*"):
            if path.is_file():
                files += 1
                try:
                    bytes_used += path.stat().st_size
                except OSError:
                    pass
    return {
        "path": str(NGINX_CACHE),
        "files": files,
        "bytes": bytes_used,
        "megabytes": round(bytes_used / 1024 / 1024, 2),
    }


def admin_config():
    runtime = runtime_config()
    environment = environment_info()
    config = {
        "mapserverNumprocs": environment["mapserverNumprocs"],
        "mapserverWorkers": environment["mapserverWorkers"],
        "nginxWorkers": environment["nginxWorkers"],
        "indexBackend": environment["indexBackend"],
        "maxMapserverNumprocs": MAX_WORKERS,
        "writeEnabled": write_enabled(),
        "collectionWriteEnabled": collection_write_enabled(),
        "parquetRefreshEnabled": parquet_refresh_enabled(),
        "parquetSelectionS3Uri": os.environ.get("PARQUET_SELECTION_S3_URI", ""),
        "fargateCpu": environment["fargateCpu"],
        "fargateMemory": environment["fargateMemory"],
        "s3Signing": environment["s3Signing"],
        "awsRegion": environment["awsRegion"],
        "staticNginxCacheSettings": STATIC_NGINX_CACHE_SETTINGS,
        **runtime,
    }
    ADMIN_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    return config


def parquet_refresh_status():
    try:
        status = json.loads(PARQUET_REFRESH_STATUS.read_text())
    except (OSError, json.JSONDecodeError):
        status = {"status": "not-run"}
    return {
        "enabled": parquet_refresh_enabled(),
        "selectionS3Uri": os.environ.get("PARQUET_SELECTION_S3_URI", ""),
        **status,
    }


_PARQUET_REFRESH_THREAD = None
_PARQUET_REFRESH_THREAD_LOCK = threading.Lock()


def _run_parquet_refresh():
    result = subprocess.run(
        ["python3", str(PARQUET_REFRESH), "refresh"],
        check=False,
        text=True,
        capture_output=True,
    )
    output = result.stdout.strip() or result.stderr.strip()
    print(
        f"admin-api: GeoParquet refresh exited {result.returncode}: {output}",
        flush=True,
    )


def start_parquet_refresh():
    global _PARQUET_REFRESH_THREAD
    if os.environ.get("DB_BACKEND") != "parquet":
        raise ValueError("GeoParquet refresh is only available with DB_BACKEND=parquet")
    if not os.environ.get("PARQUET_SELECTION_S3_URI"):
        raise ValueError("PARQUET_SELECTION_S3_URI is not configured")
    with _PARQUET_REFRESH_THREAD_LOCK:
        if _PARQUET_REFRESH_THREAD and _PARQUET_REFRESH_THREAD.is_alive():
            raise ValueError("a GeoParquet refresh is already running")
        PARQUET_REFRESH_STATUS.write_text(
            json.dumps(
                {
                    "status": "refreshing",
                    "selectionSource": {
                        "type": "s3",
                        "uri": os.environ["PARQUET_SELECTION_S3_URI"],
                    },
                },
                indent=2,
            )
            + "\n"
        )
        _PARQUET_REFRESH_THREAD = threading.Thread(
            target=_run_parquet_refresh,
            name="parquet-refresh",
            daemon=True,
        )
        _PARQUET_REFRESH_THREAD.start()
    return parquet_refresh_status()


def _collection_by_id(collections, collection_id):
    if not collection_id:
        return None
    for collection in collections:
        if collection.get("id") == collection_id:
            return collection
    return None


def _active_collection(collections, runtime=None):
    """Pick the active (default) collection.

    Resolution order:
      1. runtime_config.activeCollection if it matches an entry's id
      2. $LOCAL_COLLECTION env var if it matches an entry's id
      3. First enabled collection in draw_order order
      4. First collection at all
    """
    if not collections:
        return None
    runtime = runtime if runtime is not None else runtime_config()
    configured = _collection_by_id(collections, runtime.get("activeCollection"))
    if configured:
        return configured
    explicit = os.environ.get("LOCAL_COLLECTION")
    explicit_match = _collection_by_id(collections, explicit)
    if explicit_match:
        return explicit_match
    enabled = [c for c in collections if c.get("enabled", True)]
    enabled.sort(key=lambda c: c.get("draw_order", 10))
    if enabled:
        return enabled[0]
    return collections[0]


def collections_config():
    """Admin UI shape: every entry in collections.json projected to the
    legacy camelCase format the Collections table renderer expects.
    """
    doc = _read_collections()
    collections = doc.get("collections", [])
    active = _active_collection(collections)
    active_id = active.get("id") if active else None

    ui_collections = []
    for c in collections:
        ui_collections.append(_collection_to_ui(c))

    # Preserve collections.json order. The active row should not jump when
    # selected; only the active marker and Viewer link should move.

    return {"activeCollection": active_id, "collections": ui_collections}


def deployment_mode():
    """'aws' when running as an ECS/Fargate task, else 'local'.

    Cheap — pure env-var presence check, no metadata HTTP call.  ECS
    injects ECS_CONTAINER_METADATA_URI_V4 into every task; the CDK stack
    also passes DB_SECRET_ARN.  The viewer uses this to label the perf
    panel's network hop (ALB on AWS vs the local nginx container).
    """
    if os.environ.get("ECS_CONTAINER_METADATA_URI_V4") or os.environ.get("DB_SECRET_ARN"):
        return "aws"
    return "local"


def viewer_config():
    """Viewer shape: the active collection's bounds/center/layer name."""
    doc = _read_collections()
    active = _active_collection(doc.get("collections", []))
    config = dict(FALLBACK_VIEWER_CONFIG) if active is None else _collection_to_viewer(active)
    config["deployment"] = deployment_mode()
    return config


def set_active_collection(collection_id):
    collections = _read_collections().get("collections", [])
    selected = _collection_by_id(collections, collection_id)
    if selected is None:
        raise ValueError(f"unknown collection: {collection_id}")
    current = runtime_config()
    current["activeCollection"] = collection_id
    config = save_runtime_config(current)
    return {
        "activeCollection": collection_id,
        "viewer": _collection_to_viewer(selected),
        **config,
    }


def set_collection_enabled(collection_id, enabled):
    """Flip enabled on a single collection, persist to collections.json,
    regenerate the mapfile, and restart mapserver to pick up the new layer
    layout."""
    doc = _read_collections()
    collections = doc.get("collections", [])
    target = _collection_by_id(collections, collection_id)
    if target is None:
        raise ValueError(f"unknown collection: {collection_id}")

    previous = bool(target.get("enabled", True))
    new_value = bool(enabled)
    if previous == new_value:
        # No-op: just return current state without disturbing mapserver.
        return collections_config()

    # Mutate in place (target is a reference into collections).
    target["enabled"] = new_value
    doc["collections"] = collections
    COLLECTIONS_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    catalog_sync = sync_collections_catalog()

    # If we disabled the active collection, clear runtime.activeCollection
    # so _active_collection() falls back to the first remaining enabled one.
    if not new_value:
        current = runtime_config()
        if current.get("activeCollection") == collection_id:
            current["activeCollection"] = ""
            save_runtime_config(current)

    # Regenerate mapfile + restart mapserver so the LAYER set reflects the
    # new enabled flag. Both operations are 1-3s each.
    regen_output = regenerate_mapfile()
    restart_result = restart_mapserver_group()

    return {
        **collections_config(),
        "collectionId": collection_id,
        "enabled": new_value,
        "catalogSync": catalog_sync,
        "mapfile": regen_output,
        "mapserverRestart": restart_result,
    }


def set_numprocs(value):
    if not isinstance(value, int):
        raise ValueError("mapserverNumprocs must be an integer")
    if value < 1 or value > MAX_WORKERS:
        raise ValueError(f"mapserverNumprocs must be between 1 and {MAX_WORKERS}")

    conf = SUPERVISOR_CONF.read_text()
    updated, count = re.subn(r"^numprocs=\d+$", f"numprocs={value}", conf, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError("failed to update numprocs in supervisor config")
    SUPERVISOR_CONF.write_text(updated)

    reread = subprocess.run(
        ["supervisorctl", "reread"],
        check=False,
        text=True,
        capture_output=True,
    )
    update = subprocess.run(
        ["supervisorctl", "update"],
        check=False,
        text=True,
        capture_output=True,
    )
    if reread.returncode != 0 or update.returncode != 0:
        raise RuntimeError(
            "supervisor update failed: "
            + reread.stdout
            + reread.stderr
            + update.stdout
            + update.stderr
        )
    return {
        "mapserverNumprocs": current_numprocs(),
        "supervisor": {
            "reread": reread.stdout.strip(),
            "update": update.stdout.strip(),
        },
    }


# ----------------------------------------------------------------------
# COG scan jobs
# ----------------------------------------------------------------------
# Scans are spawned as subprocess threads; state lives in _JOBS guarded by
# _JOBS_LOCK. Job progress is intentionally in-memory and lost on container
# restart. Collection metadata is synced to S3 when COLLECTIONS_S3_URI is set.

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_COLLECTION_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DISCOVERED_RE = re.compile(r"Discovered (\d+) GeoTIFFs")
_PROGRESS_RE = re.compile(r"Scanned (\d+)/(\d+); failures=(\d+)")


def db_connection_string():
    # Intentional copy of mapfile_generator.db_connection_string — both files
    # run as separate processes and cannot share a module. Keep in sync if
    # connection-string logic changes (e.g. SSL, connection pooling).
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


def delete_postgis_collection(collection_id):
    db_conn = db_connection_string()
    if not db_conn:
        return 0
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed; cannot delete PostGIS rows")

    with psycopg2.connect(db_conn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cog_index WHERE collection_id = %s", (collection_id,))
            return cur.rowcount


def delete_collection_jobs(collection_id):
    with _JOBS_LOCK:
        deleted_job_ids = [
            job_id for job_id, job in _JOBS.items()
            if job.get("collection_id") == collection_id and job.get("status") != "running"
        ]
        for job_id in deleted_job_ids:
            _JOBS.pop(job_id, None)
    return deleted_job_ids


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def regenerate_mapfile():
    """Re-run mapfile_generator with current env. Returns the captured stderr."""
    result = subprocess.run(
        ["python3", str(MAPFILE_GENERATOR)],
        check=False, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mapfile_generator failed: {result.stderr or result.stdout}")
    return result.stderr.strip() or result.stdout.strip()


def sync_collections_catalog():
    """Upload collections.json to durable S3 storage when configured.

    Local development leaves COLLECTIONS_S3_URI unset, so scans and admin
    edits behave exactly as before. Deployed Fargate tasks set it because the
    container filesystem is ephemeral across task replacement.
    """
    uri = os.environ.get("COLLECTIONS_S3_URI")
    if not uri:
        return {"enabled": False}

    import boto3
    try:
        without_scheme = uri.removeprefix("s3://")
        bucket, _, key = without_scheme.partition("/")
        boto3.client("s3").upload_file(str(COLLECTIONS_FILE), bucket, key)
    except Exception as exc:
        raise RuntimeError(f"collections catalog S3 sync failed: {exc}")

    return {"enabled": True, "uri": uri, "output": f"Successfully uploaded collections.json to {uri}"}


def _validate_scan_payload(payload):
    required = {"collection_id", "bucket", "prefix"}
    missing = required - set(payload)
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")
    if not _COLLECTION_ID_RE.match(payload["collection_id"]):
        raise ValueError("collection_id must be lowercase kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$")


def start_scan(payload):
    _validate_scan_payload(payload)
    collection_id = payload["collection_id"]

    with _JOBS_LOCK:
        for existing in _JOBS.values():
            if existing["status"] == "running" and existing["collection_id"] == collection_id:
                raise ValueError(f"scan already running for {collection_id}")
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "collection_id": collection_id,
            "status": "running",
            "discovered": None,
            "scanned": 0,
            "failures": 0,
            "log_tail": [],
            "started_at": now_iso(),
            "completed_at": None,
            "error_message": None,
        }
        _JOBS[job_id] = job

    thread = threading.Thread(target=_run_scan, args=(job_id, payload), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "running"}


def _set_job_failed(job, message):
    with _JOBS_LOCK:
        job["status"] = "failed"
        job["error_message"] = message
        job["completed_at"] = now_iso()


def _run_scan(job_id, payload):
    job = _JOBS[job_id]
    collection_id = payload["collection_id"]
    out_native = MAPFILES_DIR / f"{collection_id}_tileindex.fgb"

    cmd = [
        "python3", str(SCAN_SCRIPT),
        "--bucket", payload["bucket"],
        "--prefix", payload["prefix"],
        "--region", payload.get("region", "us-west-2"),
        "--collection", collection_id,
        "--output-native", str(out_native),
        "--collections-file", str(COLLECTIONS_FILE),
        "--label", payload.get("label", collection_id),
        "--attribution", payload.get("attribution", ""),
        "--layer-name", payload.get("layer_name", collection_id),
        "--map-name", payload.get("map_name", f"{collection_id}-imagery"),
        "--draw-order", str(payload.get("draw_order", 10)),
        "--access-mode", payload.get("access_mode", "unsigned"),
        "--enabled", "true" if payload.get("enabled", True) else "false",
    ]
    if payload.get("min_zoom") is not None:
        cmd += ["--min-zoom", str(int(payload["min_zoom"]))]
    if payload.get("max_zoom") is not None:
        cmd += ["--max-zoom", str(int(payload["max_zoom"]))]
    if payload.get("limit"):
        cmd += ["--limit", str(int(payload["limit"]))]
    if payload.get("workers"):
        cmd += ["--workers", str(int(payload["workers"]))]
    if payload.get("requester_pays"):
        cmd.append("--requester-pays")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        _set_job_failed(job, f"failed to spawn scanner: {exc}")
        return

    for line in proc.stdout:
        line = line.rstrip()
        with _JOBS_LOCK:
            job["log_tail"].append(line)
            if len(job["log_tail"]) > 100:
                job["log_tail"] = job["log_tail"][-100:]
            m = _DISCOVERED_RE.search(line)
            if m:
                job["discovered"] = int(m.group(1))
            m = _PROGRESS_RE.search(line)
            if m:
                job["scanned"] = int(m.group(1))
                job["failures"] = int(m.group(3))

    rc = proc.wait()
    if rc != 0:
        _set_job_failed(job, f"scanner exited with code {rc}")
        return

    try:
        gen_output = regenerate_mapfile()
        with _JOBS_LOCK:
            job["log_tail"].append(f"mapfile_generator: {gen_output}")
    except Exception as exc:
        _set_job_failed(job, f"mapfile regeneration failed: {exc}")
        return

    try:
        sync_result = sync_collections_catalog()
        if sync_result.get("enabled"):
            with _JOBS_LOCK:
                job["log_tail"].append(f"collections sync: {sync_result['uri']}")
    except Exception as exc:
        _set_job_failed(job, f"collections catalog sync failed: {exc}")
        return

    try:
        restart_result = restart_mapserver_group()
        with _JOBS_LOCK:
            job["log_tail"].append(f"mapserver restart: {restart_result}")
    except Exception as exc:
        _set_job_failed(job, f"mapserver restart failed: {exc}")
        return

    with _JOBS_LOCK:
        job["status"] = "complete"
        job["completed_at"] = now_iso()


def get_job(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def list_jobs():
    with _JOBS_LOCK:
        return {"jobs": [dict(j) for j in _JOBS.values()]}


def run_benchmark(payload):
    config = viewer_config()
    doc = _read_collections()
    active = _active_collection(doc.get("collections", []))
    if not active:
        raise ValueError("no active collection is configured")
    db_conn = db_connection_string()
    if payload.get("prepareOnly"):
        return prepare_tile_benchmark(payload, active, config, db_conn)
    return run_tile_benchmark(payload, active, config, NGINX_CACHE, db_conn)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/config":
            try:
                write_json(self, 200, admin_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/viewer-config":
            try:
                write_json(self, 200, viewer_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/nginx-cache":
            try:
                write_json(self, 200, nginx_cache_stats())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/environment":
            try:
                write_json(self, 200, environment_info())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/collections":
            try:
                write_json(self, 200, collections_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/parquet-refresh":
            try:
                write_json(self, 200, parquet_refresh_status())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if path == "/jobs":
            write_json(self, 200, list_jobs())
            return
        if path.startswith("/jobs/"):
            job_id = path[len("/jobs/"):]
            job = get_job(job_id)
            if job is None:
                write_error(self, 404, "job not found")
                return
            write_json(self, 200, job)
            return
        write_error(self, 404, "not found")

    def do_POST(self):
        if self.path == "/benchmark":
            if not write_enabled():
                write_error(self, 403, "admin writes are disabled")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = run_benchmark(payload)
                write_json(self, 200, result)
            except ValueError as exc:
                write_error(self, 400, str(exc))
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/collections/scan":
            if not collection_write_enabled():
                write_error(self, 403, "collection writes are disabled")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = start_scan(payload)
                write_json(self, 202, result)
            except ValueError as exc:
                write_error(self, 400, str(exc))
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/parquet-refresh":
            if not write_enabled():
                write_error(self, 403, "admin writes are disabled")
                return
            try:
                write_json(self, 202, start_parquet_refresh())
            except ValueError as exc:
                write_error(self, 400, str(exc))
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        write_error(self, 404, "not found")

    def do_DELETE(self):
        match = re.match(r"^/collections/([^/]+)$", self.path)
        if match:
            self.delete_collection(match.group(1))
            return
        write_error(self, 404, "not found")

    def do_PUT(self):
        if self.path == "/runtime":
            self.update_runtime()
            return
        if self.path == "/collections/active":
            self.update_active_collection()
            return
        # Dynamic path: /collections/{id}/enabled
        match = re.match(r"^/collections/([^/]+)/enabled$", self.path)
        if match:
            self.update_collection_enabled(match.group(1))
            return
        if self.path != "/numprocs":
            write_error(self, 404, "not found")
            return
        if not write_enabled():
            write_error(self, 403, "admin writes are disabled")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = set_numprocs(payload.get("mapserverNumprocs"))
            write_json(self, 200, {**admin_config(), **result})
        except ValueError as exc:
            write_error(self, 400, str(exc))
        except Exception as exc:
            write_error(self, 500, str(exc))

    def delete_collection(self, collection_id):
        if not collection_write_enabled():
            write_error(self, 403, "collection writes are disabled")
            return
        try:
            docs = _read_collections()
            collections = docs.get("collections", [])
            
            target = None
            for i, c in enumerate(collections):
                if c.get("id") == collection_id:
                    target = collections.pop(i)
                    break
                    
            if not target:
                raise ValueError(f"Collection {collection_id} not found")

            deleted_postgis_rows = delete_postgis_collection(collection_id)

            docs["collections"] = collections
            COLLECTIONS_FILE.write_text(json.dumps(docs, indent=2) + "\n")
            catalog_sync = sync_collections_catalog()
            
            rt_config = runtime_config()
            if rt_config.get("activeCollection") == collection_id:
                rt_config["activeCollection"] = ""
                save_runtime_config(rt_config)
            
            deleted_artifacts = []
            for artifact_path in _collection_artifact_paths(target):
                try:
                    artifact_path.unlink(missing_ok=True)
                    deleted_artifacts.append(str(artifact_path))
                except Exception as exc:
                    print(f"WARN: failed to delete {artifact_path}: {exc}", file=sys.stderr)

            deleted_jobs = delete_collection_jobs(collection_id)

            regenerate_mapfile()
            restart_mapserver_group()
            
            write_json(self, 200, {
                **collections_config(),
                "deletedArtifacts": deleted_artifacts,
                "deletedJobs": deleted_jobs,
                "deletedPostgisRows": deleted_postgis_rows,
                "catalogSync": catalog_sync,
            })
        except ValueError as exc:
            write_error(self, 400, str(exc))
        except Exception as exc:
            write_error(self, 500, str(exc))

    def update_active_collection(self):
        if not write_enabled():
            write_error(self, 403, "admin writes are disabled")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            collection_id = payload.get("activeCollection")
            if not isinstance(collection_id, str) or not collection_id.strip():
                raise ValueError("activeCollection is required")
            result = set_active_collection(collection_id.strip())
            write_json(self, 200, {**collections_config(), **result})
        except ValueError as exc:
            write_error(self, 400, str(exc))
        except Exception as exc:
            write_error(self, 500, str(exc))

    def update_collection_enabled(self, collection_id):
        if not collection_write_enabled():
            write_error(self, 403, "collection writes are disabled")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if "enabled" not in payload:
                raise ValueError("enabled is required")
            result = set_collection_enabled(collection_id, payload["enabled"])
            write_json(self, 200, result)
        except ValueError as exc:
            write_error(self, 400, str(exc))
        except Exception as exc:
            write_error(self, 500, str(exc))

    def update_runtime(self):
        if not write_enabled():
            write_error(self, 403, "admin writes are disabled")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if "nginxCacheMaxSizeGb" in payload:
                payload["nginxCacheMaxSize"] = payload["nginxCacheMaxSizeGb"]
            previous = runtime_config()
            current = dict(previous)
            current.update({k: payload[k] for k in DEFAULT_RUNTIME.keys() if k in payload})
            config = save_runtime_config(current)

            mapserver_keys = {"gdalCacheMaxMb", "vsiCacheSizeMb", "mapserverDebug"}
            nginx_keys = {"nginxCacheMaxSize", "nginxCacheTtl", "nginxCache404Ttl"}
            mapserver_changed = any(previous[key] != config[key] for key in mapserver_keys)
            nginx_changed = any(previous[key] != config[key] for key in nginx_keys)

            response = admin_config()
            if mapserver_changed:
                apply_mapfile_runtime(config)
                response["restart"] = restart_mapserver_group()
            if nginx_changed:
                apply_nginx_runtime(config)
                response["nginx"] = reload_nginx()
            write_json(self, 200, response)
        except ValueError as exc:
            write_error(self, 400, str(exc))
        except Exception as exc:
            write_error(self, 500, str(exc))

    def log_message(self, fmt, *args):
        print(f"admin-api: {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    save_runtime_config(runtime_config())
    apply_mapfile_runtime(runtime_config())
    apply_nginx_runtime(runtime_config())
    admin_config()
    server = ThreadingHTTPServer(("127.0.0.1", 9100), Handler)
    print("admin-api: listening on 127.0.0.1:9100", flush=True)
    server.serve_forever()
