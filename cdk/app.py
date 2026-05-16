#!/usr/bin/env python3
import os
import aws_cdk as cdk

from mapserver_stack import MapserverStack

app = cdk.App()

config_bucket = app.node.try_get_context("config_bucket") or "mapserver-docker-cloudnative"
ecr_repo_name = app.node.try_get_context("ecr_repo") or "mapserver-docker-cloudnative"
image_tag = app.node.try_get_context("image_tag") or "latest"
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "us-west-2")

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
