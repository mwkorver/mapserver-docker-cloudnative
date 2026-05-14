#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SUPERVISOR_CONF = Path("/etc/supervisor/conf.d/supervisord.conf")
ADMIN_CONFIG = Path("/usr/src/admin/config.json")
RUNTIME_CONFIG = Path("/usr/src/admin/runtime_config.json")
VIEWER_CONFIG = Path("/usr/src/viewer/config.json")
MAPFILE = Path("/usr/src/mapfiles/mapfile.map")
COLLECTIONS_FILE = Path("/usr/src/mapfiles/collections.json")
MAPFILES_DIR = Path("/usr/src/mapfiles")
SCAN_SCRIPT = Path("/usr/src/scripts/scan_cog_collection.py")
MAPFILE_GENERATOR = Path("/etc/mapfile_generator.py")
NGINX_CONF = Path("/etc/nginx/sites-available/mapserver_proxy.conf")
NGINX_CACHE_CONF = Path("/etc/nginx/conf.d/cog_cache.conf")
NGINX_CACHE = Path("/var/cache/nginx/cog")
MAX_WORKERS = int(os.environ.get("MAPSERVER_MAX_NUMPROCS", "32"))
DEFAULT_RUNTIME = {
    "gdalCacheMaxMb": 128,
    "vsiCacheSizeMb": 32,
    "mapserverDebug": False,
    "imageryMinZoom": 18,
    "nginxCacheMaxSize": "20g",
    "nginxCacheTtl": "24h",
    "nginxCache404Ttl": "1m",
}
STATIC_NGINX_CACHE_SETTINGS = {
    "cacheKey": "$request_method:$request_uri:$http_range",
    "proxyCacheConvertHead": "off",
    "proxyCacheLock": "on",
    "cacheStatuses": "200 206",
    "headAndGetSeparate": True,
    "upstream": "http://127.0.0.1:9000",
}
DEFAULT_COLLECTIONS = {
    "collections": [
        {
            "collectionName": "ky-2024-3in",
            "layerName": "ky-2024",
            "enabled": True,
            "minZoom": 18,
            "maxZoom": 22,
            "drawOrder": 20,
            "status": "example COG collection",
            "sources": [
                {
                    "label": "KyFromAbove 2024 Season 1 3IN",
                    "bucket": "kyfromabove",
                    "prefix": "imagery/orthos/Phase3/KY_KYAPED_2024_Season1_3IN/",
                    "region": "us-west-2",
                    "accessMode": "unsigned",
                    "requesterPays": False,
                }
            ],
        },
        {
            "collectionName": "nj-2020-1ft",
            "layerName": "nj-2020",
            "enabled": False,
            "minZoom": 14,
            "maxZoom": 22,
            "drawOrder": 10,
            "status": "example COG collection",
            "sources": [
                {
                    "label": "NJ 2020 COGs",
                    "bucket": "njogis-imagery",
                    "prefix": "2020/cog/",
                    "region": "us-west-2",
                    "accessMode": "unsigned",
                    "requesterPays": False,
                }
            ],
        },
        {
            "collectionName": "naip-ca-2022-rgb",
            "layerName": "naip-ca-2022",
            "enabled": False,
            "minZoom": 12,
            "maxZoom": 20,
            "drawOrder": 5,
            "status": "example requester-pays source",
            "sources": [
                {
                    "label": "NAIP California 2022 RGB COGs",
                    "bucket": "naip-visualization",
                    "prefix": "ca/2022/60cm/rgb_cog/",
                    "region": "us-west-2",
                    "accessMode": "requester-pays",
                    "requesterPays": True,
                }
            ],
        },
    ]
}
VIEWER_COLLECTIONS = {
    "ky-2024-3in": {
        "collectionName": "ky-2024-3in",
        "mapName": "ky-imagery",
        "layerName": "ky-2024",
        "footprintUrl": "/ky_20x20_tileindex.geojson",
        "bounds": [[37.81, -85.51], [38.08, -85.25]],
        "center": [37.937085, -85.372578],
        "imageryMinZoom": 18,
        "attribution": "KyFromAbove / Commonwealth of Kentucky",
    },
    "nj-2020-1ft": {
        "collectionName": "nj-2020-1ft",
        "mapName": "nj-imagery",
        "layerName": "nj-2020",
        "footprintUrl": "/nj_2020_footprints_3857.geojson",
        "bounds": [[38.87, -75.6], [41.36, -73.88]],
        "center": [40.1, -74.7],
        "imageryMinZoom": 14,
        "attribution": "NJ Office of GIS / NJGIN",
    },
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


def current_numprocs():
    conf = SUPERVISOR_CONF.read_text()
    match = re.search(r"^numprocs=(\d+)$", conf, re.MULTILINE)
    if not match:
        raise RuntimeError("numprocs not found in supervisor config")
    return int(match.group(1))


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
    result["gdalCacheMaxMb"] = int(result["gdalCacheMaxMb"])
    result["vsiCacheSizeMb"] = int(result["vsiCacheSizeMb"])
    result["imageryMinZoom"] = int(result["imageryMinZoom"])
    result["mapserverDebug"] = bool(result["mapserverDebug"])
    result["nginxCacheMaxSize"] = validate_nginx_size(result["nginxCacheMaxSize"], "nginxCacheMaxSize")
    result["nginxCacheTtl"] = validate_nginx_ttl(result["nginxCacheTtl"], "nginxCacheTtl")
    result["nginxCache404Ttl"] = validate_nginx_ttl(result["nginxCache404Ttl"], "nginxCache404Ttl")
    if result["gdalCacheMaxMb"] < 0 or result["gdalCacheMaxMb"] > 65536:
        raise ValueError("gdalCacheMaxMb must be between 0 and 65536")
    if result["vsiCacheSizeMb"] < 0 or result["vsiCacheSizeMb"] > 65536:
        raise ValueError("vsiCacheSizeMb must be between 0 and 65536")
    if result["imageryMinZoom"] < 0 or result["imageryMinZoom"] > 22:
        raise ValueError("imageryMinZoom must be between 0 and 22")
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
    VIEWER_CONFIG.write_text(json.dumps({"imageryMinZoom": config["imageryMinZoom"]}, indent=2) + "\n")
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
    map_text = replace_or_insert_config(map_text, "VSI_CACHE", "TRUE")
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
    config = {
        "mapserverNumprocs": current_numprocs(),
        "maxMapserverNumprocs": MAX_WORKERS,
        "writeEnabled": write_enabled(),
        "fargateCpu": os.environ.get("FARGATE_CPU", "4096"),
        "fargateMemory": os.environ.get("FARGATE_MEMORY", "8192"),
        "s3Signing": os.environ.get("S3_SIGNING", "auto"),
        "staticNginxCacheSettings": STATIC_NGINX_CACHE_SETTINGS,
        **runtime,
    }
    ADMIN_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    return config


def collections_config():
    active_collection = os.environ.get("LOCAL_COLLECTION", "ky-2024-3in")
    collections = []
    for collection in DEFAULT_COLLECTIONS["collections"]:
        item = dict(collection)
        item["enabled"] = item["collectionName"] == active_collection
        item["status"] = "active local source" if item["enabled"] else item["status"]
        collections.append(item)
    return {"activeCollection": active_collection, "collections": collections}


def viewer_config():
    active_collection = os.environ.get("LOCAL_COLLECTION", "ky-2024-3in")
    config = dict(VIEWER_COLLECTIONS.get(active_collection, VIEWER_COLLECTIONS["ky-2024-3in"]))
    runtime = runtime_config()
    if "IMAGERY_MIN_ZOOM" in os.environ:
        config["imageryMinZoom"] = runtime["imageryMinZoom"]
    return config


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
# _JOBS_LOCK. State is in-memory (lost on container restart) — acceptable
# for now since the artifacts (geojson + collections.json) are durable.

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_COLLECTION_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DISCOVERED_RE = re.compile(r"Discovered (\d+) GeoTIFFs")
_PROGRESS_RE = re.compile(r"Scanned (\d+)/(\d+); failures=(\d+)")


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
    out_native = MAPFILES_DIR / f"{collection_id}_tileindex.geojson"
    out_web = MAPFILES_DIR / f"{collection_id}_footprints_3857.geojson"

    cmd = [
        "python3", str(SCAN_SCRIPT),
        "--bucket", payload["bucket"],
        "--prefix", payload["prefix"],
        "--region", payload.get("region", "us-west-2"),
        "--collection", collection_id,
        "--output-native", str(out_native),
        "--output-web", str(out_web),
        "--collections-file", str(COLLECTIONS_FILE),
        "--label", payload.get("label", collection_id),
        "--attribution", payload.get("attribution", ""),
        "--layer-name", payload.get("layer_name", collection_id),
        "--map-name", payload.get("map_name", f"{collection_id}-imagery"),
        "--min-zoom", str(payload.get("min_zoom", 14)),
        "--max-zoom", str(payload.get("max_zoom", 22)),
        "--draw-order", str(payload.get("draw_order", 10)),
        "--access-mode", payload.get("access_mode", "unsigned"),
        "--enabled", "true" if payload.get("enabled", True) else "false",
    ]
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/config":
            try:
                write_json(self, 200, admin_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/viewer-config":
            try:
                write_json(self, 200, viewer_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/nginx-cache":
            try:
                write_json(self, 200, nginx_cache_stats())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/collections":
            try:
                write_json(self, 200, collections_config())
            except Exception as exc:
                write_error(self, 500, str(exc))
            return
        if self.path == "/jobs":
            write_json(self, 200, list_jobs())
            return
        if self.path.startswith("/jobs/"):
            job_id = self.path[len("/jobs/"):]
            job = get_job(job_id)
            if job is None:
                write_error(self, 404, "job not found")
                return
            write_json(self, 200, job)
            return
        write_error(self, 404, "not found")

    def do_POST(self):
        if self.path == "/collections/scan":
            if not write_enabled():
                write_error(self, 403, "admin writes are disabled")
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
        write_error(self, 404, "not found")

    def do_PUT(self):
        if self.path != "/numprocs":
            if self.path == "/runtime":
                self.update_runtime()
                return
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
