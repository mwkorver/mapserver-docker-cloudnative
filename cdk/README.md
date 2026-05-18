# CDK — mapserver infrastructure

Provisions the AWS resources to run the mapserver container on Fargate behind
an ALB. Pairs with the Dockerfile and GitHub Actions workflow at the repo
root.

## What it creates

| Resource          | Notes                                              |
| ----------------- | -------------------------------------------------- |
| VPC + S3 endpoint | 2 AZ, public subnets only, free S3 gateway endpoint |
| ECS cluster       | `mapserver` (Fargate)                              |
| Task definition   | ARM64, 4 vCPU / 8 GB, image from ECR `:latest`     |
| Service           | 1 task baseline, autoscales 1→4 on 60% CPU         |
| ALB               | HTTP:80, WMS GetCapabilities health check          |
| RDS PostgreSQL    | Single instance, `mapserver` DB, PostGIS extension, schema initialized via a `DbInit` custom-resource Lambda. Scanner inserts COG indexes here on deploy; MapServer reads `cog_index` for raster TILEINDEX layers. |
| Log group         | `/ecs/mapserver`, 30-day retention                 |
| Perf API          | Small Lambda + Function URL that surfaces recent WMS GetMap stats from CloudWatch Logs Insights for the viewer's in-page performance panel. |
| AWS Budgets       | **Opt-in.** Set `-c monthly_budget_usd=N -c budget_email=…` to create a monthly budget filtered to the stack's `Project=mapserver-docker-cloudnative` cost-allocation tag. |
| Stack-wide tags   | `Project=mapserver-docker-cloudnative` on every resource for cost reporting and the budget filter. |

The S3 config bucket and ECR repo are **referenced** (assumed to exist).
Create them out-of-band — they're long-lived and shouldn't be tied to stack
lifecycle. ECR is also written to by the GitHub Actions workflow. The task
role gets `read-write` on `s3://<config-bucket>/config/*` for catalog
persistence and `read-only` on `s3://<imagery-bucket>/imagery/*` for COG
serving.

## Prereqs

```bash
pip install -r requirements.txt
npm install -g aws-cdk             # or use npx cdk
aws ecr create-repository --repository-name mapserver-docker-cloudnative --region us-west-2
aws s3 mb s3://mapserver-docker-cloudnative --region us-west-2
```

Push an image so the service can start:

```bash
# from repo root
docker build --platform linux/arm64 -t mapserver-docker-cloudnative .
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin <acct>.dkr.ecr.us-west-2.amazonaws.com
docker tag mapserver-docker-cloudnative:latest \
  <acct>.dkr.ecr.us-west-2.amazonaws.com/mapserver-docker-cloudnative:latest
docker push <acct>.dkr.ecr.us-west-2.amazonaws.com/mapserver-docker-cloudnative:latest
```

The deployed container loads `config/collections.json` from the config bucket
at startup, then generates `mapfile.map` from that catalog. If the S3 object is
missing, the image's bundled `mapfiles/collections.json` is used as a first-run
seed. Admin collection scans/enable/delete operations write the updated catalog
back to `s3://<config-bucket>/config/collections.json` so Fargate task
replacement does not lose collection metadata. Optional escape hatch:
`MAPFILE_S3_URI` will download a hand-written mapfile if set.

## Deploy

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2

cdk bootstrap                        # one-time per account/region
cdk diff                             # preview
cdk deploy
```

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
