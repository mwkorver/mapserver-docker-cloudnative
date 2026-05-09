#!/bin/bash
set -e

if [ -n "$MAPFILE_S3_URI" ]; then
    echo "Downloading mapfile from ${MAPFILE_S3_URI}..."
    aws s3 cp "${MAPFILE_S3_URI}" /usr/src/mapfiles/mapfile.map
    echo "Mapfile ready."
fi

if [ -n "$VRT_S3_URI" ]; then
    echo "Downloading VRT from ${VRT_S3_URI}..."
    aws s3 cp "${VRT_S3_URI}" /usr/src/mapfiles/mosaic.vrt
    echo "VRT ready."
fi

if [ -n "$EXTENTS_S3_URI" ]; then
    echo "Downloading tile extents from ${EXTENTS_S3_URI}..."
    aws s3 cp "${EXTENTS_S3_URI}" /usr/src/mapfiles/tile_extents.geojson
    echo "Converting to shapefile for OGC API Features..."
    ogr2ogr -f "ESRI Shapefile" /usr/src/mapfiles/tile_extents.shp \
        /usr/src/mapfiles/tile_extents.geojson \
        -t_srs EPSG:4326 -overwrite
    echo "Tile extents ready."
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
