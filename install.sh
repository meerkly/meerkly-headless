#!/bin/sh
# Meerkly headless worker installer.
#
#   curl -fsSL https://meerkly.com | sh
#   curl -fsSL https://meerkly.com | MEERKLY_API_KEY=mk_wk_... sh
#
# Bootstraps the worker as a Docker container: it pulls the published image,
# writes a small managed project under ~/.meerkly (a compose file + a 0600 .env
# holding your worker key), and starts it. Re-running upgrades in place and
# reuses the stored key. The worker needs Docker because it drives a real
# Firefox under Xvfb; there is no single-binary build.
#
# Options (also settable as environment variables):
#   --key <mk_wk_...>   worker key           (MEERKLY_API_KEY)
#   --dir <path>        install directory    (MEERKLY_DIR, default ~/.meerkly)
#   --no-pull           use the local image, skip the registry pull
#   --help
set -eu

IMAGE="${MEERKLY_IMAGE:-ghcr.io/meerkly/meerkly-headless:latest}"
DIR="${MEERKLY_DIR:-$HOME/.meerkly}"
KEY="${MEERKLY_API_KEY:-}"
NO_PULL=0

# --- pretty output ----------------------------------------------------------
# Colors only on a terminal; a piped/redirected run stays plain.
if [ -t 1 ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    RESET=$(printf '\033[0m')
else
    BOLD='' DIM='' RED='' GREEN='' RESET=''
fi

say() { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
die() {
    printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2
    exit 1
}

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# --- arguments --------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --key)
            [ $# -ge 2 ] || die "--key needs a value"
            KEY="$2"
            shift 2
            ;;
        --key=*)
            KEY="${1#--key=}"
            shift
            ;;
        --dir)
            [ $# -ge 2 ] || die "--dir needs a value"
            DIR="$2"
            shift 2
            ;;
        --dir=*)
            DIR="${1#--dir=}"
            shift
            ;;
        --no-pull)
            NO_PULL=1
            shift
            ;;
        -h | --help)
            usage 0
            ;;
        *)
            die "unknown option: $1 (try --help)"
            ;;
    esac
done

# --- preflight: docker ------------------------------------------------------
command -v docker >/dev/null 2>&1 || die \
    "Docker is required but was not found. Install Docker Desktop or Docker
Engine from https://docs.docker.com/get-docker/ and run this again."

if ! docker info >/dev/null 2>&1; then
    die "Docker is installed but the daemon is not reachable. Start Docker
(open Docker Desktop, or 'sudo systemctl start docker') and run this again."
fi

# Compose v2 is the 'docker compose' subcommand. The legacy 'docker-compose'
# v1 binary is intentionally not supported.
if ! docker compose version >/dev/null 2>&1; then
    die "Docker Compose v2 is required ('docker compose'). Update Docker Desktop,
or install the compose plugin: https://docs.docker.com/compose/install/"
fi

# --- worker key -------------------------------------------------------------
ENV_FILE="$DIR/.env"

# Reuse a previously stored key when none was supplied (re-run = upgrade).
if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
    KEY=$(sed -n 's/^MEERKLY_API_KEY=//p' "$ENV_FILE" | head -n1)
    [ -n "$KEY" ] && info "Reusing the worker key stored in $ENV_FILE"
fi

# Still nothing: prompt. stdin is the curl pipe, so read the terminal directly.
if [ -z "$KEY" ]; then
    if [ -r /dev/tty ]; then
        printf '%sEnter your Meerkly worker key%s (mk_wk_...): ' "$BOLD" "$RESET" >/dev/tty
        IFS= read -r KEY </dev/tty || true
    fi
fi

[ -n "$KEY" ] || die "No worker key provided. Get one at https://account.meerkly.com/devices
then re-run with:  curl -fsSL https://meerkly.com | MEERKLY_API_KEY=mk_wk_... sh"

case "$KEY" in
    mk_wk_*) ;;
    *) die "That does not look like a worker key (expected an 'mk_wk_' prefix)." ;;
esac

# --- write the managed project ---------------------------------------------
mkdir -p "$DIR"

COMPOSE_FILE="$DIR/docker-compose.yml"

# Single service + named volume on purpose: the volume persists the machine id,
# device token, and browser profile, so identity is stable across restarts and
# upgrades (the key is only re-read to re-enrol). This differs from the repo's
# development compose, which runs a volumeless 3-replica proxy fleet.
cat >"$COMPOSE_FILE" <<EOF
# Managed by the Meerkly installer. Re-run the install one-liner to update.
services:
  worker:
    image: $IMAGE
    # Don't implicitly pull on 'up'; the installer pulls explicitly so an
    # offline restart still works.
    pull_policy: missing
    restart: unless-stopped
    env_file: .env
    volumes:
      - meerkly-data:/data
    # Lets the container reach a gateway on the host (Linux).
    extra_hosts:
      - "host.docker.internal:host-gateway"
    # The browser needs shared memory; the default 64m crashes tabs.
    shm_size: 2gb
    # An in-flight crawl has a 30s budget; Docker's default 10s SIGKILL would
    # cut it off mid-job.
    stop_grace_period: 45s
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/readyz', timeout=4).status==200 else 1)
      interval: 30s
      timeout: 5s
      start_period: 90s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  meerkly-data:
EOF

# .env holds the worker key -- write it 0600 and never echo the value. printf
# (not a heredoc) so proxy URLs with shell metacharacters are stored verbatim.
umask 077
{
    printf '# Managed by the Meerkly installer.\n'
    printf 'MEERKLY_API_KEY=%s\n' "$KEY"
    [ -n "${PROXY_URL:-}" ] && printf 'PROXY_URL=%s\n' "$PROXY_URL"
    [ -n "${LOG_LEVEL:-}" ] && printf 'LOG_LEVEL=%s\n' "$LOG_LEVEL"
    [ -n "${APP_ENV:-}" ] && printf 'APP_ENV=%s\n' "$APP_ENV"
    [ -n "${GATEWAY_URL:-}" ] && printf 'GATEWAY_URL=%s\n' "$GATEWAY_URL"
    [ -n "${ACCOUNT_BASE_URL:-}" ] && printf 'ACCOUNT_BASE_URL=%s\n' "$ACCOUNT_BASE_URL"
} >"$ENV_FILE"
chmod 600 "$ENV_FILE"
umask 022

info "Wrote $COMPOSE_FILE"
info "Wrote $ENV_FILE (0600)"

# --- start it ---------------------------------------------------------------
if [ "$NO_PULL" -eq 0 ]; then
    info "Pulling $IMAGE"
    docker compose --project-directory "$DIR" pull
fi

info "Starting the worker"
docker compose --project-directory "$DIR" up -d

# --- done -------------------------------------------------------------------
say ""
say "${GREEN}${BOLD}Meerkly worker is running.${RESET}"
say ""
say "  ${DIM}Logs   ${RESET}docker compose --project-directory $DIR logs -f"
say "  ${DIM}Stop   ${RESET}docker compose --project-directory $DIR down"
say "  ${DIM}Update ${RESET}re-run the install one-liner"
say "  ${DIM}Remove ${RESET}docker compose --project-directory $DIR down -v && rm -rf $DIR"
say ""
say "It registers as a device at ${BOLD}https://account.meerkly.com/devices${RESET}."
