#!/bin/bash
set -e

# Optional override: pull a hand-written mapfile from S3 instead of
# generating from collections.json. Escape hatch — not used by the
# default CDK deployment.
if [ -n "$MAPFILE_S3_URI" ]; then
    echo "Downloading mapfile from ${MAPFILE_S3_URI}..."
    if aws s3 cp "${MAPFILE_S3_URI}" /usr/src/mapfiles/mapfile.map; then
        echo "Mapfile ready."
    else
        echo "WARN: mapfile download failed; falling back to generator."
    fi
fi

# Fetch DB credentials from Secrets Manager if DB_SECRET_ARN is set.
# The mapfile_generator picks POSTGIS or OGR backend per collection
# based on DB_HOST being set plus the per-collection postgis flag.
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
export MAPSERVER_NUMPROCS="${MAPSERVER_NUMPROCS:-6}"
if ! [[ "$MAPSERVER_NUMPROCS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: MAPSERVER_NUMPROCS must be a positive integer; got '${MAPSERVER_NUMPROCS}'." >&2
    exit 1
fi

sed -i "s/^numprocs=.*/numprocs=${MAPSERVER_NUMPROCS}/" /etc/supervisor/conf.d/supervisord.conf

# Default to allowing admin writes when running without a DB (local dev
# mode); deployed stack sets ADMIN_WRITE_ENABLED explicitly via CDK.
if [ -z "$ADMIN_WRITE_ENABLED" ]; then
    if [ -z "$DB_SECRET_ARN" ] && [ -z "$DB_HOST" ]; then
        export ADMIN_WRITE_ENABLED="true"
    else
        export ADMIN_WRITE_ENABLED="false"
    fi
fi
case "${ADMIN_WRITE_ENABLED,,}" in
    1|true|yes) ADMIN_WRITE_ENABLED_JSON=true ;;
    *) ADMIN_WRITE_ENABLED_JSON=false ;;
esac

cat >/usr/src/admin/config.json <<EOF
{
  "mapserverNumprocs": ${MAPSERVER_NUMPROCS},
  "writeEnabled": ${ADMIN_WRITE_ENABLED_JSON},
  "fargateCpu": "${FARGATE_CPU:-4096}",
  "fargateMemory": "${FARGATE_MEMORY:-8192}",
  "s3Signing": "${S3_SIGNING:-auto}"
}
EOF

# Generate the mapfile from collections.json unless an explicit
# MAPFILE_S3_URI override was downloaded above.
if [ -z "$MAPFILE_S3_URI" ] || [ ! -s /usr/src/mapfiles/mapfile.map ]; then
    echo "Generating mapfile from collections.json..."
    python3 /etc/mapfile_generator.py
fi

# envsubst safety net for hand-written mapfiles uploaded via MAPFILE_S3_URI
# that use ${...} placeholders. No-op for generator output.
if [ -f /usr/src/mapfiles/mapfile.map ]; then
    envsubst < /usr/src/mapfiles/mapfile.map > /usr/src/mapfiles/mapfile.rendered.map
    mv /usr/src/mapfiles/mapfile.rendered.map /usr/src/mapfiles/mapfile.map
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
