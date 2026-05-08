# mapserver-docker-cloudnative

> This repo is based on the original work by [pedros007](https://github.com/pedros007/mapserver-docker). Thanks to Pedro for laying the foundation.

A cloud-native MapServer deployment for serving Cloud Optimized GeoTIFFs (COGs) from AWS S3 via WMS. Built for AWS Fargate (ARM64/Graviton), with a GitHub Actions CI/CD pipeline to ECR.

**Stack:** MapServer 8.6.3 · GDAL 3.12.4 · nginx · FastCGI · supervisord · Ubuntu 24.04

---

## How it works

1. **Build a VRT mosaic once** — `scripts/build_vrt.py` reads COG headers in parallel from S3 and produces a single VRT XML file referencing all tiles.
2. **Store the VRT in S3** — upload it to your private bucket.
3. **Container downloads VRT at startup** — set `VRT_S3_URI` and the entrypoint fetches it before MapServer starts.
4. **MapServer serves WMS** — GDAL reads COG tiles on demand via `/vsis3/` range requests, using a 256 MB in-process cache.

---

## Quick start (local)

### Prerequisites

- Docker Desktop
- AWS credentials with read access to the COG bucket and read/write to your VRT bucket

### 1. Build the VRT

```bash
eval $(aws configure export-credentials --format env)

docker run --rm --platform linux/arm64 \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -v $(pwd)/data:/output \
  <your-image> python3 /scripts/build_vrt.py

aws s3 cp data/auckland_2024.vrt s3://<your-bucket>/auckland_2024.vrt
```

Edit `BUCKET`, `PREFIX`, and `OUTPUT` at the top of `scripts/build_vrt.py` to point at your data.

### 2. Run the container

```bash
eval $(aws configure export-credentials --format env)

docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e VRT_S3_URI=s3://<your-bucket>/auckland_2024.vrt \
  -p 8080:80 \
  <your-image>
```

### 3. Test WMS

GetCapabilities:
```
http://localhost:8080/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities
```

GetMap (Auckland 2024, EPSG:2193):
```
http://localhost:8080/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=auckland-2024&CRS=EPSG:2193&BBOX=5920000,1750000,5930000,1760000&WIDTH=256&HEIGHT=256&FORMAT=image/png
```

> **WMS 1.3.0 axis order:** EPSG:2193 (NZTM) uses Northing,Easting order — BBOX is `minN,minE,maxN,maxE`.

### 4. Open the viewer

Open `viewer/index.html` in your browser. It loads a Leaflet map with the WMS layer pre-configured for `http://localhost:8080/mapserv`. The URL can be changed in the toolbar to point at any deployed instance.

---

## CI/CD

GitHub Actions builds and pushes the image to ECR on every push to `main`. Authentication uses OIDC — no long-lived AWS keys are stored in GitHub.

**Required GitHub Actions variables:**
- `AWS_ACCOUNT_ID` — your 12-digit AWS account ID

**Required AWS setup:**
- IAM role `github-actions-mapserver` with OIDC trust policy (see `iam/trust-policy.json`)
- ECR repo named `mapserver-docker-cloudnative`

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
