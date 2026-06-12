# Build only GDAL's Parquet plugin. The source index files need direct Parquet
# mode because Arrow dataset mode does not expose their DuckDB-written schema.
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.12.4 AS parquet-builder
ARG GDAL_VERSION=3.12.4
COPY docker/gdal-parquet-force-direct.patch /tmp/gdal-parquet-force-direct.patch
RUN apt-get update && \
    env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    patch \
    libarrow-dev=21.0.0-1 \
    libparquet-dev=21.0.0-1 && \
    curl -fsSL "https://github.com/OSGeo/gdal/archive/refs/tags/v${GDAL_VERSION}.tar.gz" \
      | tar xz -C /tmp && \
    cd "/tmp/gdal-${GDAL_VERSION}" && \
    patch -p1 < /tmp/gdal-parquet-force-direct.patch && \
    cmake -S ogr/ogrsf_frmts/parquet -B /tmp/parquet-build \
      -DCMAKE_BUILD_TYPE=Release && \
    cmake --build /tmp/parquet-build --parallel "$(nproc)" && \
    mkdir -p /opt/gdalplugins && \
    cp /tmp/parquet-build/ogr_Parquet.so /opt/gdalplugins/

# Stage 1: build MapServer from source against the compact GDAL runtime.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.12.4 AS builder

ARG MAPSERVER_VERSION=8.6.3

RUN apt-get update && \
    env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    libcurl4-gnutls-dev \
    libfcgi-dev \
    libgeos-dev \
    libpq-dev \
    libxml2-dev \
    libpng-dev \
    zlib1g-dev \
    libjpeg-turbo8-dev \
    libgif-dev \
    libcairo2-dev \
    librsvg2-dev \
    libfribidi-dev \
    libfreetype6-dev \
    libharfbuzz-dev \
    protobuf-c-compiler \
    libprotobuf-c-dev \
    nlohmann-json3-dev && \
    curl https://download.osgeo.org/mapserver/mapserver-${MAPSERVER_VERSION}.tar.gz | tar zx -C /tmp && \
    mkdir /tmp/mapserver-${MAPSERVER_VERSION}/build && \
    cd /tmp/mapserver-${MAPSERVER_VERSION}/build && \
    cmake .. \
      -DWITH_CURL=1 \
      -DWITH_CAIRO=1 \
      -DWITH_RSVG=1 \
      -DWITH_CLIENT_WMS=1 \
      -DWITH_CLIENT_WFS=1 \
      -DWITH_OGC_API_ENABLED=1 \
      -DPROJ_LIBRARY=/usr/local/lib/libinternalproj.so \
      -DCMAKE_C_FLAGS=-DPROJ_RENAME_SYMBOLS && \
    make -j $(nproc) && \
    make install && \
    strip --strip-unneeded /usr/local/bin/mapserv \
      /usr/local/lib/libmapserver.so.${MAPSERVER_VERSION} && \
    rm -rf /tmp/mapserver-${MAPSERVER_VERSION}

# Stage 2: compact runtime image
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.12.4
ARG TARGETARCH

COPY --from=builder /usr/local/bin/mapserv /usr/local/bin/mapserv
COPY --from=builder /usr/local/lib/libmapserver.so.8.6.3 /usr/local/lib/libmapserver.so.8.6.3
COPY --from=builder /usr/local/share/mapserver /usr/local/share/mapserver
COPY --from=parquet-builder /etc/apt/sources.list.d/apache-arrow.sources /etc/apt/sources.list.d/apache-arrow.sources
COPY --from=parquet-builder /usr/share/keyrings/apache-arrow-apt-source.asc /usr/share/keyrings/apache-arrow-apt-source.asc
COPY --from=parquet-builder /opt/gdalplugins/ogr_Parquet.so /usr/local/lib/gdalplugins/ogr_Parquet.so

RUN apt-get update && \
    env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
    libarrow2100 \
    libparquet2100 \
    nginx \
    supervisor \
    curl \
    libfcgi0t64 \
    libcurl3t64-gnutls \
    libgeos-c1v5 \
    libpq5 \
    python3-psycopg2 \
    python3-boto3 \
    libxml2 \
    libpng16-16t64 \
    zlib1g \
    libjpeg-turbo8 \
    libgif7 \
    libcairo2 \
    librsvg2-2 \
    libfribidi0 \
    libfreetype6 \
    libharfbuzz0b \
    libprotobuf-c1 \
    libpcre2-posix3 \
    gettext-base && \
    ln -sf libmapserver.so.8.6.3 /usr/local/lib/libmapserver.so.2 && \
    ln -sf libmapserver.so.8.6.3 /usr/local/lib/libmapserver.so && \
    ldconfig && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ADD etc /etc
RUN ln -sf /etc/nginx/sites-available/mapserver_proxy.conf /etc/nginx/sites-enabled/default && \
    chmod +x /etc/entrypoint.sh && \
    chmod +x /etc/s3_sigv4_proxy.py && \
    chmod +x /etc/admin_api.py && \
    chmod +x /etc/mapfile_generator.py && \
    chmod +x /etc/prepare_parquet_backend.py && \
    chmod +x /etc/parquet_refresh.py && \
    chmod +x /etc/fetch_startup_config.py && \
    mkdir -p /var/cache/nginx/cog && \
    chown -R www-data:www-data /var/cache/nginx
COPY mapfiles /usr/src/mapfiles
COPY viewer /usr/src/viewer
COPY admin /usr/src/admin
COPY scripts /usr/src/scripts
COPY benchmark /usr/src/benchmark

EXPOSE 80

ENV MAPSERVER_CONFIG_FILE=/etc/mapserver/mapserver.conf
ENV MAPSERVER_NUMPROCS=6
ENV GDAL_DRIVER_PATH=/usr/local/lib/gdalplugins
ENV OGR_PARQUET_FORCE_DIRECT=YES

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf "http://localhost/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities" || exit 1

CMD ["/etc/entrypoint.sh"]
