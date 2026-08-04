FROM python:3.12-slim-bookworm AS build

WORKDIR /app
COPY pyproject.toml README.md ./
COPY meerkly_worker ./meerkly_worker
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INVISIBLE_PLAYWRIGHT_CACHE_DIR=/opt/engine \
    MEERKLY_HOME=/data \
    HEALTH_PORT=9090 \
    HEADLESS=false \
    INVISIBLE_CORE_AUTOFIX=off

# tini reaps the browser's children and forwards SIGTERM -- Python as PID 1
# does neither. The rest are the patched Firefox's runtime libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        xvfb \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY --from=build /install /usr/local

# Fetch the engine at build time (~238MB download, ~544MB unpacked,
# sha256-verified), then make it readable by the unprivileged runtime user.
RUN python -m invisible_playwright fetch && chmod -R a+rX /opt/engine

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Crawled pages are untrusted -- never run the browser as root.
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /data \
    && chown -R worker:worker /data
USER worker

VOLUME ["/data"]
EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh", "meerkly-worker"]
CMD ["run"]
