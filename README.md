# mapserver-docker-cloudnative

> Based on the original work by [pedros007](https://github.com/pedros007/mapserver-docker). Thanks to Pete Schmitt for laying the foundation.

A cloud-native MapServer deployment for serving Cloud Optimized GeoTIFFs (COGs) from AWS S3 over WMS / OGC API. Built to run on **AWS Fargate** (ARM64/Graviton) behind an ALB, provisioned end-to-end by a single `cdk deploy`.

**Stack:** MapServer 8.6.3 · GDAL 3.12.4 · nginx (proxy + range-cache + SigV4 signer) · FastCGI · supervisord · Ubuntu 24.04

---

## What this project is for

The point is **AWS deployment**. You have a bucket (or several) of COGs in S3 and want to serve them as a WMS endpoint without standing up a server, configuring MapServer by hand, or learning the Mapfile syntax. `cdk deploy` provisions everything; the admin UI handles the rest.

```
AWS account + S3 COGs   →   cdk deploy   →   ALB URL
                                              ├─ /viewer/     (Leaflet)
                                              ├─ /admin/      (manage collections)
                                              └─ /mapserv?... (WMS / WFS / OGC API)
```

The same container also runs locally (`docker run`) for inspection and development — point it at a public bucket, open the admin UI, see how the pieces fit together. **Local mode is a learning tool, not a production deployment path.** Latency to S3 from outside AWS makes serving real WMS traffic from your laptop impractical.

---

## How it works

1. **Deploy the stack** — `cdk deploy` provisions Fargate, ALB, RDS, log group, IAM, etc.
2. **Open the admin UI** at `<alb>/admin/` and use the **Add a Collection** form to point at any bucket+prefix of COGs.
3. The container's `scan_cog_collection.py` walks the bucket, reads each COG's header in parallel via GDAL, computes a per-COG bounding box, and writes:
   - A **native-CRS [FlatGeobuf](https://flatgeobuf.org/) tile index** (used by MapServer's raster `TILEINDEX` to pick which COGs intersect a request bbox; R-tree indexed for fast spatial lookup)
   - A **Web Mercator GeoJSON footprints file** (used by the viewer's footprint overlay and OGC API Features)
   - On AWS, the same features are also **bulk-inserted into a PostGIS `cog_index` table** keyed by `collection_id`. MapServer reads from PostGIS in deployed mode and from FGB in local mode.
   - An entry in `mapfiles/collections.json` capturing the collection's id, label, bounds, COG count, CRS, etc. Scanner-time fields include `tileindex`, `tileindexes[]` (per-EPSG when a collection straddles multiple source CRSs), `raster_processing` (per-collection PROCESSING overrides for e.g. 16-bit imagery), and auto-derived zoom range.
4. `mapfile_generator.py` reads `collections.json` and emits the MapServer `mapfile.map`. Backend is automatic — POSTGIS when `DB_HOST` is set and `collection.postgis=true`, OGR (FGB / GeoJSON) otherwise. nginx fronts FastCGI mapserv and a shared range-cache that sits in front of a local SigV4 signer — the signer uses the Fargate task's IAM role to sign requests to private (and requester-pays) buckets transparently.
5. WMS requests render COG tiles through that stack: `/mapserv?...&LAYERS={layer_name}&BBOX=...`.

---

## Where the index files come from

Two artifacts per collection live at `/usr/src/mapfiles/`:

| File | Format | Used by |
|---|---|---|
| `<id>_tileindex.fgb` (or `<id>_tileindex_<epsg>.fgb` per group) | **FlatGeobuf** (R-tree indexed binary) | MapServer raster `TILEINDEX` — local mode only |
| `<id>_footprints_3857.geojson` | **GeoJSON** | Browser fetches it directly for the Leaflet overlay; OGC API Features serves it |

These are populated two ways:

| When | Source |
|---|---|
| **Bundled samples** (KY 20×20 preview, NJ 2020) | Shipped in the image. Useful for first-run / `docker run` exploration without scanning anything. |
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

These live outside the CDK stack so they survive a `cdk destroy`:

```bash
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-west-2

aws ecr create-repository \
  --repository-name mapserver-docker-cloudnative \
  --region $AWS_REGION

aws s3 mb s3://mapserver-docker-cloudnative-${AWS_ACCOUNT}-${AWS_REGION} --region $AWS_REGION
```

> ECR repository names are per-account/per-region — no uniqueness concern.
> S3 bucket names are globally unique across all AWS accounts, so the bucket name above is namespaced with your account ID and region to guarantee it's available.

### Step 2 — Build and push the image to ECR

```bash
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

docker buildx build --platform linux/arm64 \
  -t $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/mapserver-docker-cloudnative:latest \
  --push .
```

Or push to `main` and let GitHub Actions handle it — the workflow at [`.github/workflows/build-push.yml`](.github/workflows/build-push.yml) builds and pushes on every commit. It needs one repository variable (`AWS_ACCOUNT_ID`) and an IAM role (`github-actions-mapserver`) with an OIDC trust policy — see [`iam/trust-policy.json`](iam/trust-policy.json).

### Step 3 — Deploy the CDK stack

```bash
cd cdk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CDK_DEFAULT_ACCOUNT=$AWS_ACCOUNT
export CDK_DEFAULT_REGION=$AWS_REGION

npx aws-cdk bootstrap     # one-time per account/region
npx aws-cdk diff          # preview what will be created
npx aws-cdk deploy \
  -c config_bucket=mapserver-docker-cloudnative-${AWS_ACCOUNT}-${AWS_REGION}
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
- `http://localhost:8080/viewer/` — Leaflet map
- `http://localhost:8080/admin/` — Collections / Runtime / Cache / Benchmark tabs (with live worker counts and an active-backend chip showing "FlatGeobuf" or "PostGIS")

> **SSO/STS token expiry**: temporary credentials live ~1 hour. For longer local sessions, run [`scripts/auto_refresh_credentials.sh`](scripts/auto_refresh_credentials.sh) in the background — it re-exports fresh creds into the container's `/tmp/aws_credentials.json` every 15 minutes, which the in-container SigV4 signer picks up automatically.

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

GitHub Actions builds and pushes the image to ECR on every push to `main`. Authentication is OIDC — no long-lived AWS keys in GitHub.

**Required GitHub Actions variables:**
- `AWS_ACCOUNT_ID` — your 12-digit AWS account ID

**Required AWS setup:**
- IAM role `github-actions-mapserver` with OIDC trust policy (see [`iam/trust-policy.json`](iam/trust-policy.json))
- ECR repo named `mapserver-docker-cloudnative`

See [`.github/workflows/build-push.yml`](.github/workflows/build-push.yml) for the full pipeline.

---

## GDAL performance config

Set in the generated mapfile (`mapfile_generator.py` reads env vars):

```
CONFIG "GDAL_CACHEMAX"            "128"          # MB block cache
CONFIG "VSI_CACHE_SIZE"           "33554432"     # 32 MB per worker (nginx does the heavy lifting)
CONFIG "GDAL_DISABLE_READDIR_ON_OPEN" "TRUE"
CONFIG "GDAL_HTTP_MULTIPLEX"      "YES"          # HTTP/2 multiplexing for S3
CONFIG "GDAL_HTTP_VERSION"        "2"
CONFIG "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES" "YES"
```

Per-worker VSI cache is kept small because the nginx range-cache sits in front and is shared across all workers — that's where the win is. Tunable live via the admin UI's Runtime tab; no redeploy needed.

For best performance, **deploy in the same AWS region as your S3 bucket**. Cross-region (or worse, local-to-S3) latency on range-request-heavy COG access is brutal.

---

## Links

- [GDAL Virtual File Systems](https://gdal.org/en/stable/user/virtual_file_systems.html)
- [GDAL Cloud Optimized GeoTIFF](https://gdal.org/en/stable/drivers/raster/cog.html)
- [MapServer WMS](https://mapserver.org/ogc/wms_server.html)
- [TiTiler GDAL Performance Tuning](https://developmentseed.org/titiler/advanced/performance_tuning/) — different stack but the same GDAL-on-S3 tuning notes apply
- [KyFromAbove on AWS Open Data](https://registry.opendata.aws/kyfromabove/)
- [NZ Imagery on AWS Open Data](https://registry.opendata.aws/nz-imagery/)
