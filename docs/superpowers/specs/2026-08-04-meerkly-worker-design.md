# meerkly-worker — design

**Date:** 2026-08-04
**Status:** approved
**Implementation plan:** `docs/superpowers/plans/2026-08-04-meerkly-worker.md`

## 1. Purpose

`meerkly-worker` is a Python worker for the Meerkly network. It connects to `api-gateway` over
WebSocket, registers itself, and serves `fetch` jobs by crawling with
[`invisible_playwright`](https://github.com/feder-cr/invisible_playwright) — a stealth-patched
Firefox.

It is a **new, independent project**, not a port of `meerkly-headless`. The requirement is that it
**conforms to the gateway protocol**, not that it mirrors any existing worker's internals.

### What "correct" means

`api-gateway/spec/` is the contract:

- `ws-frames.schema.json` — the `register` / `registered` / `fetch` / `result` / `error` frames
- `fetch-job.schema.json` — the wait parameters and their constants
- `vectors/` — the cases every worker must reproduce

Two pytest suites bind this worker to those files and fail loudly if the sibling checkout is
missing. Everything else is an implementation detail, written the way that is simplest in Python.

Where the spec is silent — how the "stable" DOM-quiet algorithm actually works, what the navigation
budget is — `../meerkly-headless` is a reference for *behavior*. It is not a style guide.

### Non-goals

No Kubernetes. No OAuth sign-in (worker key only). No config file. No log-file rotation. No
`status`/`diagnostics` commands. Not a replacement for `meerkly-headless` — both can run side by
side.

## 2. Shape

Python 3.11+ (required by the engine), asyncio throughout. Four runtime dependencies:
`invisible-playwright` (pinned exactly), `websockets` (auto-pongs, which the gateway's 15s ping
requires), `httpx` (one POST), `psutil` (memory reporting). Tests add `pytest`, `jsonschema`, and
`ruff`.

Eleven modules under `meerkly_worker/`: `config`, `log`, `identity`, `urls`, `fetch_spec`,
`snippets`, `browser`, `gateway`, `health`, `cli`. Configuration is environment variables only —
this runs in containers, where a baked-in config file is mostly a way to commit an API key into an
image. Logging is JSON lines on stdout, because the container runtime already collects that.

## 3. How a job is served

The gateway sends a `fetch` frame. The worker:

1. Parses it (`fetch_spec.parse_fetch_frame`), rejecting malformed frames by sending nothing — the
   job then times out gateway-side, which is the defined behavior.
2. Validates the URL with `block_private=True`. These URLs come from a remote caller, so the SSRF
   guard is what stops a crawl request from probing the worker's own network — `169.254.169.254`
   being the obvious prize. A DNS pre-check follows for hostname targets.
3. Navigates with a 30s budget (below the gateway's 35s job timeout, so the worker's own error wins
   the race), capturing the main-frame HTTP status as it goes.
4. Applies the wait condition, then extracts HTML capped at 20M characters.
5. Replies with a `result` frame in which every field is present and nulls are explicit.

Jobs are serialized behind a lock: one page, one job at a time, with `MAX_PENDING_JOBS = 3`
bounding what a flooding gateway can queue.

`httpStatus` decides whether a crawl earns credits, so it is never faked — `0` honestly means "not
captured".

## 4. Wait semantics

From `fetch-job.schema.json`, identical across every worker on the network:

- **`wait_for`** — `stable` (default), `domcontentloaded`, `networkidle`, or any other string as a
  CSS selector (wait until visible, then settle).
- **`settle_ms`** caps the stable settle (default 5000, max 25000); **`detect_ms`** caps the guard
  probe (default 200, max 25000). Neither is clamped at parse time, because the real cap also
  depends on the remaining navigation budget.
- **`wait_rules`** — an ordered list of `{if, then}`. All guards are probed concurrently; the
  **first visible one by list order** wins and its `then` becomes the wait mode. No guard matching
  is a **fallback** to `wait_for`, not a timeout.
- **`waitTimedOut`** is true from exactly two paths: `networkidle` never went idle, or a selector
  never appeared. The stable settle always resolves, at its cap if needed. A selector timeout still
  returns whatever HTML exists — best-effort capture, not a failure.

The three injected snippets live in `snippets.py`, apart from the lifecycle code, because they are
protocol artifacts rather than implementation. Two details in them are load-bearing:

- The stable observer watches `childList`, `characterData`, and `subtree` but **not `attributes`**.
  That exclusion is what stops CSS animations and class churn from resetting the quiet timer
  forever.
- A selector that throws resolves "not found" in the guard probe (keep polling — a bad guard should
  simply never match) but "timed out" in the selector wait (it can never become visible). The
  asymmetry is intentional.

## 5. Engine

`invisible_playwright` yields real Playwright objects, so `goto`, `evaluate`, `on("response")`, and
`wait_for_load_state` all work normally. Launch configuration and the reasoning:

| Setting | Value | Why |
|---|---|---|
| `profile_dir` | `$MEERKLY_HOME/profile` | Persistent profile; also makes the library yield a `BrowserContext` rather than a `Browser`. |
| `seed` | int31 derived from the machine ID | The library generates a fresh fingerprint per session otherwise. A site that saw this worker yesterday should not meet a stranger at the same IP today. |
| `humanize` | `False` | Routes pointer events through a Bezier generator costing up to 1.5s per call. We never click. |
| `headless` | from config; the container sets `false` | The library's own `headless=True` spawns Xvfb with `-ac -listen tcp` — an unauthenticated X server on the network. Our entrypoint supplies a `DISPLAY` from an Xvfb started with `-nolisten tcp`. |
| `locale` / `timezone` | auto, overridable | Coherent with the egress IP. Costs a geo lookup at launch. |

**No custom user agent, headers, viewport, or client-hint overrides.** The library's engine-level
patches are the entire fingerprint story, and it actively fights UA overrides.

### The main-world caveat

`invisible_playwright` drives Firefox over Juggler. Firefox has no CDP domains, so there is no
isolated-context guarantee — the wait snippets run in the page's main world, where page scripts can
observe or replace `MutationObserver` and the timer functions.

The mitigation is the `exec_js` wrapper: every evaluation is bounded by
`asyncio.wait_for(..., budget + 1000ms)` and returns a fallback rather than raising. A hostile page
can stall a wait; it cannot hang the worker or fabricate a result. That deadline is load-bearing
here, not defensive decoration.

## 6. Identity and pairing

`MEERKLY_API_KEY` (a `mk_wk_...` worker key) is **enrollment-only**: it grants no account reads,
serves no crawls, and is never sent to the gateway. At startup it is swapped at
`POST /api/devices/enroll` for an ordinary per-device token, after which this worker looks like any
other paired device.

Enrollment runs on **every** start, even with a token already stored — that rotates the token and
heals a revoked or copied one. Retryable failures back off on a `[1s, 3s, 8s, 20s]` schedule. A
**terminal** refusal falls back to the stored token: a revoked key must not take a running fleet
down.

Machine ID resolves as `MEERKLY_MACHINE_ID` → `machine.json` → derived UUIDv5 over
`(sha256(api_key)[:16], worker_id)` → random, and is **never derived from hardware**. The derived
step lets a container without a volume keep its identity across recreation, and mixing the key
fingerprint in means two accounts can each run a `worker-1`.

The consequence to remember: **rotating the worker key changes derived IDs.** Identity that must
survive rotation needs a volume or an explicit `MEERKLY_MACHINE_ID`. This matters because the
gateway keys live workers by machine ID — two workers sharing one means the second connects and
silently never receives a job.

Tokens are plaintext `0600` files, written temp-then-rename. Servers have no OS keychain; this is
the honest tradeoff, not an oversight.

## 7. The SSRF guard, and a Python-specific hazard

Python's `urlsplit` is **not** a WHATWG URL parser, and the gap is a security hole rather than a
cosmetic one. A browser resolves `2130706433`, `0x7f000001`, `127.1`, and `0177.0.0.1` all to
`127.0.0.1`; `urlsplit` leaves each as an opaque hostname string. A naive port of any
JavaScript-based validator would therefore let those straight through the private-host check.

`urls.as_ipv4()` reimplements the WHATWG IPv4 parser to close that. (`urlsplit` also strips the
brackets from IPv6 literals, unlike the WHATWG parser, so the code must not look for `[`.) Both the
unit tests and the end-to-end runbook check this explicitly.

## 8. Container

`python:3.12-slim-bookworm`, two stages, non-root at runtime — crawled pages are untrusted. `tini`
as PID 1 to reap the browser's children and forward `SIGTERM`. The entrypoint starts Xvfb and
`exec`s; deliberately not `xvfb-run`, which does not forward signals and would kill an in-flight
crawl instead of draining it.

`INVISIBLE_CORE_AUTOFIX=off` is **mandatory** and asserted at startup: the library's import-time pin
check will otherwise shell out to `pip install --force-reinstall` with a five-minute timeout, which
in an immutable image means either a mutated `site-packages` or a very long stall.

The engine is fetched at build time (~238MB down, ~544MB unpacked, sha256-verified) into
`/opt/engine` and made world-readable.

No seccomp profile — that existed for Chromium's user-namespace sandbox, and this is Firefox.

`docker-compose.yml` runs **one service**: a named volume for `/data`, `shm_size: 2gb`,
`stop_grace_period: 45s` (a crawl has a 30s budget), a `/readyz` healthcheck, and an empty-string
default for `MEERKLY_API_KEY` so `down`/`logs`/`ps` work without a key present.

## 9. Risks worth tracking

1. **`invisible_playwright` is young** — beta, 14 releases in the week before this design, and
   exact-pinned to `invisible-core` itself. The wrapper version is pinned exactly in
   `pyproject.toml`, and a bump means re-running the verification runbook.
2. **Firefox's post-failure navigation behavior is assumed, not measured.** Chromium commits its
   error page *after* `goto` rejects, and left in flight that commit interrupts the next job — a
   real bug once, where one unreachable URL poisoned two following crawls. The drain here matches
   `about:neterror`; the runbook verifies it empirically with three consecutive jobs and records
   the real URL.
3. **A network call happens at every launch** (the egress-IP geo lookup behind auto locale and
   timezone), so a launch can fail for reasons unrelated to the crawl.
