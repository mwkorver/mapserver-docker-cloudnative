"""
Custom resource handler that runs DB init SQL idempotently.

Triggered on stack create/update; bump `properties.version` in CDK to force
a re-run. Each statement runs in its own try/except so a missing extension
(e.g. pgstac on RDS variants where it's not pre-built) doesn't block the rest.
"""
import json
import logging
import os

import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EXTENSIONS = [
    "CREATE EXTENSION IF NOT EXISTS postgis",
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    # pgstac is not bundled with stock RDS PostgreSQL. CREATE EXTENSION will
    # fail with "could not open extension control file"; that's expected on a
    # fresh deploy. To install pgstac, run `pypgstac migrate` from a host that
    # can reach the DB (use an SSM tunnel from a bastion or local machine).
    # The other extensions don't depend on it.
    "CREATE EXTENSION IF NOT EXISTS pgstac",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cog_index (
    id            BIGSERIAL PRIMARY KEY,
    collection_id TEXT NOT NULL,
    location      TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    native_epsg   INT  NOT NULL,
    geom          GEOMETRY(Polygon, 3857) NOT NULL,
    geom_native   GEOMETRY NOT NULL,
    uploaded_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (collection_id, location)
);

CREATE INDEX IF NOT EXISTS cog_index_collection_idx  ON cog_index(collection_id);
CREATE INDEX IF NOT EXISTS cog_index_geom_idx        ON cog_index USING GIST(geom);
CREATE INDEX IF NOT EXISTS cog_index_geom_native_idx ON cog_index USING GIST(geom_native);
CREATE INDEX IF NOT EXISTS cog_index_file_name_trgm  ON cog_index USING GIN(file_name gin_trgm_ops);
"""


def _connect():
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        autocommit=True,
        connect_timeout=10,
    )


def _exec(conn, sql, label):
    try:
        conn.execute(sql)
        logger.info("ok: %s", label)
    except psycopg.Error as e:
        logger.warning("skipped %s: %s", label, e)


def main(event, context):
    logger.info("event: %s", json.dumps(event))

    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "db-init")}

    with _connect() as conn:
        for stmt in EXTENSIONS:
            label = stmt.split()[-1]
            _exec(conn, stmt, f"extension {label}")

        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            label = stmt.split("\n")[0][:60]
            _exec(conn, stmt, label)

    return {"PhysicalResourceId": "db-init"}
