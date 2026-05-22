"""
Custom resource handler that runs DB init SQL idempotently.

Triggered on stack create/update; bump `properties.version` in CDK to force
a re-run. Each statement runs in its own try/except so a missing extension
(e.g. pgstac on RDS variants where it's not pre-built) doesn't block the rest.

Retry logic: on a fresh deploy the RDS instance is brand-new. CloudFormation
marks it CREATE_COMPLETE as soon as the DescribeDBInstances status flips to
"available", but the engine can take another 10–30 s before it accepts TCP
connections. The security-group ingress rule (Lambda SG → RDS SG) is also a
separate CloudFormation resource that may be applied concurrently — if the
Lambda fires first, the SYN is silently dropped and psycopg raises
OperationalError("connection timeout expired"). Both races are handled by the
exponential-backoff retry loop below.
"""
import json
import logging
import os
import time

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

# Retry parameters: total wait budget is ~90 s so we stay well inside the
# 2-minute Lambda timeout even after the DB work itself runs.
_MAX_ATTEMPTS = 8
_BASE_DELAY_S = 5


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


def _connect_with_retry():
    """Connect to the DB, retrying with exponential backoff.

    A new RDS instance may not accept connections for 10–30 s after
    CloudFormation marks it available. The security-group ingress rule is also
    applied asynchronously and may not be in place on the first attempt.
    """
    last_exc = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            conn = _connect()
            if attempt > 1:
                logger.info("connected on attempt %d", attempt)
            return conn
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                break
            delay = _BASE_DELAY_S * (2 ** (attempt - 1))  # 5, 10, 20, 40 …
            logger.warning(
                "attempt %d/%d failed (%s); retrying in %ds",
                attempt, _MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    raise last_exc


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

    with _connect_with_retry() as conn:
        for stmt in EXTENSIONS:
            label = stmt.split()[-1]
            _exec(conn, stmt, f"extension {label}")

        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            label = stmt.split("\n")[0][:60]
            _exec(conn, stmt, label)

    return {"PhysicalResourceId": "db-init"}
