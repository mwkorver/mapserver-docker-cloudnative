# CDK — mapserver infrastructure

Provisions the AWS resources to run the shared MapServer/GDAL container on
Fargate behind an ALB. The deployment selects either PostGIS or GeoParquet as
its spatial-index backend.

## What it creates

| Resource          | Notes                                              |
| ----------------- | -------------------------------------------------- |
| VPC + S3 endpoint | 2 AZ, public subnets only, free S3 gateway endpoint |
| ECS cluster       | `mapserver` (Fargate)                              |
| Task definition   | ARM64, 4 vCPU / 8 GB, image from ECR `:latest`. Uses imported pre-created IAM roles. |
| Service           | 1 task baseline, autoscales 1→4 on 60% CPU         |
| ALB               | HTTP:80, WMS GetCapabilities health check          |
| RDS PostgreSQL    | Created only for `db_backend=postgis`. PostGIS schema is initialized by the `DbInit` custom resource. |
| Log group         | `/ecs/mapserver`, 30-day retention                 |
| Perf API          | Small Lambda + Function URL that surfaces recent WMS GetMap stats from CloudWatch Logs Insights for the viewer's in-page performance panel. |
| AWS Budgets       | **Opt-in.** Set `-c monthly_budget_usd=N -c budget_email=…` to create a monthly budget filtered to the stack's `Project=mapserver-docker-cloudnative` cost-allocation tag. |
| Stack-wide tags   | `Project=mapserver-docker-cloudnative` on every resource for cost reporting and the budget filter. |

The S3 config bucket, ECR repo, and IAM Fargate roles are **referenced** (assumed to exist).
Create them out-of-band — they're long-lived and shouldn't be tied to the stack lifecycle, promoting a robust separation of concerns. ECR is also written to by the GitHub Actions workflow. Since S3 buckets and IAM roles are imported/immutable inside the stack, S3 permissions are pre-configured directly on the out-of-band Task role itself. The RDS database secret is created inside the stack, meaning it is mutable, so CDK automatically attaches a resource-based policy to it at deploy time to authorize the imported Task role.

## Prereqs

### 1. Pre-create IAM Roles
Before deploying, the Fargate Task and Execution roles must be created out-of-band so they persist in the account after stack deletion.
Follow the instructions in the [IAM Provisioning Guide](../iam/README.md) to create them.

### 2. Prepare Resources and Environment

```bash
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-west-2

pip install -r requirements.txt
npm install -g aws-cdk             # or use npx cdk
aws ecr create-repository --repository-name mapserver-docker-cloudnative --region $AWS_REGION
aws s3 mb s3://mapserver-docker-cloudnative-${AWS_ACCOUNT}-${AWS_REGION} --region $AWS_REGION
```

ECR repository names are per-account/per-region. S3 bucket names are globally
unique, so the bucket name above is namespaced with your account ID and region.

Push an image so the service can start:

```bash
# from repo root
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin \
    ${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com
```

The build command depends on your machine (Fargate is ARM64):

**Windows or Intel Mac** — cross-compile to arm64 using buildx (included in Docker Desktop):
```bash
docker buildx build --platform linux/arm64 \
  -t ${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/mapserver-docker-cloudnative:latest \
  --push .
```

**Apple Silicon Mac (M1/M2/M3)** — your machine is already arm64, so no cross-compilation needed:
```bash
docker build \
  -t ${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/mapserver-docker-cloudnative:latest \
  --push .
```

> If `docker buildx` or `--push` gives an "unknown flag" error on Mac, buildx isn't wired up as a Docker plugin. Fix it once with:
> ```bash
> mkdir -p ~/.docker/cli-plugins
> ln -sfn $(brew --prefix)/opt/docker-buildx/bin/docker-buildx ~/.docker/cli-plugins/docker-buildx
> ```

The deployed container loads `config/collections.json` from the config bucket
at startup, then generates `mapfile.map` from that catalog. If the S3 object is
missing, the image's bundled `mapfiles/collections.json` is used as a first-run
seed. Admin collection scans/enable/delete operations write the updated catalog
back to `s3://<config-bucket>/config/collections.json` so Fargate task
replacement does not lose collection metadata. Optional escape hatch:
`MAPFILE_S3_URI` will download a hand-written mapfile if set.

## Deploy

Deploying the stack requires the pre-created Task Role ARN (strictly required) and Execution Role ARN (recommended).

```bash
export CDK_DEFAULT_ACCOUNT=$AWS_ACCOUNT
export CDK_DEFAULT_REGION=$AWS_REGION

# Retrieve your pre-created role ARNs
export TASK_ROLE_ARN=$(aws iam get-role --role-name MapserverFargateTaskRole --query "Role.Arn" --output text)
export EXECUTION_ROLE_ARN=$(aws iam get-role --role-name MapserverFargateExecutionRole --query "Role.Arn" --output text)

cdk bootstrap                        # one-time per account/region

cdk diff \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN

cdk deploy \
  -c db_backend=postgis \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN
```

### GeoParquet backend

GeoParquet mode creates no RDS instance or database secret. Provide the active
state/year combinations as CDK context:

```bash
cdk deploy \
  -c db_backend=parquet \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN \
  -c 'parquet_selections={"al":2023,"ct":2021,"tx":2020}'
```

At startup the task downloads only those indexes, rewrites them locally with
MapServer-compatible `location` and `tile_srs` fields, and generates the
mapfile. The admin collection mutation APIs are disabled in this mode.
The shared runtime image contains a direct-only GDAL Parquet plugin and its
pinned Arrow runtime libraries; it does not include DuckDB or the GDAL full
image.

Optional custom location:

```bash
cdk deploy \
  -c db_backend=parquet \
  -c 'parquet_selections={"tx":2020}' \
  -c 'parquet_index_uri_template=s3://bucket/path/state={state}/year={year}/data.parquet' \
  -c task_role_arn=$TASK_ROLE_ARN
```

### Mutable GeoParquet selections

Use an S3 JSON object as the live selection pointer:

```bash
cdk deploy \
  -c db_backend=parquet \
  -c parquet_selections_s3_uri=s3://my-config/config/parquet-selections.json \
  -c task_role_arn=$TASK_ROLE_ARN
```

The object contains either `{"tx":2020,"ct":2021}` or the explicit selection
array documented above. Replace the object, then send:

```bash
curl -X POST "$MAPSERVER_URL/admin/api/parquet-refresh"
curl "$MAPSERVER_URL/admin/api/parquet-refresh"
```

Refresh runs asynchronously and switches to the new immutable generation only
after every Parquet index and the candidate mapfile are ready. Ensure the task
role can read the pointer and all referenced index objects. Protect the admin
mutation endpoint with your ALB/WAF/authentication layer; the container does
not currently authenticate admin API calls itself.

Output `WmsUrl` is your endpoint:
```
http://MapserverStack-Alb-XXXX.us-west-2.elb.amazonaws.com/mapserv
```

## Override defaults

```bash
cdk deploy \
  -c config_bucket=my-buckets \
  -c ecr_repo=my-mapserver \
  -c image_tag=v1.2.0
```

## Budget guardrail

The stack tags its resources with `Project=mapserver-docker-cloudnative`.
To add a monthly AWS Budgets alert filtered to that tag:

```bash
cdk deploy \
  -c monthly_budget_usd=50 \
  -c budget_email=you@example.com
```

This is intentionally an AWS Budgets resource, not a CloudWatch billing alarm:
CloudWatch billing metrics are account/service level, while Budgets can filter
by cost allocation tag. New accounts may need to activate the `Project` cost
allocation tag in Billing before tag-filtered budget reports are complete.

## Park to save cost

```bash
cdk deploy -c parked=true
```

Drops the Fargate service to 0 tasks and stops the RDS instance. ALB and
networking stay (so the DNS name and stack outputs don't change), but those
are cheap (~$16/month for the ALB) compared with running Fargate + RDS.
Re-deploy without `-c parked=true` to resume.

For longer pauses, `cdk destroy` removes everything CDK-owned (the S3 bucket
and ECR repo are referenced — they persist independently).

## Operational notes

- **Durable catalog round-trip.** On every admin scan/enable/disable/delete,
  the in-container admin API uploads the modified `collections.json` to
  `s3://<config-bucket>/config/collections.json`. On any future task start
  (auto-scale, deploy, crash), the entrypoint pulls that object first; if
  missing, the bundled `mapfiles/collections.json` is used as a seed.
- **DB schema is idempotent.** The `DbInit` custom resource creates the
  `cog_index` schema with `IF NOT EXISTS` so re-running it (via the
  `properties.version` bump in `mapserver_stack.py`) does not drop
  existing rows. Schema changes that require destructive migration must
  be handled deliberately, not implicitly via a version bump.
- **`Project` cost-allocation tag** must be activated in the Billing
  console before tag-filtered budget reports become complete. New AWS
  accounts hit this — the budget resource works either way, but reports
  for the period before activation are partial.

## Migration from the existing manual deployment

The existing AWS console-created resources (cluster, ALB, task def) overlap
with what this stack creates. Two options:

1. **Cutover:** delete the manual resources, then `cdk deploy`. ~5 min of
   downtime. The DNS name will change since it's a new ALB.
2. **Import:** define matching resources in CDK, then `cdk import`. Tedious
   for ALB + ECS service due to many attributes. Only worth it if you can't
   afford a DNS change.
