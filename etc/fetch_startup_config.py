#!/usr/bin/env python3
"""
Startup helper that replaces AWS CLI calls in entrypoint.sh.
Uses boto3 (~10 MB installed) instead of the full AWS CLI (~400 MB).

Usage:
    python3 fetch_startup_config.py s3-download <s3://bucket/key> <local-dest>
    python3 fetch_startup_config.py db-secret <secret-arn> [region]

For db-secret, prints shell export statements for eval:
    eval "$(python3 /etc/fetch_startup_config.py db-secret $DB_SECRET_ARN)"
"""
import json
import os
import shlex
import sys


def s3_download(uri, dest):
    import boto3
    without_scheme = uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    boto3.client("s3").download_file(bucket, key, dest)


def db_secret(arn, region):
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=arn)
    secret = json.loads(response["SecretString"])
    for env_key, secret_key in [
        ("DB_HOST", "host"),
        ("DB_PORT", "port"),
        ("DB_NAME", "dbname"),
        ("DB_USER", "username"),
        ("DB_PASS", "password"),
    ]:
        print(f"export {env_key}={shlex.quote(str(secret.get(secret_key, '')))}")


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "s3-download":
        if len(sys.argv) < 4:
            print("usage: fetch_startup_config.py s3-download <uri> <dest>", file=sys.stderr)
            sys.exit(1)
        try:
            s3_download(sys.argv[2], sys.argv[3])
        except Exception as exc:
            print(f"ERROR: s3-download failed: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "db-secret":
        if len(sys.argv) < 3:
            print("usage: fetch_startup_config.py db-secret <arn> [region]", file=sys.stderr)
            sys.exit(1)
        region = (
            sys.argv[3] if len(sys.argv) > 3
            else os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        )
        try:
            db_secret(sys.argv[2], region)
        except Exception as exc:
            print(f"ERROR: db-secret failed: {exc}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
