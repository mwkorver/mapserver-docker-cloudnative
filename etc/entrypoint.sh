#!/bin/bash
set -e

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
