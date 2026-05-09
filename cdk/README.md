# CDK — mapserver infrastructure

Provisions the AWS resources to run the mapserver container on Fargate behind
an ALB. Pairs with the Dockerfile and GitHub Actions workflow at the repo
root.

## What it creates

| Resource          | Notes                                              |
| ----------------- | -------------------------------------------------- |
| VPC + S3 endpoint | 2 AZ, public subnets only, free S3 gateway endpoint |
| ECS cluster       | `mapserver` (Fargate)                              |
| Task definition   | ARM64, 1 vCPU / 4 GB, image from ECR `:latest`     |
| Service           | 1 task baseline, autoscales 1→4 on 60% CPU         |
| ALB               | HTTP:80, WMS GetCapabilities health check          |
| Log group         | `/ecs/mapserver`, 30-day retention                 |

The S3 config bucket and ECR repo are **referenced** (assumed to exist).
Create them out-of-band — they're long-lived and shouldn't be tied to stack
lifecycle. ECR is also written to by the GitHub Actions workflow.

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

Upload config files referenced by the task env vars:

```bash
aws s3 cp ../mapfiles/mapfile.map s3://mapserver-docker-cloudnative/mapfile.map
aws s3 cp ../data/auckland_2024.vrt s3://mapserver-docker-cloudnative/
aws s3 cp ../data/tile_extents.geojson s3://mapserver-docker-cloudnative/
```

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

## Park to save cost

```bash
aws ecs update-service --cluster mapserver --service mapserver \
  --desired-count 0 --region us-west-2
```

The ALB still costs ~$16/month while idle. For longer pauses, `cdk destroy`
removes everything (the S3 bucket and ECR repo are referenced — not deleted).

## Migration from the existing manual deployment

The existing AWS console-created resources (cluster, ALB, task def) overlap
with what this stack creates. Two options:

1. **Cutover:** delete the manual resources, then `cdk deploy`. ~5 min of
   downtime. The DNS name will change since it's a new ALB.
2. **Import:** define matching resources in CDK, then `cdk import`. Tedious
   for ALB + ECS service due to many attributes. Only worth it if you can't
   afford a DNS change.
