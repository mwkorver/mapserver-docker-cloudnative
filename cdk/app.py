#!/usr/bin/env python3
import os
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
)

app.synth()
