"""
One-shot loader: read tile_extents.geojson from S3, INSERT into cog_index.

Invoke manually after stack deploy:

    aws lambda invoke --function-name $(...) /tmp/out.json --region us-west-2 \\
        --cli-binary-format raw-in-base64-out \\
        --payload '{"prefix":"auckland/auckland_2024_0.075m/rgb/2193/","bucket":"nz-imagery"}'

Idempotent on `location` (UNIQUE) — re-runs are safe.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_COG_PREFIX = "auckland/auckland_2024_0.075m/rgb/2193/"
# Route through the in-container nginx proxy_cache layer so every FastCGI
# worker shares one disk-backed cache instead of fragmenting per-process
# VSI caches. nginx forwards misses to the real S3 endpoint.
DEFAULT_COG_PROXY = "http://localhost:8001"
BATCH = 500

# tile_extents.geojson is bundled into the Lambda asset to avoid a VPC
# round-trip to S3 (the Lambda runs in a private-subnet-style config).
EXTENTS_PATH = Path(__file__).with_name("tile_extents.geojson")


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
    cog_prefix = event.get("prefix", DEFAULT_COG_PREFIX)
    cog_proxy = event.get("proxy", DEFAULT_COG_PROXY)
    base = f"/vsicurl/{cog_proxy}/{cog_prefix}"

    logger.info("Loading bundled %s, vsi base %s", EXTENTS_PATH, base)

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
                file_name = f.get("properties", {}).get("file")
                geom = f.get("geometry")
                if not file_name or not geom:
                    skipped += 1
                    continue
                location = base + file_name
                placeholders.append(
                    "(%s, %s, "
                    "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 2193))"
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
        "vsi_base": base,
    }
