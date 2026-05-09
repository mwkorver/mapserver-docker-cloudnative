from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_applicationautoscaling as appscaling,
)
from constructs import Construct


class MapserverStack(Stack):
    """
    Cloud-native MapServer on AWS Fargate.

    Resources:
      - VPC (2 AZ, public subnets) with S3 gateway endpoint
      - ECR repo (or reference existing)
      - S3 config bucket (referenced, assumed pre-existing)
      - ECS Fargate cluster + service (ARM64, 1 vCPU / 4 GB)
      - ALB (HTTP:80) with WMS GetCapabilities health check
      - CloudWatch log group + autoscaling on CPU
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config_bucket_name: str,
        ecr_repo_name: str,
        image_tag: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Networking ----------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        # Free; lets Fargate read S3 without traversing the public internet.
        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # --- Storage and registry -----------------------------------------
        config_bucket = s3.Bucket.from_bucket_name(
            self, "ConfigBucket", config_bucket_name
        )

        ecr_repo = ecr.Repository.from_repository_name(
            self, "EcrRepo", ecr_repo_name
        )

        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name="/ecs/mapserver",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- ECS cluster, task, service -----------------------------------
        cluster = ecs.Cluster(self, "Cluster", cluster_name="mapserver", vpc=vpc)

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family="mapserver",
            cpu=1024,
            memory_limit_mib=4096,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # Task role: read-only access to the config bucket (mapfile, VRT,
        # tile_extents) and the COG bucket if it's the same one.
        config_bucket.grant_read(task_def.task_role)

        container = task_def.add_container(
            "mapserver",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, image_tag),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs", log_group=log_group
            ),
            environment={
                "VRT_S3_URI": f"s3://{config_bucket_name}/auckland_2024.vrt",
                "EXTENTS_S3_URI": f"s3://{config_bucket_name}/tile_extents.geojson",
                "MAPFILE_S3_URI": f"s3://{config_bucket_name}/mapfile.map",
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=80))

        service_sg = ec2.SecurityGroup(
            self,
            "ServiceSg",
            vpc=vpc,
            description="mapserver Fargate tasks",
            allow_all_outbound=True,
        )

        service = ecs.FargateService(
            self,
            "Service",
            service_name="mapserver",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[service_sg],
            health_check_grace_period=Duration.seconds(60),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
        )

        # --- Load balancer -------------------------------------------------
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=vpc,
            description="mapserver ALB",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            load_balancer_name="mapserver",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
        )

        listener = alb.add_listener("Http", port=80, open=False)
        listener.add_targets(
            "Mapserver",
            port=80,
            targets=[service],
            health_check=elbv2.HealthCheck(
                path="/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                healthy_http_codes="200",
            ),
            deregistration_delay=Duration.seconds(30),
        )
        service_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(80), "ALB to tasks")

        # --- Autoscaling ---------------------------------------------------
        scaling = service.auto_scale_task_count(min_capacity=1, max_capacity=4)
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=60,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "WmsUrl", value=f"http://{alb.load_balancer_dns_name}/mapserv")
        CfnOutput(self, "EcrRepoUri", value=ecr_repo.repository_uri)
