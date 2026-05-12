"""
One-shot loader for cog_index. Reads the bundled KyFromAbove tile grid
(EPSG:3857 polygons, one per COG) and INSERTs (location, geom).

Invoke after stack is up:
    aws lambda invoke --function-name $(...) /tmp/out.json --region us-west-2

Idempotent on `location` (UNIQUE). Re-runs are safe.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Route through the in-container nginx proxy_cache layer so every FastCGI
# worker shares one disk-backed cache instead of fragmenting per-process
# VSI caches. nginx forwards misses to the real S3 endpoint.
DEFAULT_COG_PROXY = "http://localhost:8001"
BATCH = 500

# Bundled tile grid: EPSG:3857 polygons, `key` is the S3 path under the
# kyfromabove bucket (e.g. "imagery/orthos/Phase3/.../file.tif").
EXTENTS_PATH = Path(__file__).with_name("ky_2024_grid.geojson")


def _connect():
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=10,
    )


def _features():
    return json.loads(EXTENTS_PATH.read_text())["features"]


def main(event: dict[str, Any], context):
    cog_proxy = event.get("proxy", DEFAULT_COG_PROXY)
    vsi_base = f"/vsicurl/{cog_proxy.rstrip('/')}"

    logger.info("Loading bundled %s, vsi base %s", EXTENTS_PATH, vsi_base)

    features = _features()
    logger.info("Parsed %d features", len(features))

    inserted = 0
    skipped = 0
    with _connect() as conn, conn.cursor() as cur:
        for start in range(0, len(features), BATCH):
            batch = features[start : start + BATCH]
            placeholders = []
            args: list[Any] = []
            for f in batch:
                key = f.get("properties", {}).get("key")
                geom = f.get("geometry")
                if not key or not geom:
                    skipped += 1
                    continue
                location = f"{vsi_base}/{key.lstrip('/')}"
                file_name = key.rsplit("/", 1)[-1]
                placeholders.append(
                    "(%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 3857))"
                )
                args.extend([location, file_name, json.dumps(geom)])

            if not placeholders:
                continue

            cur.execute(
                f"""
                INSERT INTO cog_index (location, file_name, geom)
                VALUES {",".join(placeholders)}
                ON CONFLICT (location) DO NOTHING
                """,
                args,
            )
            inserted += cur.rowcount
            logger.info("Inserted %d / %d", inserted, len(features))
        conn.commit()

    return {
        "inserted": inserted,
        "skipped": skipped,
        "total_features": len(features),
        "vsi_base": vsi_base,
    }
