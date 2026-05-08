# Stage 1: build MapServer from source
FROM --platform=linux/arm64 ghcr.io/osgeo/gdal:ubuntu-full-3.12.4 AS builder

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
    libprotobuf-c-dev && \
    curl https://download.osgeo.org/mapserver/mapserver-${MAPSERVER_VERSION}.tar.gz | tar zx -C /tmp && \
    mkdir /tmp/mapserver-${MAPSERVER_VERSION}/build && \
    cd /tmp/mapserver-${MAPSERVER_VERSION}/build && \
    cmake .. \
      -DWITH_CURL=1 \
      -DWITH_CAIRO=1 \
      -DWITH_RSVG=1 \
      -DWITH_CLIENT_WMS=1 \
      -DWITH_CLIENT_WFS=1 \
      -DPROJ_LIBRARY=/usr/local/lib/libinternalproj.so \
      -DCMAKE_C_FLAGS=-DPROJ_RENAME_SYMBOLS && \
    make -j $(nproc) && \
    make install && \
    rm -rf /tmp/mapserver-${MAPSERVER_VERSION}

# Stage 2: runtime image
FROM --platform=linux/arm64 ghcr.io/osgeo/gdal:ubuntu-full-3.12.4

COPY --from=builder /usr/local/bin/mapserv /usr/local/bin/mapserv
COPY --from=builder /usr/local/lib/libmapserver* /usr/local/lib/

RUN apt-get update && apt-get upgrade -y && \
    env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    curl \
    libfcgi0t64 \
    libcurl3t64-gnutls \
    libgeos-c1v5 \
    libpq5 \
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
    unzip && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "/tmp/awscliv2.zip" && \
    unzip -q -d /tmp /tmp/awscliv2.zip && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ADD etc /etc
RUN ln -sf /etc/nginx/sites-available/mapserver_proxy.conf /etc/nginx/sites-enabled/default && \
    chmod +x /etc/entrypoint.sh
COPY mapfiles /usr/src/mapfiles

EXPOSE 80

ENV MAPSERVER_CONFIG_FILE=/etc/mapserver/mapserver.conf

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf "http://localhost/mapserv?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities" || exit 1

CMD ["/etc/entrypoint.sh"]
