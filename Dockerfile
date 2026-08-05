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
    HEADLESS=true \
    INVISIBLE_CORE_AUTOFIX=off

# HEADLESS=true does NOT mean a headless browser. invisible_playwright runs
# Firefox headed-and-hidden: on Linux it starts its own Xvfb and points the
# browser at it, because a truly headless browser is detectable in ways no flag
# fixes. That is why the xvfb package below is required even though nothing in
# this image starts an X server itself.

# tini reaps the browser's children and forwards SIGTERM -- Python as PID 1
# does neither. libgtk-3-0 transitively pulls in the X, atk, pango, cairo and
# gdk-pixbuf libraries Firefox needs, so they are not listed individually.
# Deliberately NOT installed: libnss3 / libnspr4. Firefox ships its own copies
# and resolves them through an $ORIGIN rpath, so the system ones would be dead
# weight -- verified by loading an HTTPS page, which cannot work without NSS.
#
# The fonts are not cosmetic. A container with only the three DejaVu families
# is a loud fingerprinting signal, since font enumeration is a standard probe
# and no real desktop looks like that. Liberation adds the metric-compatible
# Arial/Times/Courier substitutes sites actually test for, and Noto covers
# non-Latin scripts so international pages lay out correctly rather than
# rendering as tofu.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        xvfb \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libasound2 \
        libxtst6 \
        libxss1 \
        fonts-liberation \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY --from=build /install /usr/local

# Fetch the engine at build time (~238MB download, ~544MB unpacked,
# sha256-verified), then make it readable by the unprivileged runtime user.
# The engine tree stays read-only to that user on purpose: the browser runs as
# `worker`, and a compromised page should not be able to rewrite the binary it
# runs. Only the geoip cache needs to be writable -- without it the library
# cannot resolve the session locale and silently falls back to en-US while
# still taking the timezone from the egress IP, producing exactly the
# language/timezone mismatch a fingerprinter looks for.
COPY docker/warmup.py /tmp/warmup.py
RUN python -m invisible_playwright fetch \
    && mkdir -p /opt/engine/geoip \
    && python /tmp/warmup.py \
    && rm -f /tmp/warmup.py \
    && chmod -R a+rX /opt/engine

# Crawled pages are untrusted -- never run the browser as root.
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /data \
    && chown -R worker:worker /data /opt/engine/geoip
USER worker

VOLUME ["/data"]
EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "meerkly-worker"]
CMD ["run"]
