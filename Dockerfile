FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder

ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       ca-certificates openjdk-21-jre-headless \
       libharfbuzz0b libfreetype6 libasound2t64 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN python scripts/cpython_security.py apply-current --prefix /usr/local \
    --receipt /usr/local/share/e-rechnung-pruefer/cpython-security.json
# This build chroot has no /proc/self/exe for Java's $ORIGIN lookup. Java,
# its truststore and KoSIT are checked in the actual container with /proc.
RUN test "$TARGETARCH" = "amd64" -o "$TARGETARCH" = "arm64" \
    && python -m pip install --force-reinstall --require-hashes --only-binary=:all: \
       -r packaging/docker/requirements-builder.txt \
    && python -m pip check \
    && python scripts/dependency_audit.py capture --lock packaging/docker/requirements-builder.txt \
       --output /tmp/builder-inventory.json \
    && python scripts/dependency_lock.py verify --wheelhouse /wheels \
       --lock "packaging/docker/requirements-linux-${TARGETARCH}.txt" \
    && python scripts/docker_runtime_lock.py check \
       --parent "packaging/docker/requirements-linux-${TARGETARCH}.txt" \
       --lock "packaging/docker/requirements-runtime-${TARGETARCH}.txt" \
    && python -m venv --without-pip /opt/runtime \
    && python -m pip --python /opt/runtime install --no-index --find-links=/wheels \
       --no-compile --require-hashes --only-binary=:all: \
       -r "packaging/docker/requirements-runtime-${TARGETARCH}.txt" \
    && python -m pip --python /opt/runtime check \
    && python scripts/dependency_audit.py capture --python /opt/runtime/bin/python \
       --output /tmp/runtime-inventory.json \
    && python scripts/docker_runtime_lock.py verify \
       --parent "packaging/docker/requirements-linux-${TARGETARCH}.txt" \
       --lock "packaging/docker/requirements-runtime-${TARGETARCH}.txt" --inventory /tmp/runtime-inventory.json \
    && python scripts/build_container_rootfs.py --output /runtime-root \
       --runtime-lock "packaging/docker/requirements-runtime-${TARGETARCH}.txt" \
       --runtime-metadata "packaging/docker/requirements-runtime-${TARGETARCH}.txt.metadata.json" \
    && cp /tmp/builder-inventory.json /runtime-root/usr/share/e-rechnung-pruefer/builder-inventory.json \
    && chroot /runtime-root /opt/runtime/bin/python -c "import ssl, sqlite3, bz2, lzma, lxml.etree, PIL.Image, reportlab, uvloop"

# Only measured runtime payloads cross this boundary. Package identities and
# copyrights for every retained Debian payload remain available to scanners.
FROM scratch
COPY --from=builder /runtime-root/ /
COPY --chown=10001:10001 . /app/
ENV PATH=/opt/runtime/bin:/usr/local/bin:/usr/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    HOME=/home/appuser \
    LANG=C.UTF-8
WORKDIR /app
USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"]

CMD ["python", "-m", "app"]
