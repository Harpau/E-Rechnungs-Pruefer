FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY packaging/docker/requirements-linux-*.txt /tmp/docker-locks/
RUN test "$TARGETARCH" = "amd64" -o "$TARGETARCH" = "arm64" \
    && python -m pip install --force-reinstall --require-hashes --only-binary=:all: \
       -r "/tmp/docker-locks/requirements-linux-${TARGETARCH}.txt" \
    && python -m pip check \
    && rm -rf /tmp/docker-locks

COPY . .
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/vendor \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()" || exit 1

CMD ["python", "-m", "app"]
