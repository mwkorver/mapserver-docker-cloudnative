#!/bin/bash
set -e

write_local_preview_mapfile() {
    cat >/usr/src/mapfiles/mapfile.map <<'EOF'
MAP
  NAME "ky-imagery"
  STATUS ON
  SIZE 256 256
  EXTENT -9968000 4368000 -9124000 4744000
  UNITS METERS

  OUTPUTFORMAT
    NAME png24
    DRIVER "AGG/PNG"
    MIMETYPE "image/png"
    IMAGEMODE RGB
    EXTENSION "png"
  END

  IMAGETYPE png24
  IMAGECOLOR 255 255 255

  PROJECTION
    "init=epsg:3857"
  END

  WEB
    METADATA
      "ows_title"           "KyFromAbove 2024 Imagery"
      "ows_abstract"        "Local preview mode without PostGIS"
      "ows_onlineresource"  "http://${PUBLIC_HOST}/mapserv"
      "ows_srs"             "EPSG:3857 EPSG:4326 EPSG:3089"
      "ows_enable_request"  "*"
      "F_enable_request"    "*"
      "wms_allow_getmap_without_styles" "true"
    END
  END

  CONFIG "MS_ERRORFILE" "stderr"
  CONFIG "MS_DEBUGLEVEL" "5"
  CONFIG "CPL_DEBUG" "ON"
  CONFIG "AWS_NO_SIGN_REQUEST" "YES"
  CONFIG "VSI_CACHE" "TRUE"
  CONFIG "VSI_CACHE_SIZE" "134217728"
  CONFIG "GDAL_DISABLE_READDIR_ON_OPEN" "TRUE"
  CONFIG "GDAL_HTTP_MULTIPLEX" "YES"
  CONFIG "GDAL_HTTP_VERSION" "2"
  CONFIG "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES" "YES"
  CONFIG "CPL_VSIL_CURL_ALLOWED_EXTENSIONS" ".tif,.tiff"

  LAYER
    NAME "cog-extents"
    TYPE POLYGON
    STATUS ON
    TEMPLATE "unused"
    EXTENT -9523006 4553015 -9484241 4591943
    CONNECTIONTYPE OGR
    CONNECTION "/usr/src/mapfiles/ky_20x20_tileindex.geojson"
    DATA "ky_20x20_tileindex"
    PROJECTION
      "init=epsg:3857"
    END
    METADATA
      "ows_title"          "Local 20x20 COG Tile Index"
      "ows_abstract"       "Local preview COG footprints"
      "ows_srs"            "EPSG:3857 EPSG:4326"
      "gml_include_items"  "all"
      "gml_featureid"      "file_name"
      "gml_geometries"     "msGeometry"
      "gml_msGeometry_type" "multipolygon"
      "ows_enable_request" "*"
      "wfs_enable_request" "*"
      "F_enable_request"   "*"
      "ows_maxfeatures"    "10000"
    END
  END

  LAYER
    NAME "ky-2024"
    TYPE RASTER
    STATUS ON
    TILEINDEX "cog-tileindex"
    TILEITEM "location"
    PROCESSING "BANDS=1,2,3"
    PROCESSING "RESAMPLE=AVERAGE"
    PROJECTION
      "init=epsg:3089"
    END
    METADATA
      "ows_title"    "KyFromAbove 2024 Season 1 (3 in)"
      "ows_abstract" "Local 20x20 COG tile index preview through nginx cache"
    END
  END

  LAYER
    NAME "cog-tileindex"
    TYPE POLYGON
    STATUS OFF
    EXTENT 4980000 3815000 5080000 3915000
    CONNECTIONTYPE OGR
    CONNECTION "/usr/src/mapfiles/ky_20x20_tileindex_3089.geojson"
    DATA "ky_20x20_tileindex_3089"
    PROJECTION
      "init=epsg:3089"
    END
  END
END
EOF
}

# Optional: download a fresh mapfile template from S3, overwriting the bundled one.
if [ -n "$MAPFILE_S3_URI" ]; then
    echo "Downloading mapfile from ${MAPFILE_S3_URI}..."
    if aws s3 cp "${MAPFILE_S3_URI}" /usr/src/mapfiles/mapfile.map; then
        echo "Mapfile ready."
    else
        echo "WARN: mapfile download failed; using bundled image default."
    fi
fi

# Fetch DB credentials from Secrets Manager and export the values that the
# mapfile template references via ${DB_*} envsubst placeholders.
if [ -n "$DB_SECRET_ARN" ]; then
    echo "Fetching DB credentials from ${DB_SECRET_ARN}..."
    SECRET_JSON=$(aws secretsmanager get-secret-value \
        --secret-id "$DB_SECRET_ARN" \
        --region "${AWS_REGION:-us-west-2}" \
        --query SecretString --output text)
    export DB_HOST=$(jq -r .host     <<< "$SECRET_JSON")
    export DB_PORT=$(jq -r .port     <<< "$SECRET_JSON")
    export DB_NAME=$(jq -r .dbname   <<< "$SECRET_JSON")
    export DB_USER=$(jq -r .username <<< "$SECRET_JSON")
    export DB_PASS=$(jq -r .password <<< "$SECRET_JSON")
    echo "DB host: ${DB_HOST}"
fi

# PUBLIC_HOST is what ows_onlineresource advertises; fall back to localhost.
export PUBLIC_HOST="${PUBLIC_HOST:-localhost}"

if [ -z "$DB_SECRET_ARN" ] && [ -z "$DB_HOST" ]; then
    echo "No database configuration found; enabling local preview mapfile."
    write_local_preview_mapfile
fi

# Render the mapfile template — substitutes ${DB_HOST}, ${DB_PORT}, etc.
if [ -f /usr/src/mapfiles/mapfile.map ]; then
    echo "Rendering mapfile with envsubst..."
    envsubst < /usr/src/mapfiles/mapfile.map > /usr/src/mapfiles/mapfile.rendered.map
    mv /usr/src/mapfiles/mapfile.rendered.map /usr/src/mapfiles/mapfile.map
fi

# Legacy paths — kept for older mapfiles that still reference local VRT or
# shapefile tileindex artifacts. Safe to leave unset when using PostGIS.
if [ -n "$VRT_S3_URI" ]; then
    echo "Downloading VRT from ${VRT_S3_URI}..."
    aws s3 cp "${VRT_S3_URI}" /usr/src/mapfiles/mosaic.vrt
fi

if [ -n "$EXTENTS_S3_URI" ]; then
    echo "Downloading tile extents from ${EXTENTS_S3_URI}..."
    aws s3 cp "${EXTENTS_S3_URI}" /usr/src/mapfiles/tile_extents.geojson
    ogr2ogr -f "ESRI Shapefile" /usr/src/mapfiles/tile_extents.shp \
        /usr/src/mapfiles/tile_extents.geojson \
        -t_srs EPSG:4326 -overwrite
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
