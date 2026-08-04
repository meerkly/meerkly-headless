# meerkly-worker

A Meerkly worker for headless servers. It opens a persistent WebSocket to the
Meerkly gateway, registers itself, and serves crawl jobs using
[invisible_playwright](https://github.com/feder-cr/invisible_playwright) — a
stealth-patched Firefox.

It is a sibling of `meerkly-headless`, not a port of it: same gateway protocol
and same wait semantics (both enforced by conformance tests against
`api-gateway/spec`), different engine and a much smaller surface.

> **Status: under construction.** URL validation, configuration, and logging are
> implemented and tested. The engine, gateway client, and CLI entry point are
> not written yet, so the container builds but `meerkly-worker run` will not
> start until those land. See `docs/superpowers/plans/`.

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
MEERKLY_API_KEY=mk_wk_your_key_here .venv/bin/meerkly-worker run
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
| `MEERKLY_HOME` | `~/.meerkly-worker` | Data directory (`/data` in the image). |
| `GATEWAY_URL` | `wss://gateway.meerkly.com/v1/connect` | |
| `ACCOUNT_BASE_URL` | `https://account.meerkly.com` | |
| `APP_ENV` | `production` | `development` points both URLs at localhost. |
| `HEALTH_PORT` | `9090` | `0` disables the health server. |
| `HEADLESS` | `true` | Any value but the literal `false` means true. See the note below — this is not a headless browser. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`. |
| `MEERKLY_LOCALE` / `MEERKLY_TIMEZONE` | auto | Pin them instead of deriving from the egress IP. |
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

## Data directory

`$MEERKLY_HOME` (`/data` in the container, on the `worker-data` volume) holds
the machine ID, the device token, and the browser profile. **The device token is
a plaintext `0600` file**, not keychain-encrypted — servers have no OS keychain.
That is the honest tradeoff, not an oversight. Back the volume up or don't, but
treat it as a secret either way.

Losing the volume is survivable: with an API key set, the machine ID is derived
from the key and the worker ID, so a recreated container keeps its identity. Note
the corollary — **rotating the worker key changes derived machine IDs**. If an
identity must survive rotation, give it a volume or set `MEERKLY_MACHINE_ID`.

## Troubleshooting

**"This worker is not paired."** No `MEERKLY_API_KEY` and no stored device token.
Create a worker key in the dashboard under `/devices`.

**Build fails on `libasound2`.** Newer Debian bases renamed it; use
`libasound2t64` in the Dockerfile.

**"Could not reach Meerkly" pointing at localhost.** Inside a container,
localhost is the container. Use `host.docker.internal` (already mapped in
`docker-compose.yml`) or the service name.

**Container stays `starting` and never reaches `healthy`.** Read the logs — this
is nearly always enrollment or registration failing, not a broken probe.
