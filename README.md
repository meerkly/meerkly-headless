# meerkly-headless

A Meerkly worker for headless servers. It opens a persistent WebSocket to the
Meerkly gateway, registers itself, and serves crawl jobs using
[invisible_playwright](https://github.com/feder-cr/invisible_playwright) — a
stealth-patched Firefox.

It is a sibling of the `meerkly-desktop` and `meerkly-android` workers, not a
port of them: same gateway protocol and same wait semantics (both enforced by
conformance tests against `api-gateway/spec`), different engine and a much
smaller surface.

## Quick start with Docker

Create a `.env` next to `docker-compose.yml` with a worker key from your Meerkly
dashboard (`/devices`):

```bash
MEERKLY_API_KEY=mk_wk_your_key_here
```

Then:

```bash
docker compose up -d --build
```

The first build downloads the browser engine (~238 MB, ~544 MB unpacked), so
expect it to take a few minutes. After that:

```bash
docker compose logs -f worker
```

The worker is ready once the log shows `Registered with gateway`. The compose
healthcheck polls `/readyz`, so `docker compose ps` reporting `(healthy)` means
it is actually serving, not merely alive.

To stop it:

```bash
docker compose down
```

`stop_grace_period` is 45s because an in-flight crawl has a 30s budget — Docker's
default 10s would cut a job off mid-request.

## Running from source

Requires Python 3.11+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m invisible_playwright fetch
MEERKLY_API_KEY=mk_wk_your_key_here .venv/bin/meerkly-headless run
```

Run the tests with:

```bash
.venv/bin/python -m pytest
```

The suite includes conformance checks against `../api-gateway/spec`, so that
sibling repo must be checked out beside this one (or `SPEC_DIR` set). Those
tests fail loudly rather than skipping if it is missing — a conformance suite
that quietly disappears is worse than none.

## Configuration

Environment variables only; there is no config file. Anything blank counts as
unset.

| Variable | Default | Purpose |
|---|---|---|
| `MEERKLY_API_KEY` | — | Worker key (`mk_wk_…`). Required to pair. |
| `MEERKLY_WORKER_ID` | hostname | Replica name; also salts the derived machine ID. |
| `MEERKLY_WORKER_NAME` | worker ID | Display name in the device list. |
| `MEERKLY_MACHINE_ID` | — | Pins the machine ID. Must be a UUID. |
| `MEERKLY_HOME` | `~/.meerkly-headless` | Data directory (`/data` in the image). |
| `GATEWAY_URL` | `wss://gateway.meerkly.com/v1/connect` | |
| `ACCOUNT_BASE_URL` | `https://account.meerkly.com` | |
| `APP_ENV` | `production` | `development` points both URLs at localhost. |
| `HEALTH_PORT` | `9090` | `0` disables the health server. |
| `HEADLESS` | `true` | Any value but the literal `false` means true. See the note below — this is not a headless browser. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`. |
| `MEERKLY_LOCALE` / `MEERKLY_TIMEZONE` | auto | Pin them instead of deriving from the egress IP. |
| `MEERKLY_PROFILE_RESET_JOBS` | `50` | Jobs served before cookies and site storage are dropped. `0` disables. |
| `PROXY_URL` | — | Route all browser traffic through this proxy. A literal `<sid>` becomes a rotating session id — see [Proxy mode](#proxy-mode). |
| `ALLOW_INSECURE_GATEWAY` | `false` | Permits plaintext to a remote host. Exactly `true` to enable. |

Two image settings are deliberate and should not be "simplified":

- **`HEADLESS=true` does not mean a headless browser.** `invisible_playwright`
  runs Firefox headed-and-hidden: on Linux it starts its own Xvfb and points the
  browser at it, because a truly headless browser is detectable in ways no flag
  fixes. This is why the image installs the `xvfb` package even though nothing
  in it starts an X server directly. Worth knowing: the library's Xvfb is
  started with `-ac -listen tcp`, so the display accepts unauthenticated TCP
  connections from inside the container's network namespace. Nothing publishes
  that port, so it is not reachable from outside — but don't put this container
  on a shared network namespace with anything untrusted.
- **`INVISIBLE_CORE_AUTOFIX=off`.** Otherwise importing the engine can shell out
  to `pip install --force-reinstall` with a five-minute timeout — in an
  immutable image that means either a mutated `site-packages` or a long stall.

## Proxy mode

Set `PROXY_URL` to a full proxy URL and every crawl exits through it:

```
PROXY_URL=http://R5TfrmSltHC9-sid-<sid>-ttl-60:password@unlimited.proxiware.com:1337
```

`http`, `https`, and `socks5` schemes are supported; an explicit port is
required. The credentials are split out before the URL reaches the engine and
are **never logged** — only the credential-free server address and the current
session id appear in the logs.

**Rotating session id.** The literal token `<sid>` (anywhere in the URL — usually
inside the username, as above) is replaced with a random 6-digit id. It is
generated once at startup and **rotated on every profile reset**
(`MEERKLY_PROFILE_RESET_JOBS`), so each fresh cookie jar pairs with a fresh exit
IP. A crash relaunch reuses the same profile and therefore **keeps** the current
sid — same cookies, same exit IP.

**Fail-fast, not fail-open.** With a proxy set, the engine probes its egress IP
through the proxy at launch and Firefox is configured with no direct fallback, so
an unreachable proxy makes the worker fail to start rather than silently crawl
from the host IP. A malformed `PROXY_URL` likewise exits at startup.

**Geo.** A proxied worker stays unregioned — the gateway derives region from the
worker's own connection IP, which no longer matches the proxy's (random) exit
country. Unregioned workers are still served for requests that don't pin a
country, and are correctly skipped for country-specific ones.

**Locale.** `MEERKLY_LOCALE`/`MEERKLY_TIMEZONE` default to `auto`, which behind a
proxy derives from the exit country. Pin them if you need a fixed locale.

### Running a proxied fleet

`docker-compose.yml` runs **three replicas** by default. Each container
generates its own session id, so the three land on three different exit IPs with
no extra configuration:

```bash
PROXY_URL='http://…-sid-<sid>-ttl-60:password@unlimited.proxiware.com:1337' \
MEERKLY_API_KEY=mk_wk_… docker compose up -d --build
```

The compose file deliberately runs the replicas **without a data volume and
without `MEERKLY_WORKER_ID`**: each worker derives a distinct machine ID from the
API key and its own container hostname. Sharing a volume or a worker ID across
replicas would collide their machine IDs, and the gateway would orphan the
duplicates. The trade-off is that identity and the HTTP cache are not persisted
across container recreation — acceptable for a stateless proxied fleet.

## Data directory

`$MEERKLY_HOME` (`/data` in the container) holds the machine ID, the device
token, and the browser profile. **The device token is a plaintext `0600` file**,
not keychain-encrypted — servers have no OS keychain. That is the honest
tradeoff, not an oversight. Mount a volume at `/data` if you want it persisted,
and treat it as a secret either way. (The default 3-replica compose runs
*without* a volume on purpose — see [Running a proxied fleet](#running-a-proxied-fleet).)

Running without a persisted volume is survivable: with an API key set, the
machine ID is derived from the key and the worker ID, so a recreated container
keeps its identity. Note the corollary — **rotating the worker key changes
derived machine IDs**. If an identity must survive rotation, give it a volume or
set `MEERKLY_MACHINE_ID`.

## Troubleshooting

**"This worker is not paired."** No `MEERKLY_API_KEY` and no stored device token.
Create a worker key in the dashboard under `/devices`.

**Build fails on `libasound2`.** Newer Debian bases renamed it; use
`libasound2t64` in the Dockerfile.

**"Could not reach Meerkly" pointing at localhost.** Inside a container,
localhost is the container. `APP_ENV=development` defaults both URLs to
localhost, which is right when running from source on the host and wrong in
Docker. Name the host explicitly in `.env`:

```bash
APP_ENV=development
ACCOUNT_BASE_URL=http://host.docker.internal:3000
GATEWAY_URL=ws://host.docker.internal:8080/v1/connect
```

`docker-compose.yml` already maps `host.docker.internal` (needed on Linux), and
the transport guard treats it as host-local, so this does not need
`ALLOW_INSECURE_GATEWAY`. If the local service binds only to `127.0.0.1`, the
container still cannot reach it — bind it to `0.0.0.0` as well.

**Container stays `starting` and never reaches `healthy`.** Read the logs — this
is nearly always enrollment or registration failing, not a broken probe.
