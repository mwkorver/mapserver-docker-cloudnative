#!/bin/bash
set -e

if [ -n "$VRT_S3_URI" ]; then
    echo "Downloading VRT from ${VRT_S3_URI}..."
    aws s3 cp "${VRT_S3_URI}" /usr/src/mapfiles/mosaic.vrt
    echo "VRT ready."
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
