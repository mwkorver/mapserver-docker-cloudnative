#!/usr/bin/env python3
"""Atomically refresh the active GeoParquet selection generation."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from prepare_parquet_backend import parse_selections, split_s3_uri


MAPFILES_DIR = Path(os.environ.get("MAPFILES_DIR", "/usr/src/mapfiles"))
COLLECTIONS_FILE = Path(
    os.environ.get("COLLECTIONS_FILE", str(MAPFILES_DIR / "collections.json"))
)
MAPFILE = Path(os.environ.get("MAPFILE_OUTPUT", str(MAPFILES_DIR / "mapfile.map")))
GENERATIONS_DIR = Path(
    os.environ.get(
        "PARQUET_GENERATIONS_DIR", str(MAPFILES_DIR / "parquet-generations")
    )
)
STATUS_FILE = Path(
    os.environ.get(
        "PARQUET_REFRESH_STATUS_FILE", str(MAPFILES_DIR / "parquet-refresh.json")
    )
)
LOCK_FILE = Path(
    os.environ.get("PARQUET_REFRESH_LOCK_FILE", "/tmp/parquet-refresh.lock")
)


def selection_document():
    pointer_uri = os.environ.get("PARQUET_SELECTION_S3_URI", "").strip()
    if pointer_uri:
        import boto3

        bucket, key = split_s3_uri(pointer_uri)
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read().decode("utf-8")
        source = {
            "type": "s3",
            "uri": pointer_uri,
            "etag": str(response.get("ETag", "")).strip('"'),
            "versionId": response.get("VersionId"),
        }
    else:
        raw = os.environ.get("PARQUET_SELECTION_JSON", "")
        source = {"type": "environment"}

    # Validate before downloading any indexes. Keep the original accepted
    # object/array shape for prepare_parquet_backend.py.
    parse_selections(raw)
    return raw, source


def run_checked(command, env=None):
    result = subprocess.run(
        command,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed: {result.stderr or result.stdout}"
        )
    return (result.stderr or result.stdout).strip()


def supervisor(action):
    return run_checked(["supervisorctl", action, "mapserver:"])


def generation_paths(doc):
    paths = set()
    root = GENERATIONS_DIR.resolve()
    for collection in doc.get("collections", []):
        for item in collection.get("tileindexes") or []:
            value = item.get("tileindex")
            if not value:
                continue
            try:
                path = Path(value).resolve()
            except OSError:
                continue
            if root in path.parents:
                paths.add(path.parent)
    return paths


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_status(payload):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    candidate = STATUS_FILE.with_suffix(".tmp")
    candidate.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(candidate, STATUS_FILE)


def refresh(restart_workers):
    raw, source = selection_document()
    generation_id = uuid.uuid4().hex
    generation_dir = GENERATIONS_DIR / generation_id
    candidate_collections = MAPFILES_DIR / f".collections-{generation_id}.json"
    candidate_mapfile = MAPFILES_DIR / f".mapfile-{generation_id}.map"
    backup_collections = MAPFILES_DIR / f".collections-{generation_id}.bak"
    backup_mapfile = MAPFILES_DIR / f".mapfile-{generation_id}.bak"
    old_catalog = read_json(COLLECTIONS_FILE)
    old_generation_paths = generation_paths(old_catalog)
    workers_stopped = False
    collections_replaced = False
    mapfile_replaced = False

    generation_dir.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env.update(
        {
            "PARQUET_SELECTION_JSON": raw,
            "PARQUET_INDEX_DIR": str(generation_dir),
            "COLLECTIONS_FILE": str(candidate_collections),
        }
    )
    try:
        prepare_output = run_checked(
            ["python3", "/etc/prepare_parquet_backend.py"], env=env
        )
        map_env = env.copy()
        map_env["MAPFILE_OUTPUT"] = str(candidate_mapfile)
        map_output = run_checked(["python3", "/etc/mapfile_generator.py"], env=map_env)

        if restart_workers:
            stop_output = supervisor("stop")
            workers_stopped = True
        else:
            stop_output = ""

        COLLECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if COLLECTIONS_FILE.exists():
            os.replace(COLLECTIONS_FILE, backup_collections)
        if MAPFILE.exists():
            os.replace(MAPFILE, backup_mapfile)
        os.replace(candidate_collections, COLLECTIONS_FILE)
        collections_replaced = True
        os.replace(candidate_mapfile, MAPFILE)
        mapfile_replaced = True

        if restart_workers:
            start_output = supervisor("start")
            workers_stopped = False
        else:
            start_output = ""

        backup_collections.unlink(missing_ok=True)
        backup_mapfile.unlink(missing_ok=True)

        for old_path in old_generation_paths:
            if old_path != generation_dir.resolve():
                shutil.rmtree(old_path, ignore_errors=True)

        result = {
            "generation": generation_id,
            "selectionSource": source,
            "collections": len(read_json(COLLECTIONS_FILE).get("collections", [])),
            "prepare": prepare_output,
            "mapfile": map_output,
            "mapserverStop": stop_output,
            "mapserverStart": start_output,
        }
        write_status({"status": "ready", **result})
        return result
    except Exception as exc:
        candidate_collections.unlink(missing_ok=True)
        candidate_mapfile.unlink(missing_ok=True)
        if backup_collections.exists():
            os.replace(backup_collections, COLLECTIONS_FILE)
        elif collections_replaced:
            COLLECTIONS_FILE.unlink(missing_ok=True)
        if backup_mapfile.exists():
            os.replace(backup_mapfile, MAPFILE)
        elif mapfile_replaced:
            MAPFILE.unlink(missing_ok=True)
        shutil.rmtree(generation_dir, ignore_errors=True)
        if workers_stopped:
            try:
                supervisor("start")
            except Exception as restart_exc:
                raise RuntimeError(
                    f"{exc}; additionally failed to restart MapServer: {restart_exc}"
                ) from exc
        write_status(
            {
                "status": "failed",
                "generation": generation_id,
                "selectionSource": source,
                "error": str(exc),
            }
        )
        raise


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "startup"
    if command not in ("startup", "refresh"):
        raise ValueError("usage: parquet_refresh.py [startup|refresh]")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("a GeoParquet refresh is already running") from exc
        result = refresh(restart_workers=command == "refresh")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: GeoParquet refresh failed: {exc}", file=sys.stderr)
        sys.exit(1)
