#!/usr/bin/env python3
import os
import json
import aws_cdk as cdk

from mapserver_stack import MapserverStack

app = cdk.App()

region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "us-west-2")
account = os.environ.get("CDK_DEFAULT_ACCOUNT", "")

# Default bucket name is account+region namespaced so it is globally unique
# without any manual -c config_bucket= override. S3 names are global; ECR
# names (ecr_repo_name) are per-account/per-region and need no namespacing.
default_bucket = f"mapserver-docker-cloudnative-{account}-{region}" if account else "mapserver-docker-cloudnative"
config_bucket = app.node.try_get_context("config_bucket") or default_bucket
ecr_repo_name = app.node.try_get_context("ecr_repo") or "mapserver-docker-cloudnative"
image_tag = app.node.try_get_context("image_tag") or "latest"

cpu = int(app.node.try_get_context("mapserver_cpu") or 4096)
memory = int(app.node.try_get_context("mapserver_memory_limit_mib") or 8192)
ephemeral_storage_gib = int(app.node.try_get_context("mapserver_ephemeral_storage_gib") or 21)

task_role_arn = app.node.try_get_context("task_role_arn")
execution_role_arn = app.node.try_get_context("execution_role_arn")
db_backend = str(app.node.try_get_context("db_backend") or "postgis").lower()
if db_backend not in ("postgis", "parquet"):
    raise ValueError("db_backend must be 'postgis' or 'parquet'")

parquet_selections = app.node.try_get_context("parquet_selections") or {}
if isinstance(parquet_selections, str):
    parquet_selections = json.loads(parquet_selections)
parquet_selection_json = json.dumps(parquet_selections, separators=(",", ":"))
parquet_index_uri_template = (
    app.node.try_get_context("parquet_index_uri_template")
    or "s3://cog-stac-viewer-495811053987-us-west-2/lake/"
       "collection=naip/region={state}/year={year}/data_0.parquet"
)
parquet_selections_s3_uri = app.node.try_get_context("parquet_selections_s3_uri")

MapserverStack(
    app,
    "MapserverStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
    config_bucket_name=config_bucket,
    ecr_repo_name=ecr_repo_name,
    image_tag=image_tag,
    cpu=cpu,
    memory=memory,
    ephemeral_storage_gib=ephemeral_storage_gib,
    task_role_arn=task_role_arn,
    execution_role_arn=execution_role_arn,
    db_backend=db_backend,
    parquet_selection_json=parquet_selection_json,
    parquet_index_uri_template=parquet_index_uri_template,
    parquet_selections_s3_uri=parquet_selections_s3_uri,
)

app.synth()
