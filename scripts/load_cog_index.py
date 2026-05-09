#!/usr/bin/env python3
"""
Bulk-load COG metadata into the cog_index table on the RDS instance.

Reads tiff headers from S3 in parallel (same approach as build_vrt.py), then
INSERTs one row per COG with bbox geometry in EPSG:2193.

Usage:
    1. Start an SSM tunnel to the RDS instance (RDS is not publicly reachable):

        aws ssm start-session \
          --target $(aws ec2 describe-instances --filters \
            'Name=tag:Name,Values=*bastion*' --query \
            'Reservations[0].Instances[0].InstanceId' --output text) \
          --document-name AWS-StartPortForwardingSessionToRemoteHost \
          --parameters '{"host":["<db-endpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}'

       Or use a one-off Fargate run-task with `awsvpc` networking — it can
       reach RDS directly through the VPC. The script reads the DB secret
       via the AWS SDK and connects as if it were any in-VPC client.

    2. Set DB_SECRET_ARN to the RDS secret ARN (CfnOutput: DbSecretArn) and
       optionally DB_HOST to override (e.g. localhost for SSM tunnel).

        export DB_SECRET_ARN=arn:aws:secretsmanager:us-west-2:...
        export DB_HOST=localhost   # if using SSM tunnel
        python3 scripts/load_cog_index.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import psycopg
from osgeo import gdal

# --- config -------------------------------------------------------------
BUCKET = "nz-imagery"
PREFIX = "auckland/auckland_2024_0.075m/rgb/2193/"
VSIS3_BASE = f"/vsis3/{BUCKET}/{PREFIX}"
EPSG = 2193
WORKERS = 32
BATCH = 500

gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
gdal.SetConfigOption("VSI_CACHE", "TRUE")
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "TRUE")


def list_tiffs():
    files = gdal.ReadDir(VSIS3_BASE)
    return [f for f in files if f.endswith(".tiff") or f.endswith(".tif")]


def read_header(file_name):
    path = f"{VSIS3_BASE}{file_name}"
    ds = gdal.Open(path)
    if ds is None:
        return None
    gt = ds.GetGeoTransform()
    width = ds.RasterXSize
    height = ds.RasterYSize
    ds = None

    # bbox in source SRID (EPSG:2193)
    min_x = gt[0]
    max_y = gt[3]
    max_x = gt[0] + gt[1] * width
    min_y = gt[3] + gt[5] * height
    return {
        "location": path,
        "file_name": file_name,
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "res_m": gt[1],
        "width": width,
        "height": height,
    }


def get_dsn():
    secret_arn = os.environ.get("DB_SECRET_ARN")
    if not secret_arn:
        sys.exit("error: set DB_SECRET_ARN")
    secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)[
            "SecretString"
        ]
    )
    host = os.environ.get("DB_HOST", secret["host"])
    port = os.environ.get("DB_PORT", secret["port"])
    return (
        f"host={host} port={port} dbname={secret['dbname']} "
        f"user={secret['username']} password={secret['password']}"
    )


def main():
    print("Listing TIFFs in S3...")
    files = list_tiffs()
    print(f"Found {len(files)} files. Reading headers ({WORKERS} workers)...")

    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(read_header, f): f for f in files}
        for i, fut in enumerate(as_completed(futures)):
            r = fut.result()
            if r:
                rows.append(r)
            if (i + 1) % 1000 == 0:
                print(f"  Read {i + 1}/{len(files)}...")

    print(f"Read {len(rows)} headers. Inserting...")

    with psycopg.connect(get_dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            for start in range(0, len(rows), BATCH):
                batch = rows[start : start + BATCH]
                args = []
                placeholders = []
                for r in batch:
                    placeholders.append(
                        "(%s, %s, ST_MakeEnvelope(%s, %s, %s, %s, %s), %s, %s, %s)"
                    )
                    args.extend(
                        [
                            r["location"],
                            r["file_name"],
                            r["min_x"],
                            r["min_y"],
                            r["max_x"],
                            r["max_y"],
                            EPSG,
                            r["res_m"],
                            r["width"],
                            r["height"],
                        ]
                    )
                cur.execute(
                    f"""
                    INSERT INTO cog_index (location, file_name, geom, res_m, width, height)
                    VALUES {",".join(placeholders)}
                    ON CONFLICT (location) DO NOTHING
                    """,
                    args,
                )
                print(f"  Inserted {start + len(batch)}/{len(rows)}")
        conn.commit()

    print("Done.")


if __name__ == "__main__":
    main()
