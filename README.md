# mapserver-docker-cloudnative

> This repo is based on the original work by [pedros007](https://github.com/pedros007/mapserver-docker). Thanks to Pedro for laying the foundation.

A cloud-native MapServer deployment for serving Cloud Optimized GeoTIFFs (COGs) from AWS S3 via WMS. Built for AWS Fargate (ARM64/Graviton), with a GitHub Actions CI/CD pipeline to ECR.

**Stack:** MapServer 8.6.3 · GDAL 3.12.4 · nginx · FastCGI · supervisord · Ubuntu 24.04

---

## How it works

1. **Deploy the stack** — Use AWS CDK to deploy the Fargate container, or run the Docker image locally.
2. **Access the Admin UI** — Navigate to `/admin/` in your browser.
3. **Scan an S3 Bucket** — Enter an S3 bucket and prefix containing COGs. The internal `scan_cog_collection.py` script automatically scans the bucket, calculates spatial extents, and generates GeoJSON TileIndexes.
4. **MapServer serves WMS** — MapServer natively reads the generated GeoJSON TileIndex and proxies `/vsicurl/` range requests through an internal Nginx byte-range cache to serve ultra-fast WMS tiles.

---

## Quick start (local)

### Prerequisites

- A local container engine. *Note: Must support `buildx` for `linux/arm64` cross-compilation.*
  - **macOS:** This project was built and tested locally using [Colima](https://github.com/abiosoft/colima).
  - **Windows:** We recommend [Rancher Desktop](https://rancherdesktop.io/) or [Podman Desktop](https://podman-desktop.io/).
  - **Linux:** Native Docker CE or Podman.
- AWS credentials with read access to the COG bucket.

### 1. Run the container

```bash
eval $(aws configure export-credentials --format env)

docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -p 8080:80 \
  <your-image>
```

### 2. Configure via Admin UI

1. Open `http://localhost:8080/admin/` in your browser.
2. Go to the **Collections** tab.
3. Enter your S3 bucket and prefix (e.g. `my-bucket` and `imagery/2024/`).
4. Click **Start Scan**. The backend will automatically generate the required TileIndexes and reload MapServer.

### 3. Test WMS & Viewer

- **Viewer**: Once the scan is complete, click the **Viewer** tab in the Admin UI to see an interactive Leaflet map of your data.
- **WMS GetCapabilities**:
  ```
  http://localhost:8080/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities
  ```

---

## Infrastructure

The full AWS deployment (VPC, ALB, ECS Fargate, IAM, CloudWatch, autoscaling)
is defined in [cdk/](cdk/). One `cdk deploy` provisions everything from
scratch.

```bash
cd cdk && pip install -r requirements.txt && cdk deploy
```

See [cdk/README.md](cdk/README.md) for prereqs and migration notes.

---

## Runtime config

The container reads three optional env vars at startup. Each downloads a
file from S3, overwriting the bundled default:

| Env var          | Target path                            | Purpose                       |
| ---------------- | -------------------------------------- | ----------------------------- |
| `MAPFILE_S3_URI` | `/usr/src/mapfiles/mapfile.map`        | MapServer config              |
| `VRT_S3_URI`     | `/usr/src/mapfiles/mosaic.vrt`         | GDAL VRT mosaicing the COGs   |
| `EXTENTS_S3_URI` | `/usr/src/mapfiles/tile_extents.geojson` | OGC API Features layer source |

This lets you change the mapfile or rebuild the VRT without rebuilding the
image — upload to S3, then force a new ECS deployment.

---

## CI/CD

GitHub Actions builds and pushes the image to ECR on every push to `main`. Authentication uses OIDC — no long-lived AWS keys are stored in GitHub.

**Required GitHub Actions variables:**
- `AWS_ACCOUNT_ID` — your 12-digit AWS account ID

**Required AWS setup:**
- IAM role `github-actions-mapserver` with OIDC trust policy (see `iam/trust-policy.json`)
- ECR repo named `mapserver-docker-cloudnative`

The workflow pins the build to `linux/arm64` because the target is AWS Graviton (ARM64). An `amd64` image would not run on Graviton Fargate tasks. Local builds use the host's native architecture — the `--platform` flag is only set in CI/CD.

See `.github/workflows/build-push.yml` for the full pipeline.

---

## GDAL performance config

Set in `mapfile.map` (MAP block):

```
CONFIG "AWS_NO_SIGN_REQUEST" "YES"        # omit for private buckets
CONFIG "VSI_CACHE" "TRUE"
CONFIG "VSI_CACHE_SIZE" "268435456"       # 256 MB per FastCGI worker
CONFIG "GDAL_DISABLE_READDIR_ON_OPEN" "TRUE"
CONFIG "CPL_VSIL_CURL_ALLOWED_EXTENSIONS" ".tiff,.tif"
```

For best performance, deploy in the same AWS region as your S3 bucket. The latency difference between local and in-region is substantial for range-request-heavy COG access.

---

## Links

- [GDAL Virtual File Systems](https://gdal.org/en/stable/user/virtual_file_systems.html)
- [GDAL Cloud Optimized GeoTIFF](https://gdal.org/en/stable/drivers/raster/cog.html)
- [TiTiler GDAL Performance Tuning](https://developmentseed.org/titiler/advanced/performance_tuning/)
- [MapServer WMS](https://mapserver.org/ogc/wms_server.html)
- [NZ Imagery on AWS Open Data](https://registry.opendata.aws/nz-imagery/)
