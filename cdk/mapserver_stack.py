from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    Tags,
    BundlingOptions,
    CustomResource,
    aws_budgets as budgets,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_rds as rds,
    aws_s3 as s3,
    aws_applicationautoscaling as appscaling,
    custom_resources as cr,
)
from constructs import Construct


class MapserverStack(Stack):
    """
    Cloud-native MapServer on AWS Fargate, backed by RDS PostgreSQL/PostGIS.

    Resources:
      - VPC (2 AZ, public subnets) with S3 gateway endpoint
      - ECR repo (referenced)
      - S3 config bucket (referenced)
      - RDS PostgreSQL db.t4g.micro with PostGIS, pg_stat_statements, pg_trgm
      - DB init Lambda (custom resource): enables extensions, creates schema
      - ECS Fargate cluster + service (ARM64, 4 vCPU / 8 GB)
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
        cpu: int = 4096,
        memory: int = 8192,
        ephemeral_storage_gib: int = 21,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # `cdk deploy -c parked=true` scales Fargate to 0 and stops RDS.
        # Default is unparked (running). Re-deploy without the flag to wake.
        parked_ctx = self.node.try_get_context("parked")
        parked = str(parked_ctx).lower() in ("true", "1", "yes")
        mapserver_numprocs = str(self.node.try_get_context("mapserver_numprocs") or "6")
        cost_tag_key = "Project"
        cost_tag_value = "mapserver-docker-cloudnative"
        Tags.of(self).add(cost_tag_key, cost_tag_value)
        Tags.of(self).add("awsApplication", construct_id)

        monthly_budget_usd = float(self.node.try_get_context("monthly_budget_usd") or 0)
        budget_email = self.node.try_get_context("budget_email")
        if monthly_budget_usd > 0 and budget_email:
            budgets.CfnBudget(
                self,
                "StackMonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name=f"{construct_id}-monthly",
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(
                        amount=monthly_budget_usd,
                        unit="USD",
                    ),
                    cost_types=budgets.CfnBudget.CostTypesProperty(
                        include_credit=False,
                        include_discount=True,
                        include_other_subscription=True,
                        include_recurring=True,
                        include_refund=False,
                        include_subscription=True,
                        include_support=True,
                        include_tax=True,
                        include_upfront=True,
                        use_amortized=False,
                        use_blended=False,
                    ),
                    filter_expression=budgets.CfnBudget.ExpressionProperty(
                        tags=budgets.CfnBudget.TagValuesProperty(
                            key=cost_tag_key,
                            match_options=["EQUALS"],
                            values=[cost_tag_value],
                        ),
                    ),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type="ACTUAL",
                            threshold=80,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(
                                address=str(budget_email),
                                subscription_type="EMAIL",
                            )
                        ],
                    ),
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type="FORECASTED",
                            threshold=100,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(
                                address=str(budget_email),
                                subscription_type="EMAIL",
                            )
                        ],
                    ),
                ],
            )

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
        imagery_bucket = s3.Bucket.from_bucket_name(
            self, "ImageryBucket", "kyfromabove"
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

        # --- RDS PostgreSQL + PostGIS -------------------------------------
        # Custom parameter group enables pg_stat_statements via shared_preload_libraries
        db_params = rds.ParameterGroup(
            self,
            "DbParams",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_17_2,
            ),
            parameters={
                "shared_preload_libraries": "pg_stat_statements",
                "track_activity_query_size": "4096",
            },
        )

        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            description="RDS PostgreSQL, in-VPC only",
            allow_all_outbound=False,
        )

        db = rds.DatabaseInstance(
            self,
            "Db",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_17_2,
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON,
                ec2.InstanceSize.MICRO,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            publicly_accessible=False,
            allocated_storage=20,
            storage_type=rds.StorageType.GP3,
            backup_retention=Duration.days(1),
            delete_automated_backups=True,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            credentials=rds.Credentials.from_generated_secret("postgres"),
            database_name="mapserver",
            parameter_group=db_params,
            security_groups=[db_sg],
        )

        # --- DB init Lambda (custom resource) -----------------------------
        init_sg = ec2.SecurityGroup(
            self,
            "DbInitSg",
            vpc=vpc,
            description="DB init lambda",
            allow_all_outbound=True,
        )
        db.connections.allow_default_port_from(init_sg, "DB init lambda")

        init_fn = lambda_.Function(
            self,
            "DbInit",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.main",
            code=lambda_.Code.from_asset(
                "lambda/db_init",
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                    ],
                ),
            ),
            # 5 min: retry budget is ~90 s (8 attempts × backoff); give headroom.
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[init_sg],
            allow_public_subnet=True,
            environment={
                # Pass secret values directly — Lambda in VPC has no NAT and
                # no Secrets Manager endpoint, so it cannot call the API.
                # CloudFormation resolves these at deploy time.
                "DB_HOST": db.instance_endpoint.hostname,
                "DB_PORT": str(db.instance_endpoint.port),
                "DB_NAME": "mapserver",
                "DB_USER": db.secret.secret_value_from_json("username").unsafe_unwrap(),
                "DB_PASSWORD": db.secret.secret_value_from_json("password").unsafe_unwrap(),
            },
        )

        # Ensure the Lambda function is not invoked until the RDS instance and
        # its security-group ingress rule are both in place. Without this,
        # CloudFormation may fire the custom resource trigger while the SG rule
        # is still being applied in parallel, causing a connection timeout.
        init_fn.node.add_dependency(db)

        provider = cr.Provider(self, "DbInitProvider", on_event_handler=init_fn)
        # Bump `version` to force the custom resource to re-run on a deploy
        # (e.g., after editing INIT_SQL in the lambda).
        CustomResource(
            self,
            "DbInitTrigger",
            service_token=provider.service_token,
            properties={"version": "6"},  # retry logic + explicit DB dependency
        )

        # COG loading is no longer a one-shot lambda. The admin scan flow
        # (admin_api.py → scan_cog_collection.py, running in the Fargate
        # task) populates cog_index per-collection when DB_HOST is set.

        # --- Perf API (Lambda Function URL) -------------------------------
        # Returns the most recent 100 WMS GetMap requests for the viewer's
        # in-page performance panel. No VPC — the Lambda only talks to
        # CloudWatch Logs Insights via the public regional endpoint.
        perf_fn = lambda_.Function(
            self,
            "PerfApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambda/perf_api"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "LOG_GROUP": log_group.log_group_name,
                "WINDOW_HOURS": "1",
            },
        )
        perf_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:StartQuery",
                    "logs:GetQueryResults",
                    "logs:StopQuery",
                ],
                resources=[
                    log_group.log_group_arn,
                    f"{log_group.log_group_arn}:*",
                ],
            )
        )

        perf_url = perf_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.GET],
                allowed_headers=["*"],
            ),
        )
        CfnOutput(self, "PerfApiUrl", value=perf_url.url)

        # --- ECS cluster, task, service -----------------------------------
        cluster = ecs.Cluster(self, "Cluster", cluster_name="mapserver", vpc=vpc)

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family="mapserver",
            cpu=cpu,
            memory_limit_mib=memory,
            ephemeral_storage_gib=ephemeral_storage_gib,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # Task role: read/write S3 for durable config, read-only imagery,
        # plus read the DB secret.
        # GDAL still reads /vsicurl/http://localhost:8001/...; nginx handles
        # range-aware cache lookup, then the local signer uses this task role
        # only on cache misses against private S3.
        config_bucket.grant_read_write(task_def.task_role, "config/*")
        imagery_bucket.grant_read(task_def.task_role, "*")
        db.secret.grant_read(task_def.task_role)

        # Allow the scanner to list and read any S3 bucket.
        # Cross-account S3 access (public-dataset and requester-pays buckets)
        # requires an explicit ALLOW in the IAM identity policy even when the
        # bucket policy grants broad access — IAM evaluates both, and an
        # implicit deny on the identity side wins.  The scanner may target any
        # public-data bucket (NAIP, NLCD, Copernicus, …) so we grant s3:Get*
        # and s3:ListBucket on *.  This is read-only and scoped to S3 only.
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectTagging",
                         "s3:GetBucketLocation", "s3:ListBucket"],
                resources=["*"],
            )
        )

        container = task_def.add_container(
            "mapserver",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, image_tag),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs", log_group=log_group
            ),
            environment={
                # No MAPFILE_S3_URI: mapfile is derived at startup from the
                # collections.json loaded from S3 by /etc/entrypoint.sh.
                # S3 is the durable source of truth; bundled collections.json
                # is only a first-run seed when config/collections.json is
                # not present yet.
                "DB_SECRET_ARN": db.secret.secret_arn,
                "AWS_REGION": self.region,
                "COLLECTIONS_S3_URI": f"s3://{config_bucket_name}/config/collections.json",
                "S3_BUCKET": "kyfromabove",
                "S3_REGION": self.region,
                "S3_SIGNING": "required",
                "MAPSERVER_NUMPROCS": mapserver_numprocs,
                # Allow admin UI writes in the deployed stack so the user can
                # add collections via the web UI. Lock down later if exposed.
                "ADMIN_WRITE_ENABLED": "true",
                "FARGATE_CPU": str(cpu),
                "FARGATE_MEMORY": str(memory),
                "FARGATE_EPHEMERAL_STORAGE_GIB": str(ephemeral_storage_gib),
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
        db.connections.allow_default_port_from(service_sg, "Fargate to DB")

        service = ecs.FargateService(
            self,
            "Service",
            service_name="mapserver",
            cluster=cluster,
            task_definition=task_def,
            desired_count=0 if parked else 1,
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
        container.add_environment("PUBLIC_HOST", alb.load_balancer_dns_name)

        # --- Autoscaling ---------------------------------------------------
        # Skipped when parked — registering a target with min_capacity=1
        # would override desired_count=0.
        if not parked:
            scaling = service.auto_scale_task_count(min_capacity=1, max_capacity=4)
            scaling.scale_on_cpu_utilization(
                "CpuScaling",
                target_utilization_percent=60,
                scale_in_cooldown=Duration.seconds(60),
                scale_out_cooldown=Duration.seconds(60),
            )

        # --- RDS stop/start when parked -----------------------------------
        # AWS RDS allows stopping a single-AZ instance for up to 7 days; it
        # auto-restarts after that. The custom resource fires on every deploy
        # and ignores the "already in this state" error from RDS.
        rds_action = "stopDBInstance" if parked else "startDBInstance"
        cr.AwsCustomResource(
            self,
            "RdsState",
            on_create=cr.AwsSdkCall(
                service="RDS",
                action=rds_action,
                parameters={"DBInstanceIdentifier": db.instance_identifier},
                physical_resource_id=cr.PhysicalResourceId.of("rds-state"),
                ignore_error_codes_matching="InvalidDBInstanceState",
            ),
            on_update=cr.AwsSdkCall(
                service="RDS",
                action=rds_action,
                parameters={"DBInstanceIdentifier": db.instance_identifier},
                physical_resource_id=cr.PhysicalResourceId.of("rds-state"),
                ignore_error_codes_matching="InvalidDBInstanceState",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["rds:StopDBInstance", "rds:StartDBInstance"],
                        resources=["*"],
                    ),
                ]
            ),
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "WmsUrl", value=f"http://{alb.load_balancer_dns_name}/mapserv")
        CfnOutput(self, "EcrRepoUri", value=ecr_repo.repository_uri)
        CfnOutput(self, "DbEndpoint", value=db.instance_endpoint.hostname)
        CfnOutput(self, "DbSecretArn", value=db.secret.secret_arn)
        CfnOutput(self, "DbInstanceId", value=db.instance_identifier)
