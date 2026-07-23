# mapserver-docker-cloudnative

[![Test](https://github.com/mwkorver/mapserver-docker-cloudnative/actions/workflows/test.yml/badge.svg)](https://github.com/mwkorver/mapserver-docker-cloudnative/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Based on the original work by [pedros007](https://github.com/pedros007/mapserver-docker). Thanks to Pete Schmitt for laying the foundation.

A self-contained MapServer + GDAL + nginx stack for serving Cloud Optimized GeoTIFFs (COGs) from AWS S3 over WMS / OGC API — built as a **tool for measuring server-side WMS performance** under different configurations, deployed end-to-end on AWS Fargate (ARM64/Graviton) by a single `cdk deploy`.

**Stack:** MapServer 8.6.3 · GDAL 3.12.4 with GeoParquet · nginx (proxy + range-cache + SigV4 signer) · FastCGI · supervisord · Ubuntu 24.04

The production image uses GDAL's compact Ubuntu base. It builds only the
standalone Parquet plugin needed by this application, pins its Arrow ABI to
21.0.0, and excludes DuckDB and GDAL's unrelated full-image drivers. The same
image serves both index backends.

## Index backend options

The same MapServer/GDAL container supports two deployment-time index backends:

| Backend | CDK context | Index source | Collection mutation |
|---|---|---|---|
| PostgreSQL/PostGIS | `-c db_backend=postgis` | `cog_index` in RDS | Admin scans enabled |
| GDAL GeoParquet | `-c db_backend=parquet` | Selected state/year files staged locally from S3 | Read-only; change selections and redeploy |

PostGIS remains the default. The Parquet backend omits RDS, its secret, DB
security groups, and the DB-init Lambda. At task startup it downloads only the
configured state/year indexes, adds `location` and per-COG `tile_srs` fields,
and generates `collections.json` plus `mapfile.map`. MapServer uses GDAL's
Parquet driver directly; DuckDB is not in the request path.

Example Parquet deployment:

```bash
npx aws-cdk deploy \
  -c db_backend=parquet \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN \
  -c 'parquet_selections={"tx":2020,"ct":2021}'
```

The default index URI template is:

```text
s3://cog-stac-viewer-495811053987-us-west-2/lake/collection=naip/region={state}/year={year}/data_0.parquet
```

Override it with `-c parquet_index_uri_template=...`. The selection may also
be a JSON array when a state/year needs an explicit URI:

```json
[
  {
    "state": "tx",
    "year": 2020,
    "uri": "s3://another-bucket/indexes/tx-2020.parquet"
  }
]
```

For mutable selections, point the task at a small JSON object in S3:

```bash
npx aws-cdk deploy \
  -c db_backend=parquet \
  -c parquet_selections_s3_uri=s3://my-config/config/parquet-selections.json \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN
```

The object uses the same JSON mapping or array accepted by
`parquet_selections`. After replacing it, invalidate the running container:

```bash
curl -X POST "$MAPSERVER_URL/admin/api/parquet-refresh"
curl "$MAPSERVER_URL/admin/api/parquet-refresh"
```

The POST returns `202` and refreshes in the background. New indexes are fully
downloaded and staged in an immutable generation before MapServer workers are
briefly stopped and the catalog/mapfile are atomically switched. A failed
refresh leaves the prior generation active. The task role needs `s3:GetObject`
for both the pointer object and every referenced Parquet index. The current
admin API has no built-in authentication, so do not expose its mutation
endpoints publicly without an ALB/WAF/authentication control.

---

## What this project is for

Serving COGs over WMS at acceptable latency is not "deploy and forget", even on managed compute like ECS/Fargate. Throughput, tail latency, and cost-per-request depend on at least:

- **MapServer worker count** (FastCGI procs) and the Fargate task's CPU/memory allocation
- **GDAL cache tuning** (`GDAL_CACHEMAX`, `VSI_CACHE_SIZE`) per worker
- **nginx range-cache size** in front of `/vsicurl` byte-range reads against S3
- **Co-location** of compute and S3 bucket region
- **Image-format pipeline** (PNG24 vs RGBA, hi-DPI tile doubling, `RESAMPLE` method, `PROCESSING` overrides for 16-bit imagery)
- **COG block size and overview structure** of the source imagery
- **WMS request shape** (tile size, projection mismatches, layer count)

Getting any of those wrong leaves throughput or latency on the table. Getting them right requires **measurement**, not guesswork. This project bundles MapServer with the tooling to actually see what's happening on the server:

- **`/viewer/`** — OpenLayers map that paints per-tile WMS round-trip times directly onto each tile cell, plus a slide-out perf panel summarising server and client samples.
- **`/admin/`** — live worker counts, GDAL config introspection, the active backend chip (FlatGeobuf vs PostGIS), and a benchmark tab driving synthetic load.
- **`/admin/api/benchmark`** — programmatic load endpoint for scripted before/after comparisons across deploys or env changes.
- **MapServer numprocs / GDAL knobs** are env-driven on the running container or task definition: change them, rerun the benchmark, see the difference.

**Scope:** server-side. How fast can MapServer + GDAL + nginx return WMS tiles for a given COG layout on a given Fargate configuration. Client-side, network-path, and CDN tuning (CloudFront, browser cache, edge gzip) are out of scope.

```
AWS account + S3 COGs   →   cdk deploy   →   ALB URL
                                              ├─ /viewer/     (OpenLayers + tile timings)
                                              ├─ /admin/      (collections + perf knobs + benchmark)
                                              └─ /mapserv?... (WMS / WFS / OGC API)
```

The same container also runs locally (`docker run`) for inspection and development — point it at a public bucket, open the admin UI, see how the pieces fit together. **Local mode is a learning tool, not a perf-measurement environment.** Latency from outside AWS to S3 dwarfs any tuning differences, so any numbers from `docker run` on your laptop are useless for comparison purposes. Measure on Fargate.

---

## What's new here

Most published cloud-native COG WMS / tile work falls into one of three patterns:

- **Lambda + TiTiler** (DevelopmentSeed) — stateless per-invocation, no persistent cache layer in front of GDAL. The [TiTiler performance tuning notes](https://developmentseed.org/titiler/advanced/performance_tuning/) are the canonical published reference for the GDAL knobs that matter on serverless.
- **GeoServer + GeoWebCache** — enterprise-default Java stack. GWC caches *rendered* output tiles, not the byte ranges of source data — a different caching layer than this project's.
- **MapProxy / TileServer GL** in front of pre-rendered data — again, rendered-tile caching, not byte-range caching against live source COGs.

This project is a different shape:

- **Persistent compute** (ECS/Fargate) instead of Lambda — `/vsicurl/` state, the GDAL block cache, and the FastCGI worker pool all survive across requests.
- **nginx byte-range proxy cache** between GDAL's `/vsicurl/` and the (private, SigV4-signed) S3 source — cache hits return COG byte ranges from local disk in single-digit milliseconds without re-hitting S3.
- **Measurement built in** (viewer tile-timing overlay, perf panel, admin benchmark tab + `/admin/api/benchmark` endpoint) so the tunables are observable as you change them.

### Credits

The nginx-byte-range-proxy-cache-in-front-of-GDAL pattern has been discussed by **[Even Rouault](https://github.com/rouault)** — the lead GDAL maintainer — on the `gdal-dev` mailing list and at FOSS4G as one approach to amortising S3 byte-range latency. To my knowledge no published reference deployment of that pattern existed before this repo; the components have been talked about, but not assembled into a `cdk deploy`-able working stack with the measurement surface attached. Credit for the underlying idea belongs there.

This repo also builds on the original Dockerised-MapServer work by [pedros007](https://github.com/pedros007/mapserver-docker), credited at the top.

### Divergence from pedros007

This fork keeps the "MapServer + GDAL in a container" premise but rebuilds the serving path and deployment around cloud-native COG measurement:

- **nginx + FastCGI instead of Apache + CGI** — a persistent FastCGI worker pool behind nginx, replacing the per-request CGI model.
- **nginx byte-range proxy cache + SigV4 signer** (`etc/s3_sigv4_proxy.py`) in front of GDAL's `/vsicurl/`, so private/requester-pays S3 COGs are signed and their byte ranges cached on local disk.
- **Two deployment-time index backends** — PostGIS (`cog_index` in RDS) or GDAL GeoParquet staged from S3 — with a `scan_cog_collection.py` scanner and generated `collections.json` / `mapfile.map`.
- **Admin, viewer, and benchmark surfaces** — an admin scan UI, an OpenLayers viewer with tile-timing overlay, and a benchmark tab / `/admin/api/benchmark` endpoint for observing the GDAL and cache tunables.
- **End-to-end AWS deployment** — a single `cdk deploy` provisions Fargate (ARM64/Graviton), ALB, logs, and optional RDS, with least-privilege task/execution roles managed out of band.

---

## How it works

1. **Deploy the stack** — `cdk deploy` provisions Fargate, ALB, logs, and optionally RDS/PostGIS.
2. **Open the admin UI** at `<alb>/admin/` and use the **Add a Collection** form to point at any bucket+prefix of COGs.
3. The container's `scan_cog_collection.py` walks the bucket, reads each COG's header in parallel via GDAL, computes a per-COG bounding box, and writes:
   - A **native-CRS [FlatGeobuf](https://flatgeobuf.org/) tile index** (used by MapServer's raster `TILEINDEX` for tile lookup, **and** by MapServer's WFS layer that the OpenLayers viewer queries for COG footprints — R-tree indexed for fast spatial lookup at both layers).
   - On AWS, the same features are also **bulk-inserted into a PostGIS `cog_index` table** keyed by `collection_id`. MapServer reads from PostGIS in deployed mode and from FGB in local mode.
   - An entry in `mapfiles/collections.json` capturing the collection's id, label, bounds, COG count, CRS, etc. Scanner-time fields include `tileindex`, `tileindexes[]` (per-EPSG when a collection straddles multiple source CRSs), `raster_processing` (per-collection PROCESSING overrides for e.g. 16-bit imagery), and auto-derived zoom range.
4. `mapfile_generator.py` reads `collections.json` and emits the MapServer `mapfile.map`. Backend is automatic — POSTGIS when `DB_HOST` is set and `collection.postgis=true`, OGR (FGB / GeoJSON) otherwise. nginx fronts FastCGI mapserv and a shared range-cache that sits in front of a local SigV4 signer — the signer uses the Fargate task's IAM role to sign requests to private (and requester-pays) buckets transparently.
5. WMS requests render COG tiles through that stack: `/mapserv?...&LAYERS={layer_name}&BBOX=...`.

---

## Where the index files come from

One artifact per collection lives at `/usr/src/mapfiles/`:

| File | Format | Used by |
|---|---|---|
| `<id>_tileindex.fgb` (or `<id>_tileindex_<epsg>.fgb` per group) | **FlatGeobuf** (R-tree indexed binary) | MapServer raster `TILEINDEX` (local mode) and MapServer's WFS layer that the OpenLayers viewer queries for COG footprints |

These are populated two ways:

| When | Source |
|---|---|
| **Bundled sample** (NJ 2020 1ft) | Shipped in the image — `mapfiles/nj_2020_tileindex_6527.fgb`. Useful for first-run / `docker run` exploration without scanning anything. |
| **Live deployment** | The admin UI's scan flow writes them. The scanner output and `collections.json` (synced to `s3://<config-bucket>/config/`) are the single source of truth for what the deployed WMS serves. |

**Backend selection is automatic:**

- **Local mode** (no `DB_HOST` set): MapServer reads tile indexes from the FGB files. FlatGeobuf has a packed Hilbert R-tree built in, so bbox queries on tens of thousands of features stay fast without spinning up a database.
- **AWS mode** (`DB_HOST` set by the CDK stack): the scanner also bulk-inserts into a PostGIS `cog_index` table keyed by `collection_id`. MapServer reads from PostGIS exclusively; the FGB files are still produced for cross-checking but not used for serving.

Why both? Local mode has no DB to manage and stays portable. AWS mode benefits from PostGIS's concurrent-write tolerance and integration with future pgstac use; the durable collection catalog on S3 also means Fargate task replacement doesn't lose state.

---

## Quick start: AWS deployment

The intended path.

### Prerequisites
- AWS account with admin access (or sufficient permissions for ECS, IAM, RDS, ALB, VPC, ECR, S3)
- AWS CLI v2 configured with credentials (`aws configure sso` or `aws configure`)
- Docker with buildx for `linux/arm64` (Fargate is ARM64/Graviton)
- Node.js 20+ (for the CDK CLI; `npx aws-cdk` is fine)
- Python 3.12+ (for the CDK app)

### Step 1 — Create the long-lived resources

These live outside the CDK stack so they survive a `cdk destroy` (promoting separation of concerns):

```bash
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-west-2

# 1. Create the ECR Repository
aws ecr create-repository \
  --repository-name mapserver-docker-cloudnative \
  --region $AWS_REGION

# 2. Create the S3 Configuration Bucket
aws s3 mb s3://mapserver-docker-cloudnative-${AWS_ACCOUNT}-${AWS_REGION} --region $AWS_REGION

# 3. Create the ECS IAM Roles
# Follow the step-by-step instructions in the [IAM Provisioning Guide](iam/README.md)
# to create the Fargate Task Role and Fargate Task Execution Role out-of-band.
```

> ECR repository names are per-account/per-region — no uniqueness concern.
> S3 bucket names are globally unique across all AWS accounts, so the bucket name above is namespaced with your account ID and region to guarantee it's available.

### Step 2 — Build and push the image to ECR

```bash
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
```

The build command depends on your machine:

**Windows or Intel Mac** — cross-compile to arm64 using buildx (included in Docker Desktop):
```bash
docker buildx build --platform linux/arm64 \
  -t $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/mapserver-docker-cloudnative:latest \
  --push .
```

**Apple Silicon Mac (M1/M2/M3)** — your machine is already arm64, so no cross-compilation needed:
```bash
docker build \
  -t $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/mapserver-docker-cloudnative:latest \
  --push .
```

> If `docker buildx` or `--push` gives an "unknown flag" error on Mac, buildx isn't wired up as a Docker plugin. Fix it once with:
> ```bash
> mkdir -p ~/.docker/cli-plugins
> ln -sfn $(brew --prefix)/opt/docker-buildx/bin/docker-buildx ~/.docker/cli-plugins/docker-buildx
> ```

Or skip the manual build entirely and push to `main` — the GitHub Actions workflow at [`.github/workflows/build-push.yml`](.github/workflows/build-push.yml) builds the image on every pull request and pushes it to ECR on merge to `main` (x86 runners, so it always cross-compiles to arm64). It needs one repository variable (`AWS_ACCOUNT_ID`) and an IAM role (`github-actions-mapserver`) with an OIDC trust policy — see [`iam/trust-policy.json`](iam/trust-policy.json).

### Step 3 — Deploy the CDK stack

Deploying the stack requires the pre-created Task Role ARN (strictly required) and Execution Role ARN (recommended).

```bash
cd cdk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CDK_DEFAULT_ACCOUNT=$AWS_ACCOUNT
export CDK_DEFAULT_REGION=$AWS_REGION

# Retrieve your pre-created role ARNs
export TASK_ROLE_ARN=$(aws iam get-role --role-name MapserverFargateTaskRole --query "Role.Arn" --output text)
export EXECUTION_ROLE_ARN=$(aws iam get-role --role-name MapserverFargateExecutionRole --query "Role.Arn" --output text)

npx aws-cdk bootstrap     # one-time per account/region

npx aws-cdk diff \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN

npx aws-cdk deploy \
  -c task_role_arn=$TASK_ROLE_ARN \
  -c execution_role_arn=$EXECUTION_ROLE_ARN
```

Deploy takes ~10 min (RDS provisioning is the long pole). The stack outputs include the ALB DNS name and the full WMS URL.

### Step 4 — Add your data via the admin UI

Open `<alb-dns>/admin/` → **Collections** tab → **Add a collection** form. Bucket + prefix + access mode (unsigned / signed / requester-pays) is all you need. The form submits a background scan that runs through the container's IAM role; progress streams in via the **Active scans** panel. When the scan completes, a new layer appears in WMS and the viewer.

### Step 5 — Park or destroy

```bash
# Park (Fargate→0, RDS stopped) — saves cost, keeps state (~$16/mo for the ALB)
npx aws-cdk deploy -c parked=true

# Tear down everything CDK-owns (S3 bucket and ECR repo survive)
npx aws-cdk destroy
```

See [cdk/README.md](cdk/README.md) for parameterization details (custom bucket name, image tag, budget alerts).

### Changing Fargate CPU / memory from the AWS Console

Fargate does not have an EC2-style instance type. The equivalent runtime shape is
the task definition's **CPU** and **Memory** values. Changing those values creates
a new ECS task definition revision and rolls the service to replacement tasks.
Expect a short service rollout, usually a few minutes.

The ALB does not change when you update the ECS service to a new task
definition revision. The same `/admin/`, `/viewer/`, and `/mapserv` URLs keep
working after the replacement task passes health checks. During rollout you may
briefly see errors if no healthy task is available.

AWS Console flow:

1. Open **ECS** → **Task definitions**.
2. Select the `mapserver` task definition family.
3. Open the latest revision and choose **Create new revision**.
4. Change **Task size**:
   - CPU, for example `4096` = 4 vCPU
   - Memory, for example `8192` = 8 GB
5. Keep the container settings the same, then create the new revision.
6. Open **ECS** → **Clusters** → `mapserver` → **Services** → `mapserver`.
7. Choose **Update service**.
8. Select the new task definition revision.
9. Keep desired count unchanged and start the deployment.
10. Watch the service events until the new task is healthy behind the ALB.

After the new task is running, open `/admin/` → **Runtime** and check:

- configured MapServer workers
- observed MapServer worker processes
- WMS GetCapabilities response time

CPU/memory does not automatically change `MAPSERVER_NUMPROCS`. If you scale
Fargate up, test whether increasing MapServer workers improves throughput. If
you scale down, reduce workers if render latency or memory pressure gets worse.

Console edits are useful for testing, but they create drift from CDK. A later
`cdk deploy` can revert CPU/memory back to the values in CDK context. Once you
find a good setting, encode it in CDK:

```bash
npx aws-cdk deploy \
  -c mapserver_cpu=4096 \
  -c mapserver_memory_limit_mib=8192
```

---

## Local exploration (optional)

For seeing the architecture work without an AWS bill. Not for serving real traffic.

```bash
docker build -t mapserver:local .

# Anonymous public buckets (e.g. the bundled KyFromAbove sample):
docker run -d --name mapserver --rm -p 8080:80 \
  -e ADMIN_WRITE_ENABLED=true mapserver:local

# Or, with AWS credentials for private/requester-pays buckets:
eval $(aws configure export-credentials --format env)
docker run -d --name mapserver --rm -p 8080:80 \
  -e ADMIN_WRITE_ENABLED=true \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  mapserver:local
```

Open:
- `http://localhost:8080/viewer/` — OpenLayers map
- `http://localhost:8080/admin/` — Collections / Runtime / Cache / Benchmark tabs (with live worker counts and an active-backend chip showing "FlatGeobuf", "PostGIS", or "GeoParquet")

> **SSO/STS token expiry**: temporary credentials live ~1 hour. For longer local sessions, run [`scripts/auto_refresh_credentials.sh`](scripts/auto_refresh_credentials.sh) in the background — it re-exports fresh creds into the container's `/tmp/aws_credentials.json` every 15 minutes, which the in-container SigV4 signer picks up automatically.

### Running with GeoParquet backend locally

To run and explore the GeoParquet backend locally, pass `DB_BACKEND=parquet` and define `PARQUET_SELECTION_JSON` with your selections:

```bash
eval $(aws configure export-credentials --format env)
docker run -d --name mapserver --rm -p 8080:80 \
  -e DB_BACKEND=parquet \
  -e PARQUET_SELECTION_JSON='{"tx":2020,"ct":2021}' \
  -e ADMIN_WRITE_ENABLED=true \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  mapserver:local
```

---

## Architecture detail

```
                  ┌──────────────────────────────────────────────────────────┐
                  │  Fargate task (4 vCPU / 8 GB ARM64)                      │
                  │                                                          │
 Browser ─► ALB ──┼──► nginx :80 ─┬─► mapserv FastCGI ──► mapfile.map        │
                  │               │                       (generated from    │
                  │               │                        collections.json) │
                  │               ├─► /admin/  (HTML)                        │
                  │               ├─► /viewer/ (HTML)                        │
                  │               └─► /admin/api/* ──► admin_api.py :9100 ───┼──► s3://…/config/collections.json
                  │                                                          │     (durable catalog;
                  │   mapserv ─► PostGIS cog_index ──► RDS PostgreSQL ───────┼──►   pulled at boot,
                  │            (tileindex layer)         (+ PostGIS, pgstac) │      synced on every
                  │                                                          │      admin mutation)
                  │   mapserv ─► GDAL /vsicurl/http://localhost:8001/...     │
                  │                                       │                  │
                  │                          nginx :8001  ▼                  │
                  │                          (range-cache, 4 GB on disk)     │
                  │                                       │ on miss          │
                  │                                       ▼                  │
                  │                          s3_sigv4_proxy.py :9000         │
                  │                          (signs with task IAM role)      │
                  └───────────────────────────────────────┼──────────────────┘
                                                          │
                                                      Any S3 bucket
                                                      the task can read
```

Two layers of caching: nginx range-cache (shared across all FastCGI workers, persistent on the task) and GDAL's per-worker VSI cache. The SigV4 proxy handles signed and requester-pays buckets — including buckets in regions other than the task's — without GDAL having to know IAM details.

In local mode (no RDS), MapServer reads tile indexes from FGB files instead of PostGIS; everything else in the diagram is the same.

---

## CI/CD

Two GitHub Actions workflows run on every push and pull request to `main`:

- **[`test.yml`](.github/workflows/test.yml)** runs the Python unit tests (`pytest -q`). The tests are hermetic — `osgeo`/`boto3`/`psycopg2` are lazily imported and the one GDAL-dependent test class is `skipif`-guarded — so the job needs only `pytest` and no AWS credentials.
- **[`build-push.yml`](.github/workflows/build-push.yml)** builds the ARM64 image; on pushes to `main` it also pushes to ECR. Authentication is OIDC — no long-lived AWS keys in GitHub.

**Required GitHub Actions variables (build/push only):**
- `AWS_ACCOUNT_ID` — your 12-digit AWS account ID

**Required AWS setup (build/push only):**
- IAM role `github-actions-mapserver` with OIDC trust policy (see [`iam/trust-policy.json`](iam/trust-policy.json))
- ECR repo named `mapserver-docker-cloudnative`

---

## Development and Testing

### Python Unit Tests
The python unit tests cover the mapfile generator, Parquet backends, refresh routines, and catalog scanner.

To install dependencies and run the unit tests locally:
```bash
# Create a virtual environment and install requirements
pip install pytest boto3

# Run the unit tests
pytest
```

### UI Integration Tests
UI integration tests verify OpenLayers viewer functionality, WMS capabilities, and admin interfaces using Playwright.

To set up and run the UI tests:
```bash
# Install node packages
npm install

# Install Playwright browsers
npx playwright install

# Run the UI tests
npm test
```

---

## GDAL performance config

Set in the generated mapfile (`mapfile_generator.py` reads env vars):

```
CONFIG "GDAL_CACHEMAX"                      "128"          # MB block cache
CONFIG "VSI_CACHE"                          "FALSE"        # RAM VSI cache disabled by default (nginx does the heavy lifting)
CONFIG "VSI_CACHE_SIZE"                     "33554432"     # 32 MB per worker (ignored unless VSI_CACHE is TRUE)
CONFIG "GDAL_DISABLE_READDIR_ON_OPEN"       "TRUE"         # Avoid costly ListBucket operations on S3
CONFIG "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES" "YES"          # Merge consecutive byte reads
```

> [!NOTE]
> **HTTP/2 Multiplexing:** `GDAL_HTTP_MULTIPLEX` and `GDAL_HTTP_VERSION` are intentionally omitted. Because GDAL talks locally to the container's Nginx loopback range-cache (`http://localhost:8001`), which operates on plaintext HTTP/1.1, the loopback connection negotiates back down to 1.1 automatically. The actual latency-sensitive remote connection (from the proxy signer to S3) is governed by the signer's own HTTP/2 client, not by GDAL.

Per-worker VSI cache is kept small (or disabled) because the nginx range-cache sits in front and is shared across all workers — that's where the win is. Tunable live via the admin UI's Runtime tab; no redeploy needed.

For best performance, **deploy in the same AWS region as your S3 bucket**. Cross-region (or worse, local-to-S3) latency on range-request-heavy COG access is brutal.

---

## Links

- [GDAL Virtual File Systems](https://gdal.org/en/stable/user/virtual_file_systems.html)
- [GDAL Cloud Optimized GeoTIFF](https://gdal.org/en/stable/drivers/raster/cog.html)
- [MapServer WMS](https://mapserver.org/ogc/wms_server.html)
- [TiTiler GDAL Performance Tuning](https://developmentseed.org/titiler/advanced/performance_tuning/) — different stack but the same GDAL-on-S3 tuning notes apply
- [KyFromAbove on AWS Open Data](https://registry.opendata.aws/kyfromabove/)
- [NZ Imagery on AWS Open Data](https://registry.opendata.aws/nz-imagery/)
