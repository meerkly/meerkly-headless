# meerkly-headless Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python worker that connects to `api-gateway` over WebSocket, serves `fetch` jobs by crawling with `invisible_playwright` (stealth-patched Firefox), and conforms to the protocol spec in `api-gateway/spec/`.

**Architecture:** A handful of asyncio modules wired together in `cli.py`. One persistent Firefox context serves one job at a time behind a lock. Configuration is environment variables only. Pairing is a worker key swapped for a device token at startup.

**Tech Stack:** Python 3.11+, asyncio, `invisible-playwright`, `websockets`, `httpx`; `pytest` + `jsonschema` + `ruff`. Docker with Xvfb, single-service compose.

**What "correct" means here:** `api-gateway/spec/` is the contract — `ws-frames.schema.json`, `fetch-job.schema.json`, and the vectors under `spec/vectors/`. Two conformance suites (Tasks 3 and 6) bind this worker to them. Everything else is an implementation detail and should be written the way that is simplest in Python.

`../meerkly-headless` is a reference for *behavior* where the spec is silent — how the stable-settle algorithm works, what the wait budget is. It is not a style guide, and this plan does not try to mirror its module layout.

## Global Constraints

- **Python 3.11+**, required by `invisible_playwright`. `requires-python = ">=3.11"`.
- **`invisible-playwright` is pinned exactly** (`==`). It is beta, ships weekly, and exact-pins `invisible-core` itself. A bump means re-running Task 8's runbook.
- **`INVISIBLE_CORE_AUTOFIX=off` in the image.** Otherwise `import invisible_playwright` can shell out to `pip install --force-reinstall` at startup.
- **No custom user agent, headers, viewport, or client-hint overrides.** The library's engine-level patches are the whole fingerprint story and it actively fights UA overrides.
- **Never derive the machine ID from hardware.** The gateway keys live workers by `machineId`; two workers sharing one means the second connects and silently never receives a job.
- **`httpStatus` is never faked.** `0` means "not captured". It decides whether a crawl earns credits.
- **Wire fields are camelCase** (`jobId`, `waitFor`, `settleMs`, `waitRules`, `detectMs`, `finalUrl`, `waitTimedOut`, `matchedRule`, `httpStatus`); the **enrollment HTTP body is snake_case** (`machine_id`, `device_model`, `app_version`, `engine_version`). Both are fixed by their respective APIs.
- **Navigation budget is 30s**, below the gateway's 35s job timeout so the worker's own error wins the race. HTTP calls time out at 15s.
- **Secrets are written 0600.**
- **Every task ends with `ruff check . && ruff format --check . && pytest` passing.**

## File Structure

```
meerkly-headless/
├── pyproject.toml
├── .gitignore  .dockerignore  .env.example
├── Dockerfile  docker-entrypoint.sh  docker-compose.yml
├── README.md  CLAUDE.md
├── meerkly_headless/
│   ├── __init__.py       # __version__
│   ├── config.py         # environment variables -> Config
│   ├── log.py            # JSON lines to stdout
│   ├── identity.py       # machine id, device token storage, enrollment
│   ├── urls.py           # URL validation + SSRF guard
│   ├── fetch_spec.py     # spec constants, frame parsing, effective values
│   ├── snippets.py       # the injected wait JavaScript
│   ├── browser.py        # engine lifecycle + navigate_and_extract
│   ├── gateway.py        # WebSocket client
│   ├── health.py         # /healthz, /readyz
│   └── cli.py            # `meerkly-headless run`
└── tests/
    ├── conftest.py
    ├── test_config.py  test_identity.py  test_urls.py
    ├── test_fetch_spec.py    # <- spec conformance
    ├── test_snippets.py  test_browser.py
    ├── test_gateway.py       # <- spec conformance
    └── test_health.py
```

Deliberately absent: no `config.json` (environment only — this runs in containers), no log-file rotation (Docker collects stdout), no `status`/`diagnostics`/`login` commands, no OAuth, no seccomp profile, no Kubernetes.

---

### Task 1: Scaffolding, config, and logging

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `meerkly_headless/__init__.py`, `meerkly_headless/config.py`, `meerkly_headless/log.py`, `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `meerkly_headless.__version__: str`
  - `config.Config` — frozen dataclass: `gateway_url, account_base_url, api_key, worker_id, worker_name, machine_id_override, home, headless, health_port, log_level, locale, timezone, allow_insecure`
  - `config.load_config() -> Config`
  - `log.get_logger(level: str) -> Logger` with `.debug/.info/.warn/.error(message, **fields)`
  - `tests/conftest.py::spec_dir`, `::load_vector`, `::home` fixtures

- [ ] **Step 1: Create `pyproject.toml`**

Check the current version first and use it in place of `0.6.0`:

```bash
python3 -m pip index versions invisible-playwright 2>&1 | head -3
```

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "meerkly-headless"
version = "1.0.0"
description = "Meerkly server worker (invisible_playwright / Firefox)"
requires-python = ">=3.11"
dependencies = [
    "invisible-playwright==0.6.0",
    "websockets>=13,<16",
    "httpx>=0.27,<1",
    "psutil>=5.9",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "jsonschema>=4.21", "ruff>=0.6"]

[project.scripts]
meerkly-headless = "meerkly_headless.cli:main"

[tool.setuptools.packages.find]
include = ["meerkly_headless*"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `.gitignore` and `meerkly_headless/__init__.py`**

`.gitignore`:

```gitignore
__pycache__/
*.py[cod]
.venv/
*.egg-info/
dist/
build/
.pytest_cache/
.ruff_cache/
.env
```

`meerkly_headless/__init__.py`:

```python
"""Meerkly server worker built on invisible_playwright (patched Firefox)."""

__version__ = "1.0.0"
```

- [ ] **Step 3: Create `tests/conftest.py`**

```python
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    override = os.environ.get("SPEC_DIR")
    directory = (
        Path(override).resolve()
        if override
        else (REPO_ROOT.parent / "api-gateway" / "spec").resolve()
    )
    if not directory.is_dir():
        raise RuntimeError(
            f"Protocol spec not found at {directory}. Check out api-gateway beside this "
            f"repo or set SPEC_DIR. Conformance must never silently skip."
        )
    return directory


@pytest.fixture(scope="session")
def load_vector(spec_dir):
    def _load(name: str) -> dict:
        return json.loads((spec_dir / "vectors" / name).read_text())

    return _load


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "home"
    monkeypatch.setenv("MEERKLY_HOME", str(directory))
    return directory
```

The `spec_dir` fixture **raises rather than skips**: a conformance suite that quietly disappears when the sibling checkout is missing is worse than none.

- [ ] **Step 4: Write the failing config tests**

`tests/test_config.py`:

```python
import pytest

from meerkly_headless.config import load_config

ALL_VARS = [
    "GATEWAY_URL",
    "ACCOUNT_BASE_URL",
    "MEERKLY_API_KEY",
    "MEERKLY_WORKER_ID",
    "MEERKLY_WORKER_NAME",
    "MEERKLY_MACHINE_ID",
    "HEADLESS",
    "HEALTH_PORT",
    "LOG_LEVEL",
    "MEERKLY_LOCALE",
    "MEERKLY_TIMEZONE",
    "ALLOW_INSECURE_GATEWAY",
    "APP_ENV",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, home):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_production_defaults():
    cfg = load_config()
    assert cfg.gateway_url == "wss://gateway.meerkly.com/v1/connect"
    assert cfg.account_base_url == "https://account.meerkly.com"
    assert cfg.log_level == "info"
    assert cfg.health_port == 9090
    assert cfg.api_key is None


def test_development_flips_both_urls(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    cfg = load_config()
    assert cfg.gateway_url == "ws://localhost:8080/v1/connect"
    assert cfg.account_base_url == "http://localhost:3000"


def test_explicit_urls_win(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("GATEWAY_URL", "wss://staging.example.com/v1/connect")
    assert load_config().gateway_url == "wss://staging.example.com/v1/connect"


def test_api_key_is_trimmed_and_blank_is_none(monkeypatch):
    monkeypatch.setenv("MEERKLY_API_KEY", "  mk_wk_abc  ")
    assert load_config().api_key == "mk_wk_abc"
    monkeypatch.setenv("MEERKLY_API_KEY", "   ")
    assert load_config().api_key is None


def test_worker_name_defaults_to_worker_id(monkeypatch):
    monkeypatch.setenv("MEERKLY_WORKER_ID", "crawler-7")
    cfg = load_config()
    assert cfg.worker_id == "crawler-7"
    assert cfg.worker_name == "crawler-7"


def test_worker_id_defaults_to_hostname():
    cfg = load_config()
    assert cfg.worker_id
    assert cfg.worker_name == cfg.worker_id


def test_headless_is_true_unless_literal_false(monkeypatch):
    assert load_config().headless is True
    monkeypatch.setenv("HEADLESS", "false")
    assert load_config().headless is False
    monkeypatch.setenv("HEADLESS", "0")
    assert load_config().headless is True


def test_invalid_log_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "chatty")
    assert load_config().log_level == "info"


@pytest.mark.parametrize("value,expected", [("0", 0), ("8080", 8080), ("nope", 9090), ("70000", 9090)])
def test_health_port(monkeypatch, value, expected):
    monkeypatch.setenv("HEALTH_PORT", value)
    assert load_config().health_port == expected


def test_allow_insecure_must_be_exactly_true(monkeypatch):
    assert load_config().allow_insecure is False
    monkeypatch.setenv("ALLOW_INSECURE_GATEWAY", "true")
    assert load_config().allow_insecure is True
    monkeypatch.setenv("ALLOW_INSECURE_GATEWAY", "TRUE")
    assert load_config().allow_insecure is False


def test_home_honours_the_env_var(home):
    assert load_config().home == home
```

- [ ] **Step 5: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.config'`.

- [ ] **Step 6: Implement `meerkly_headless/config.py`**

```python
"""Configuration, from environment variables only.

No config file: this worker runs in containers, where environment variables are
the native mechanism and a baked-in config file is a footgun (it is also how an
API key ends up committed to an image).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

LOG_LEVELS = ("debug", "info", "warn", "error")

PROD_GATEWAY_URL = "wss://gateway.meerkly.com/v1/connect"
DEV_GATEWAY_URL = "ws://localhost:8080/v1/connect"
PROD_ACCOUNT_BASE_URL = "https://account.meerkly.com"
DEV_ACCOUNT_BASE_URL = "http://localhost:3000"
DEFAULT_HEALTH_PORT = 9090


@dataclass(frozen=True)
class Config:
    gateway_url: str
    account_base_url: str
    api_key: str | None
    worker_id: str
    worker_name: str
    machine_id_override: str | None
    home: Path
    headless: bool
    health_port: int
    log_level: str
    locale: str | None
    timezone: str | None
    allow_insecure: bool


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_config() -> Config:
    is_dev = _env("APP_ENV") == "development"
    worker_id = _env("MEERKLY_WORKER_ID") or socket.gethostname()

    log_level = (_env("LOG_LEVEL") or "info").lower()
    if log_level not in LOG_LEVELS:
        log_level = "info"

    return Config(
        gateway_url=_env("GATEWAY_URL") or (DEV_GATEWAY_URL if is_dev else PROD_GATEWAY_URL),
        account_base_url=(
            _env("ACCOUNT_BASE_URL") or (DEV_ACCOUNT_BASE_URL if is_dev else PROD_ACCOUNT_BASE_URL)
        ),
        api_key=_env("MEERKLY_API_KEY"),
        worker_id=worker_id,
        worker_name=_env("MEERKLY_WORKER_NAME") or worker_id,
        machine_id_override=_env("MEERKLY_MACHINE_ID"),
        home=Path(_env("MEERKLY_HOME") or "~/.meerkly-headless").expanduser(),
        # Any value except the literal "false" means true.
        headless=os.environ.get("HEADLESS", "").strip() != "false",
        health_port=_health_port(),
        log_level=log_level,
        locale=_env("MEERKLY_LOCALE"),
        timezone=_env("MEERKLY_TIMEZONE"),
        # Strictly "true" — a stray "1" must not disable the transport guard.
        allow_insecure=os.environ.get("ALLOW_INSECURE_GATEWAY") == "true",
    )


def _health_port() -> int:
    raw = _env("HEALTH_PORT")
    if raw is None:
        return DEFAULT_HEALTH_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_HEALTH_PORT
    return port if 0 <= port <= 65535 else DEFAULT_HEALTH_PORT
```

- [ ] **Step 7: Implement `meerkly_headless/log.py`**

Structured JSON to stdout; Docker collects it. No file rotation, no ring buffer.

```python
"""Structured logging: one JSON object per line on stdout.

Container-native — the runtime collects stdout, so there is no file rotation or
retention here. Never log page HTML or a token.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

PRIORITY = {"debug": 0, "info": 1, "warn": 2, "error": 3}


class Logger:
    def __init__(self, level: str) -> None:
        # An unknown level must not pass everything through.
        self._threshold = PRIORITY.get(level, PRIORITY["info"])

    def debug(self, message: str, **fields) -> None:
        self._emit("debug", message, fields)

    def info(self, message: str, **fields) -> None:
        self._emit("info", message, fields)

    def warn(self, message: str, **fields) -> None:
        self._emit("warn", message, fields)

    def error(self, message: str, **fields) -> None:
        self._emit("error", message, fields)

    def _emit(self, level: str, message: str, fields: dict) -> None:
        if PRIORITY[level] < self._threshold:
            return
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "message": message,
        }
        if fields:
            entry["data"] = fields
        stream = sys.stderr if level in ("warn", "error") else sys.stdout
        print(json.dumps(entry, default=str), file=stream, flush=True)


def get_logger(level: str) -> Logger:
    return Logger(level)
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
python3 -m pip install -e '.[dev]' && python3 -m pytest tests/test_config.py -v
```

Expected: 14 passed.

- [ ] **Step 9: Create `.env.example`**

```bash
# Required: a worker key from your Meerkly dashboard (/devices).
MEERKLY_API_KEY=mk_wk_replace_me

# Optional
MEERKLY_WORKER_ID=worker-1
LOG_LEVEL=info
HEALTH_PORT=9090

# Point at a local stack instead of production
# APP_ENV=development
# ALLOW_INSECURE_GATEWAY=true

# Pin the browser locale/timezone instead of deriving them from the egress IP
# MEERKLY_LOCALE=en-US
# MEERKLY_TIMEZONE=Europe/Madrid
```

- [ ] **Step 10: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add -A && git commit -m "feat: scaffolding, environment config, and JSON logging"
```

---

### Task 2: `urls.py` — validation and the SSRF guard

**Files:**
- Create: `meerkly_headless/urls.py`
- Test: `tests/test_urls.py`

**Interfaces:**
- Produces:
  - `urls.UrlCheck` — frozen dataclass `(valid: bool, url: str | None, error: str | None)`
  - `urls.check_url(value, *, block_private: bool = False) -> UrlCheck`
  - `urls.is_private_ip(address: str) -> bool`
  - `urls.as_ipv4(host: str) -> str | None`
  - `urls.resolves_to_private(url: str) -> bool` (async)

Fetch jobs arrive from a remote gateway, so `block_private=True` is what stops a crawl request from probing the worker's own network — cloud metadata at `169.254.169.254` being the obvious prize.

**Read this before writing code.** Python's `urlsplit` is not a WHATWG URL parser, and the gap is a security hole rather than a cosmetic one:

| input host | a browser resolves it to | `urlsplit(...).hostname` gives |
|---|---|---|
| `2130706433` | `127.0.0.1` | `2130706433` |
| `0x7f000001` | `127.0.0.1` | `0x7f000001` |
| `127.1` | `127.0.0.1` | `127.1` |
| `0177.0.0.1` | `127.0.0.1` | `0177.0.0.1` |

Firefox will happily resolve every one of those to loopback, so `as_ipv4()` reimplements the WHATWG IPv4 parser to catch them before the job reaches the browser. Note also that `urlsplit` **strips the brackets** from an IPv6 literal, so the code must not look for `[`.

- [ ] **Step 1: Write the failing tests**

`tests/test_urls.py`:

```python
import pytest

from meerkly_headless.urls import as_ipv4, check_url, is_private_ip, resolves_to_private
from meerkly_headless import urls as urls_module


@pytest.mark.parametrize("value", ["", "   ", None, 42, [], {}])
def test_empty_or_non_string_is_rejected(value):
    result = check_url(value)
    assert not result.valid
    assert result.error == "URL cannot be empty"


def test_https_is_prepended_when_no_scheme():
    assert check_url("example.com/path").url == "https://example.com/path"


def test_existing_scheme_and_trimming():
    assert check_url("  http://example.com/  ").url == "http://example.com/"


@pytest.mark.parametrize(
    "value",
    ["file:///etc/passwd", "chrome://settings", "about:blank", "ABOUT:BLANK", "ftp://example.com/"],
)
def test_non_http_schemes_are_rejected(value):
    assert not check_url(value).valid


def test_about_blank_is_caught_before_parsing():
    """about:blank has no '//', so only a raw-prefix check catches it."""
    result = check_url("about:blank")
    assert not result.valid
    assert "about:" in result.error


def test_missing_hostname_is_rejected():
    assert not check_url("https:///path").valid


@pytest.mark.parametrize(
    "host,expected",
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("127.0.1", "127.0.0.1"),
        ("192.168.1.1", "192.168.1.1"),
        ("3232235777", "192.168.1.1"),
        ("0", "0.0.0.0"),
    ],
)
def test_as_ipv4_handles_every_notation(host, expected):
    assert as_ipv4(host) == expected


@pytest.mark.parametrize("host", ["example.com", "", "1.2.3.4.5", "256.1.1.1", "0x", "1.2.3.999"])
def test_as_ipv4_returns_none_for_non_addresses(host):
    assert as_ipv4(host) is None


PRIVATE = [
    "localhost", "foo.localhost", "127.0.0.1", "127.53.1.9", "0.0.0.0", "10.1.2.3",
    "100.64.0.1", "169.254.169.254", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "[::1]", "[::]", "[fe80::1]", "[fc00::1]", "[fd12::1]", "[::ffff:127.0.0.1]",
    "[::ffff:7f00:1]", "2130706433", "0x7f000001", "127.1", "0177.0.0.1",
]

PUBLIC = [
    "example.com", "8.8.8.8", "1.1.1.1", "172.15.0.1", "172.32.0.1", "192.169.1.1",
    "100.63.255.255", "169.253.0.1", "11.0.0.1", "[2606:4700::1111]", "notlocalhost",
]


@pytest.mark.parametrize("host", PRIVATE)
def test_private_hosts_are_blocked_when_asked(host):
    result = check_url(f"https://{host}/", block_private=True)
    assert not result.valid, host
    assert result.error == "Private, loopback, and link-local addresses are not allowed"


@pytest.mark.parametrize("host", PRIVATE)
def test_private_hosts_pass_when_not_asked(host):
    assert check_url(f"https://{host}/").valid, host


@pytest.mark.parametrize("host", PUBLIC)
def test_public_hosts_pass(host):
    assert check_url(f"https://{host}/", block_private=True).valid, host


@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", True), ("10.0.0.1", True), ("169.254.169.254", True), ("8.8.8.8", False),
        ("::1", True), ("::", True), ("fe80::1", True), ("fec0::1", False), ("fc00::1", True),
        ("fd00::1", True), ("2606:4700::1111", False), ("::ffff:127.0.0.1", True),
        ("::ffff:7f00:1", True), ("::ffff:8.8.8.8", False), ("not-an-ip", False),
    ],
)
def test_is_private_ip(address, expected):
    assert is_private_ip(address) is expected


async def test_literals_skip_dns(monkeypatch):
    monkeypatch.setattr(urls_module, "_resolve", lambda h: pytest.fail("must not resolve"))
    assert await resolves_to_private("https://8.8.8.8/") is False


async def test_hostname_resolving_to_loopback_is_flagged(monkeypatch):
    monkeypatch.setattr(urls_module, "_resolve", lambda h: ["127.0.0.1"])
    assert await resolves_to_private("https://evil.example.com/") is True


async def test_any_private_answer_is_enough(monkeypatch):
    monkeypatch.setattr(urls_module, "_resolve", lambda h: ["93.184.216.34", "10.0.0.5"])
    assert await resolves_to_private("https://mixed.example.com/") is True


async def test_public_and_unresolvable_both_proceed(monkeypatch):
    monkeypatch.setattr(urls_module, "_resolve", lambda h: ["93.184.216.34"])
    assert await resolves_to_private("https://example.com/") is False

    def boom(host):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(urls_module, "_resolve", boom)
    assert await resolves_to_private("https://nope.invalid/") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_urls.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.urls'`.

- [ ] **Step 3: Implement `meerkly_headless/urls.py`**

```python
"""URL validation and the SSRF guard for gateway-dispatched jobs.

block_private=True is applied to every fetch job, because those URLs come from
a remote caller and must not be able to probe this worker's own network
(169.254.169.254 being the obvious target).

Hostnames are not resolved by check_url itself; resolves_to_private adds a
best-effort DNS pre-check on top. The browser resolves independently, so a
rebinding window remains by design — the real fix is network-level.
"""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

FORBIDDEN_PREFIXES = ("file:", "chrome:", "chrome-extension:", "about:")
PRIVATE_HOST_ERROR = "Private, loopback, and link-local addresses are not allowed"

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_MAPPED_HEX_RE = re.compile(r"^([0-9a-f]{1,4}):([0-9a-f]{1,4})$")


@dataclass(frozen=True)
class UrlCheck:
    valid: bool
    url: str | None
    error: str | None


def check_url(value, *, block_private: bool = False) -> UrlCheck:
    # Jobs arrive as untyped JSON, so `value` may not be a string at all.
    if not isinstance(value, str) or not value.strip():
        return UrlCheck(False, None, "URL cannot be empty")

    candidate = value.strip()

    # On the RAW string, before the https:// prepend — this is what catches
    # "about:blank", which has no "//" and would otherwise become
    # "https://about:blank".
    lowered = candidate.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if lowered.startswith(prefix):
            return UrlCheck(False, None, f"Protocol {prefix} is not allowed")

    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"

    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError as err:
        return UrlCheck(False, None, f"Invalid URL: {err}")

    if parts.scheme not in ("http", "https"):
        return UrlCheck(False, None, f"Only http and https are allowed, got: {parts.scheme}:")
    if not host:
        return UrlCheck(False, None, "URL must have a hostname")
    if block_private and _is_private_host(host):
        return UrlCheck(False, None, PRIVATE_HOST_ERROR)

    href = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, parts.fragment))
    return UrlCheck(True, href, None)


def as_ipv4(host: str) -> str | None:
    """Parse a host as IPv4 the way the WHATWG URL spec does.

    urlsplit leaves "2130706433", "0x7f000001", "127.1" and "0177.0.0.1" as
    opaque strings, but a browser resolves every one to 127.0.0.1. Without this
    the private-host guard is trivially bypassable.
    """
    if not host:
        return None

    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts = parts[:-1]  # one trailing dot is allowed
    if len(parts) > 4:
        return None

    numbers: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                if len(part) == 2:
                    return None
                numbers.append(int(part[2:], 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part[1:], 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None  # contains a letter: it is a domain, not an address

    if any(number > 255 for number in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None

    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def is_private_ip(address: str) -> bool:
    ip = address.lower()

    if ":" in ip:
        if ip in ("::", "::1"):
            return True
        if re.match(r"^fe[89ab]", ip):  # link-local fe80::/10
            return True
        if re.match(r"^f[cd]", ip):  # unique-local fc00::/7
            return True
        if ip.startswith("::ffff:"):
            # IPv4-mapped: DNS gives ::ffff:127.0.0.1, URL parsers give
            # ::ffff:7f00:1 — handle both.
            rest = ip[7:]
            if ":" in rest:
                match = _MAPPED_HEX_RE.match(rest)
                if not match:
                    return False
                hi, lo = int(match.group(1), 16), int(match.group(2), 16)
                return is_private_ip(
                    f"{(hi >> 8) & 0xFF}.{hi & 0xFF}.{(lo >> 8) & 0xFF}.{lo & 0xFF}"
                )
            return is_private_ip(rest)
        return False

    match = _IPV4_RE.match(ip)
    if not match:
        return False
    a, b = int(match.group(1)), int(match.group(2))
    return (
        a in (0, 10, 127)
        or (a == 100 and 64 <= b <= 127)  # CGNAT
        or (a == 169 and b == 254)  # link-local, incl. cloud metadata
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
    )


def _is_private_host(hostname: str) -> bool:
    host = hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # urlsplit already stripped the brackets from an IPv6 literal.
    if ":" in host:
        return is_private_ip(host)
    ipv4 = as_ipv4(host)
    return is_private_ip(ipv4) if ipv4 is not None else False


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


async def resolves_to_private(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if not host or ":" in host or as_ipv4(host) is not None:
        return False  # literals are already screened by check_url

    try:
        addresses = await asyncio.get_running_loop().run_in_executor(None, _resolve, host)
    except Exception:
        return False  # unresolvable names proceed and fail naturally
    return any(is_private_ip(address) for address in addresses)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_urls.py -v
```

Expected: all pass (roughly 90 parametrized cases).

- [ ] **Step 5: Prove the alternate-notation hole is closed**

```bash
python3 -c "
from meerkly_headless.urls import check_url
for h in ['2130706433','0x7f000001','127.1','0177.0.0.1','169.254.169.254','localhost']:
    assert not check_url(f'https://{h}/', block_private=True).valid, f'LEAK: {h}'
    print(f'{h:18} blocked')
print('SSRF guard OK')
"
```

Expected: six `blocked` lines then `SSRF guard OK`.

- [ ] **Step 6: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/urls.py tests/test_urls.py && git commit -m "feat: URL validation and SSRF guard"
```

---

### Task 3: `fetch_spec.py` — the wait contract (SPEC CONFORMANCE)

**Files:**
- Create: `meerkly_headless/fetch_spec.py`
- Test: `tests/test_fetch_spec.py`

**Interfaces:**
- Consumes: `conftest.load_vector`, `conftest.spec_dir`
- Produces:
  - Constants `WAIT_FOR_DEFAULT`, `SETTLE_DEFAULT_MS`, `SETTLE_MAX_MS`, `DETECT_DEFAULT_MS`, `DETECT_MAX_MS`, `STABLE_QUIET_MS`
  - `fetch_spec.parse_fetch_frame(msg) -> dict | None` returning **exactly the vectors' `parsed` shape**: `{"jobId", "url", "waitFor", "settleMs", "waitRules": [{"if", "then"}], "detectMs"}`
  - `fetch_spec.effective_settle_cap(settle_ms, budget_ms=inf) -> int`
  - `fetch_spec.effective_detect_probe(detect_ms, budget_ms=inf) -> int`

**This is one of the two conformance tasks.** The parser returns a plain dict with wire-shaped keys so the vector test is a direct `==` with no mapping layer to drift. If a change here would require editing a vector, stop — that is a protocol change and needs the spec plus every worker updated together.

The semantics, from `fetch-job.schema.json`:

- `wait_for`: `stable` (default) / `domcontentloaded` / `networkidle` / any other string is a CSS selector.
- `settle_ms` caps the stable settle; `detect_ms` caps the guard probe. Neither is clamped at parse time — clamping also depends on the remaining navigation budget, so it lives in the `effective_*` helpers.
- `wait_rules` is an ordered list of `{if, then}`. The **first** rule whose `if` selector is visible wins, and its `then` becomes the wait mode. No match means fall back to `wait_for` — a fallback, not a timeout.

- [ ] **Step 1: Write the failing tests**

`tests/test_fetch_spec.py`:

```python
import json
import math

import pytest

from meerkly_headless import fetch_spec


def test_constants_match_the_schema(spec_dir):
    constants = json.loads((spec_dir / "fetch-job.schema.json").read_text())["$defs"]["constants"]
    assert fetch_spec.WAIT_FOR_DEFAULT == constants["waitForDefault"]
    assert fetch_spec.SETTLE_DEFAULT_MS == constants["settleDefaultMs"]
    assert fetch_spec.SETTLE_MAX_MS == constants["settleMaxMs"]
    assert fetch_spec.DETECT_DEFAULT_MS == constants["detectDefaultMs"]
    assert fetch_spec.DETECT_MAX_MS == constants["detectMaxMs"]
    assert fetch_spec.STABLE_QUIET_MS == constants["stableQuietMs"]


def test_every_vector_case(load_vector):
    cases = load_vector("fetch-frames.json")["cases"]
    assert cases, "vector file is empty"

    for case in cases:
        parsed = fetch_spec.parse_fetch_frame(case["frame"])
        assert parsed == case["parsed"], case["name"]

        effective = case["effective"]
        assert fetch_spec.effective_settle_cap(parsed["settleMs"]) == effective["settleCapMs"]
        assert fetch_spec.effective_detect_probe(parsed["detectMs"]) == effective["detectProbeMs"]


@pytest.mark.parametrize(
    "frame",
    [
        None, "fetch", 42, [],
        {"type": "fetch", "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1"},
        {"type": "fetch", "jobId": "", "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1", "url": ""},
        {"type": "fetch", "jobId": 1, "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1", "url": None},
    ],
)
def test_malformed_frames_parse_to_none(frame):
    assert fetch_spec.parse_fetch_frame(frame) is None


def test_values_are_not_clamped_at_parse_time():
    parsed = fetch_spec.parse_fetch_frame(
        {"jobId": "j", "url": "u", "settleMs": 99999, "detectMs": 99999}
    )
    assert parsed["settleMs"] == 99999
    assert parsed["detectMs"] == 99999
    assert fetch_spec.effective_settle_cap(99999) == fetch_spec.SETTLE_MAX_MS
    assert fetch_spec.effective_detect_probe(99999) == fetch_spec.DETECT_MAX_MS


def test_negative_and_float_values():
    parsed = fetch_spec.parse_fetch_frame(
        {"jobId": "j", "url": "u", "settleMs": -5, "detectMs": 1200.9}
    )
    assert parsed["settleMs"] == 0
    assert parsed["detectMs"] == 1200


def test_booleans_are_not_numbers():
    """bool subclasses int in Python — settleMs: true must not become 1."""
    parsed = fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "settleMs": True})
    assert parsed["settleMs"] == 0


def test_rules_without_a_guard_are_dropped_and_order_survives():
    parsed = fetch_spec.parse_fetch_frame(
        {
            "jobId": "j",
            "url": "u",
            "waitRules": [
                {"if": "#first", "then": "#a"},
                {"then": "#orphan"},
                {"if": "", "then": "#empty"},
                {"if": "#second"},
                {"if": "#third", "then": 42},
                "not-an-object",
            ],
        }
    )
    assert parsed["waitRules"] == [
        {"if": "#first", "then": "#a"},
        {"if": "#second", "then": ""},
        {"if": "#third", "then": ""},
    ]


def test_wait_rules_that_are_not_a_list_are_ignored():
    assert fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "waitRules": "no"})["waitRules"] == []


@pytest.mark.parametrize("value", ["", None, 42])
def test_blank_wait_for_becomes_stable(value):
    parsed = fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "waitFor": value})
    assert parsed["waitFor"] == fetch_spec.WAIT_FOR_DEFAULT


def test_budget_is_a_third_clamp():
    assert fetch_spec.effective_settle_cap(0, 1200) == 1200
    assert fetch_spec.effective_settle_cap(30000, 1200) == 1200
    assert fetch_spec.effective_detect_probe(0, 50) == 50
    assert fetch_spec.effective_settle_cap(0, math.inf) == fetch_spec.SETTLE_DEFAULT_MS
    assert isinstance(fetch_spec.effective_settle_cap(0, 1200.7), int)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_fetch_spec.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.fetch_spec'`.

- [ ] **Step 3: Implement `meerkly_headless/fetch_spec.py`**

```python
"""The fetch-job wire contract.

Canonical source: api-gateway/spec/fetch-job.schema.json and its vectors. This
module is conformance-tested against them, so changing anything here means
changing the spec first.

parse_fetch_frame returns a plain dict with wire-shaped camelCase keys, so the
conformance test is a direct == against the vector.
"""

from __future__ import annotations

import math
from typing import Any

WAIT_FOR_DEFAULT = "stable"
SETTLE_DEFAULT_MS = 5000
SETTLE_MAX_MS = 25000
DETECT_DEFAULT_MS = 200
DETECT_MAX_MS = 25000
STABLE_QUIET_MS = 500


def _positive_int(value: Any) -> int:
    # bool subclasses int in Python, so exclude it: JSON `true` is not 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return math.floor(value) if value > 0 else 0


def parse_fetch_frame(msg: Any) -> dict | None:
    """Parse a gateway `fetch` frame; None when it is unusable.

    A None return means no result is sent at all and the job times out
    gateway-side, which is the defined behavior for a malformed frame.
    """
    if not isinstance(msg, dict):
        return None

    job_id, url = msg.get("jobId"), msg.get("url")
    if not isinstance(job_id, str) or not job_id:
        return None
    if not isinstance(url, str) or not url:
        return None

    wait_for = msg.get("waitFor")
    wait_for = wait_for if isinstance(wait_for, str) and wait_for else WAIT_FOR_DEFAULT

    rules: list[dict] = []
    raw_rules = msg.get("waitRules")
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            guard = rule.get("if")
            if not isinstance(guard, str) or not guard:
                continue
            target = rule.get("then")
            rules.append({"if": guard, "then": target if isinstance(target, str) else ""})

    return {
        "jobId": job_id,
        "url": url,
        "waitFor": wait_for,
        # Not clamped here: the cap also depends on the remaining navigation
        # budget, which only the browser knows.
        "settleMs": _positive_int(msg.get("settleMs")),
        "waitRules": rules,
        "detectMs": _positive_int(msg.get("detectMs")),
    }


def effective_settle_cap(settle_ms: int, budget_ms: float = math.inf) -> int:
    return int(min(settle_ms if settle_ms > 0 else SETTLE_DEFAULT_MS, budget_ms, SETTLE_MAX_MS))


def effective_detect_probe(detect_ms: int, budget_ms: float = math.inf) -> int:
    return int(min(detect_ms if detect_ms > 0 else DETECT_DEFAULT_MS, budget_ms, DETECT_MAX_MS))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_fetch_spec.py -v
```

Expected: all pass, with `test_every_vector_case` covering at least 6 cases.

- [ ] **Step 5: Confirm conformance fails loudly without the spec**

```bash
SPEC_DIR=/nonexistent python3 -m pytest tests/test_fetch_spec.py -q 2>&1 | tail -5
```

Expected: an error mentioning `Protocol spec not found` — **not** "skipped".

- [ ] **Step 6: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/fetch_spec.py tests/test_fetch_spec.py
git commit -m "feat: fetch spec parsing with vector conformance"
```

---

### Task 4: `identity.py` — machine ID, device token, enrollment

**Files:**
- Create: `meerkly_headless/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `config.Config`, `log.Logger`
- Produces:
  - `identity.resolve_machine_id(cfg, logger) -> str`
  - `identity.DeviceTokenStore(home, machine_id, logger)` with `.read() -> str | None`, `.write(token: str) -> None`
  - `identity.EnrollResult` — frozen dataclass `(token: str | None, error: str | None, retryable: bool)`
  - `identity.enroll(cfg, machine_id, logger) -> EnrollResult` (async, never raises)
  - `identity.obtain_device_token(cfg, machine_id, store, logger) -> str | None` (async)
  - `identity.device_info() -> dict` — the register frame's `device` object
  - `identity.ENROLL_RETRY_MS = (1000, 3000, 8000, 20000)`

Machine ID resolution: `MEERKLY_MACHINE_ID` → `$MEERKLY_HOME/machine.json` → derived UUIDv5 over `(sha256(api_key)[:16], worker_id)` → random. The derived step is what lets a container without a volume keep its identity across recreation; mixing the key fingerprint in means two accounts can each run a `worker-1`. **Rotating the key changes derived IDs** — identity that must survive rotation needs a volume or an explicit `MEERKLY_MACHINE_ID`.

Enrollment runs on **every** start, even with a token already stored: that rotates the token and heals a revoked or copied one. A **terminal** refusal falls back to the stored token, because a revoked key must not take a running fleet down.

- [ ] **Step 1: Write the failing tests**

`tests/test_identity.py`:

```python
import json
import stat
import uuid

import httpx
import pytest

from meerkly_headless import identity
from meerkly_headless.config import Config
from meerkly_headless.identity import DeviceTokenStore, EnrollResult
from meerkly_headless.log import get_logger

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"
OTHER = "3f2b7c1e-0000-4000-8000-000000000002"


def make_config(tmp_path, api_key="mk_wk_testkey", **overrides):
    base = dict(
        gateway_url="wss://gateway.meerkly.com/v1/connect",
        account_base_url="https://account.meerkly.com",
        api_key=api_key,
        worker_id="worker-1",
        worker_name="worker-1",
        machine_id_override=None,
        home=tmp_path,
        headless=False,
        health_port=0,
        log_level="error",
        locale=None,
        timezone=None,
        allow_insecure=False,
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def log():
    return get_logger("error")


# --- machine id -------------------------------------------------------------

def test_override_wins(tmp_path, log):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "machine.json").write_text(json.dumps({"machineId": str(uuid.uuid4())}))
    cfg = make_config(tmp_path, machine_id_override=MACHINE)
    assert identity.resolve_machine_id(cfg, log) == MACHINE


def test_override_must_be_a_uuid(tmp_path, log):
    cfg = make_config(tmp_path, machine_id_override="not-a-uuid")
    with pytest.raises(ValueError, match="MEERKLY_MACHINE_ID must be a UUID"):
        identity.resolve_machine_id(cfg, log)


def test_persisted_file_beats_derivation(tmp_path, log):
    (tmp_path / "machine.json").write_text(json.dumps({"machineId": OTHER}))
    assert identity.resolve_machine_id(make_config(tmp_path), log) == OTHER


def test_derivation_is_stable_across_recreation(tmp_path, log):
    """A container with no volume keeps its identity."""
    first = identity.resolve_machine_id(make_config(tmp_path), log)
    (tmp_path / "machine.json").unlink()
    assert identity.resolve_machine_id(make_config(tmp_path), log) == first
    assert uuid.UUID(first).version == 5


def test_derivation_varies_by_key_and_worker(tmp_path, log):
    a = identity.resolve_machine_id(make_config(tmp_path), log)
    (tmp_path / "machine.json").unlink()
    b = identity.resolve_machine_id(make_config(tmp_path, worker_id="worker-2"), log)
    (tmp_path / "machine.json").unlink()
    c = identity.resolve_machine_id(make_config(tmp_path, api_key="mk_wk_other"), log)
    assert len({a, b, c}) == 3


def test_random_when_no_key(tmp_path, log):
    machine = identity.resolve_machine_id(make_config(tmp_path, api_key=None), log)
    assert uuid.UUID(machine).version == 4


def test_machine_id_is_persisted(tmp_path, log):
    machine = identity.resolve_machine_id(make_config(tmp_path), log)
    assert json.loads((tmp_path / "machine.json").read_text())["machineId"] == machine


def test_garbage_machine_file_is_ignored(tmp_path, log):
    (tmp_path / "machine.json").write_text("{ broken")
    assert identity.resolve_machine_id(make_config(tmp_path), log)


def test_unwritable_home_is_survivable(tmp_path, log):
    """Read-only filesystems are supported."""
    tmp_path.chmod(0o500)
    try:
        assert identity.resolve_machine_id(make_config(tmp_path), log)
    finally:
        tmp_path.chmod(0o700)


# --- token storage ----------------------------------------------------------

def test_token_round_trip_and_permissions(tmp_path, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    assert store.read() is None

    store.write("dt_abc")
    assert store.read() == "dt_abc"
    assert DeviceTokenStore(tmp_path, MACHINE, log).read() == "dt_abc"
    assert stat.S_IMODE((tmp_path / "device.json").stat().st_mode) == 0o600


def test_token_for_another_machine_is_ignored(tmp_path, log):
    """Catches a copied data directory."""
    DeviceTokenStore(tmp_path, OTHER, log).write("dt_other")
    assert DeviceTokenStore(tmp_path, MACHINE, log).read() is None


def test_broken_token_file_is_ignored(tmp_path, log):
    (tmp_path / "device.json").write_text("{ broken")
    assert DeviceTokenStore(tmp_path, MACHINE, log).read() is None


# --- device info ------------------------------------------------------------

def test_device_info_has_exactly_the_schema_keys():
    assert set(identity.device_info()) == {
        "deviceModel", "os", "arch", "appVersion", "engineVersion",
        "cpuCores", "memoryMb", "screen", "timezone", "locale",
    }


def test_device_info_types_and_no_display():
    info = identity.device_info()
    assert isinstance(info["cpuCores"], int) and info["cpuCores"] >= 1
    assert isinstance(info["memoryMb"], int) and info["memoryMb"] >= 1
    assert info["screen"] == ""
    assert info["engineVersion"].startswith("Invisible Playwright")


def test_arch_normalisation():
    assert identity.normalize_arch("x86_64") == "x64"
    assert identity.normalize_arch("aarch64") == "arm64"
    assert identity.normalize_arch("riscv64") == "riscv64"


# --- enrollment -------------------------------------------------------------

def mount(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


async def test_enroll_success_and_request_shape(tmp_path, monkeypatch, log):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"device_token": "dt_new"})

    mount(monkeypatch, handler)
    result = await identity.enroll(make_config(tmp_path), MACHINE, log)

    assert result.token == "dt_new"
    assert seen["url"] == "https://account.meerkly.com/api/devices/enroll"
    assert seen["auth"] == "Bearer mk_wk_testkey"
    assert seen["body"]["machine_id"] == MACHINE
    assert seen["body"]["platform"] == "server"
    assert set(seen["body"]) == {
        "machine_id", "platform", "name", "device_model", "os", "arch",
        "app_version", "engine_version",
    }


@pytest.mark.parametrize("status", [401, 403, 409, 422])
async def test_terminal_statuses(tmp_path, monkeypatch, log, status):
    mount(monkeypatch, lambda r: httpx.Response(status))
    result = await identity.enroll(make_config(tmp_path), MACHINE, log)
    assert result.token is None
    assert result.retryable is False
    assert result.error


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_other_statuses_are_retryable(tmp_path, monkeypatch, log, status):
    mount(monkeypatch, lambda r: httpx.Response(status))
    assert (await identity.enroll(make_config(tmp_path), MACHINE, log)).retryable is True


async def test_missing_token_field_is_retryable(tmp_path, monkeypatch, log):
    mount(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))
    result = await identity.enroll(make_config(tmp_path), MACHINE, log)
    assert result.token is None
    assert result.retryable is True


async def test_network_error_never_raises(tmp_path, monkeypatch, log):
    def boom(request):
        raise httpx.ConnectError("refused")

    mount(monkeypatch, boom)
    result = await identity.enroll(make_config(tmp_path), MACHINE, log)
    assert result.retryable is True
    assert result.token is None


async def test_plaintext_remote_account_url_is_refused(tmp_path, monkeypatch, log):
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"device_token": "dt"})

    mount(monkeypatch, handler)
    cfg = make_config(tmp_path, account_base_url="http://account.example.com")
    result = await identity.enroll(cfg, MACHINE, log)

    assert called is False, "must refuse before sending the key"
    assert result.retryable is False


# --- obtain_device_token orchestration --------------------------------------

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(identity.asyncio, "sleep", instant)


def stub_enroll(monkeypatch, *results):
    calls = {"n": 0}

    async def fake(cfg, machine_id, logger):
        result = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return result

    monkeypatch.setattr(identity, "enroll", fake)
    return calls


async def test_enrolls_even_when_a_token_is_stored(tmp_path, monkeypatch, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    store.write("dt_old")
    calls = stub_enroll(monkeypatch, EnrollResult("dt_rotated", None, False))

    token = await identity.obtain_device_token(make_config(tmp_path), MACHINE, store, log)

    assert calls["n"] == 1
    assert token == "dt_rotated"
    assert store.read() == "dt_rotated"


async def test_retryable_failures_retry_then_give_up(tmp_path, monkeypatch, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    calls = stub_enroll(monkeypatch, EnrollResult(None, "503", True))

    token = await identity.obtain_device_token(make_config(tmp_path), MACHINE, store, log)

    assert calls["n"] == len(identity.ENROLL_RETRY_MS) + 1
    assert token is None


async def test_retryable_failure_can_recover(tmp_path, monkeypatch, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    calls = stub_enroll(
        monkeypatch, EnrollResult(None, "503", True), EnrollResult("dt_ok", None, False)
    )
    assert await identity.obtain_device_token(make_config(tmp_path), MACHINE, store, log) == "dt_ok"
    assert calls["n"] == 2


async def test_terminal_refusal_falls_back_to_the_stored_token(tmp_path, monkeypatch, log):
    """A revoked key must not take a running fleet down."""
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    store.write("dt_existing")
    calls = stub_enroll(monkeypatch, EnrollResult(None, "rejected", False))

    token = await identity.obtain_device_token(make_config(tmp_path), MACHINE, store, log)

    assert calls["n"] == 1, "terminal refusals must not be retried"
    assert token == "dt_existing"


async def test_no_api_key_uses_the_stored_token(tmp_path, monkeypatch, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    store.write("dt_stored")

    async def must_not_enroll(*args, **kwargs):
        pytest.fail("must not enroll without an API key")

    monkeypatch.setattr(identity, "enroll", must_not_enroll)
    cfg = make_config(tmp_path, api_key=None)
    assert await identity.obtain_device_token(cfg, MACHINE, store, log) == "dt_stored"


async def test_no_key_and_no_token_is_unpaired(tmp_path, log):
    store = DeviceTokenStore(tmp_path, MACHINE, log)
    cfg = make_config(tmp_path, api_key=None)
    assert await identity.obtain_device_token(cfg, MACHINE, store, log) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_identity.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.identity'`.

- [ ] **Step 3: Implement `meerkly_headless/identity.py`**

```python
"""Machine identity, device-token storage, and worker-key enrollment.

The worker key (mk_wk_..., MEERKLY_API_KEY) is enrollment-only: it grants no
account reads, serves no crawls, and is never sent to the gateway. At startup
it is swapped for an ordinary per-device token, after which this worker looks
like any other paired device to the gateway.

Tokens are plaintext 0600 files rather than keychain-encrypted, because servers
have no OS keychain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import locale as locale_module
import os
import platform
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from . import __version__
from .config import Config

MACHINE_FILE = "machine.json"
DEVICE_FILE = "device.json"
NAMESPACE = uuid.UUID("6f9b1d6c-5a2e-4f7a-9c3b-6d1e0a8f4c21")
HTTP_TIMEOUT_SEC = 15.0
ENROLL_RETRY_MS = (1000, 3000, 8000, 20000)
HOST_LOCAL = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ARCH_ALIASES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}

TERMINAL_MESSAGES = {
    401: "The worker key was rejected. Check MEERKLY_API_KEY, or create a new key at /devices.",
    403: "This worker key has reached its device limit. Raise the limit or use another key.",
    409: "This machine is already linked to another Meerkly account.",
    422: "The enrollment request was rejected (missing machine id).",
}

_browser_version: str | None = None


# --- device info ------------------------------------------------------------

def normalize_arch(machine: str) -> str:
    return _ARCH_ALIASES.get(machine.lower(), machine.lower())


def set_browser_version(value: str | None) -> None:
    global _browser_version
    _browser_version = value


def engine_version() -> str:
    try:
        wrapper = version("invisible-playwright")
    except PackageNotFoundError:
        wrapper = "unknown"
    base = f"Invisible Playwright {wrapper}"
    return f"{base} / Firefox {_browser_version}" if _browser_version else base


def device_info() -> dict:
    """The register frame's `device` object.

    The schema sets additionalProperties:false, so an extra key is a
    validation failure. All ten fields are required.
    """
    return {
        "deviceModel": socket.gethostname(),
        "os": f"{platform.system().lower()} {platform.release()}",
        "arch": normalize_arch(platform.machine()),
        "appVersion": __version__,
        "engineVersion": engine_version(),
        "cpuCores": os.cpu_count() or 1,
        "memoryMb": round(psutil.virtual_memory().total / (1024 * 1024)),
        "screen": "",  # no display
        "timezone": _safe(lambda: datetime.now().astimezone().tzname() or ""),
        "locale": _safe(lambda: (locale_module.getlocale()[0] or "").replace("_", "-")),
    }


def _safe(fn) -> str:
    try:
        return fn()
    except Exception:
        return ""


# --- machine id -------------------------------------------------------------

def resolve_machine_id(cfg: Config, logger) -> str:
    """Resolve and persist this install's machine id.

    Order: explicit override, persisted file, derived, random. Never derived
    from hardware — the gateway keys live workers by this value, so a collision
    silently orphans a worker.
    """
    if cfg.machine_id_override:
        if not _UUID_RE.match(cfg.machine_id_override):
            raise ValueError("MEERKLY_MACHINE_ID must be a UUID")
        return _persist(cfg.home, cfg.machine_id_override, "env", logger)

    stored = _read_json(cfg.home / MACHINE_FILE)
    if stored:
        candidate = stored.get("machineId")
        if isinstance(candidate, str) and _UUID_RE.match(candidate):
            return _persist(cfg.home, candidate, "file", logger)

    if cfg.api_key and cfg.worker_id:
        # Lets a container without a volume keep its identity across
        # recreation. Rotating the key changes this value.
        fingerprint = hashlib.sha256(cfg.api_key.encode()).hexdigest()[:16]
        derived = str(uuid.uuid5(NAMESPACE, f"{fingerprint}:{cfg.worker_id}"))
        return _persist(cfg.home, derived, "derived", logger)

    return _persist(cfg.home, str(uuid.uuid4()), "random", logger)


def _persist(home: Path, machine_id: str, source: str, logger) -> str:
    payload = {
        "machineId": machine_id,
        "source": source,
        "updatedAt": datetime.now(UTC).isoformat(),
    }
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / MACHINE_FILE).write_text(json.dumps(payload, indent=2))
    except OSError as err:
        # Read-only filesystems are supported.
        logger.warn("Could not persist machine id", error=str(err))
    return machine_id


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- device token storage ---------------------------------------------------

class DeviceTokenStore:
    def __init__(self, home: Path, machine_id: str, logger) -> None:
        self._path = home / DEVICE_FILE
        self._machine_id = machine_id
        self._logger = logger

    def read(self) -> str | None:
        stored = _read_json(self._path)
        if not stored:
            return None

        token = stored.get("deviceToken")
        if not isinstance(token, str) or not token:
            return None

        # A token minted for another machine means this data directory was
        # copied; presenting it would collide with the original worker.
        if stored.get("machineId") != self._machine_id:
            self._logger.warn(
                "Ignoring a device token stored for a different machineId",
                expected=self._machine_id,
                found=stored.get("machineId"),
            )
            return None
        return token

    def write(self, token: str) -> None:
        payload = {
            "deviceToken": token,
            "machineId": self._machine_id,
            "registeredAt": datetime.now(UTC).isoformat(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(tmp, self._path)
        self._path.chmod(0o600)


# --- enrollment -------------------------------------------------------------

@dataclass(frozen=True)
class EnrollResult:
    token: str | None
    error: str | None
    retryable: bool


async def enroll(cfg: Config, machine_id: str, logger) -> EnrollResult:
    """Swap the worker key for a device token. Never raises."""
    base = cfg.account_base_url.rstrip("/")

    if not _transport_ok(base, cfg.allow_insecure):
        return EnrollResult(
            None,
            f"Refusing to send the worker key over plaintext http to {base}. "
            f"Use https, or set ALLOW_INSECURE_GATEWAY=true on a trusted network.",
            False,
        )

    info = device_info()
    body = {
        "machine_id": machine_id,
        "platform": "server",
        "name": cfg.worker_name,
        "device_model": info["deviceModel"],
        "os": info["os"],
        "arch": info["arch"],
        "app_version": info["appVersion"],
        "engine_version": info["engineVersion"],
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{base}/api/devices/enroll",
                json=body,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
            )
    except Exception as err:
        return EnrollResult(None, f"Could not reach Meerkly: {err}{_loopback_hint(base)}", True)

    if response.status_code in TERMINAL_MESSAGES:
        return EnrollResult(None, TERMINAL_MESSAGES[response.status_code], False)
    if response.status_code not in (200, 201):
        return EnrollResult(None, f"Enrollment failed (HTTP {response.status_code})", True)

    try:
        payload = response.json()
    except ValueError:
        payload = None

    token = payload.get("device_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        return EnrollResult(None, "Enrollment response did not include a device_token", True)

    logger.info("Enrolled with the account service", machineId=machine_id)
    return EnrollResult(token, None, False)


def _transport_ok(url: str, allow_insecure: bool) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "http":
        return True
    return parts.hostname in HOST_LOCAL or allow_insecure


def _loopback_hint(url: str) -> str:
    if not Path("/.dockerenv").exists():
        return ""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return ""
    if host not in ("localhost", "127.0.0.1", "::1"):
        return ""
    return (
        " This worker runs in a container, where localhost is the container itself — "
        "point it at host.docker.internal instead."
    )


async def obtain_device_token(cfg: Config, machine_id: str, store, logger) -> str | None:
    """Enrol (rotating the token) and return a usable device token, or None."""
    stored = store.read()
    if not cfg.api_key:
        return stored

    attempts = 1 + len(ENROLL_RETRY_MS)
    for attempt in range(attempts):
        result = await enroll(cfg, machine_id, logger)

        if result.token:
            store.write(result.token)
            return result.token

        if not result.retryable:
            logger.error("Enrollment refused", error=result.error)
            if stored:
                # A revoked key must not take a running fleet down.
                logger.warn("Continuing with the stored device token")
            return stored

        if attempt < attempts - 1:
            delay_ms = ENROLL_RETRY_MS[attempt]
            logger.warn("Enrollment failed; retrying", error=result.error, retryInMs=delay_ms)
            await asyncio.sleep(delay_ms / 1000)
        else:
            logger.error("Enrollment failed after retries", error=result.error)

    return stored
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_identity.py -v
```

Expected: all pass (roughly 35 cases with parametrization).

- [ ] **Step 5: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/identity.py tests/test_identity.py
git commit -m "feat: machine identity, token storage, and worker-key enrollment"
```

---

### Task 5: `snippets.py` and `browser.py` — the crawl engine

**Files:**
- Create: `meerkly_headless/snippets.py`, `meerkly_headless/browser.py`
- Test: `tests/test_snippets.py`, `tests/test_browser.py`

**Interfaces:**
- Consumes: `config.Config`, `log.Logger`, `fetch_spec.effective_*`, `identity.set_browser_version`
- Produces:
  - `snippets.PROBE_RULES`, `WAIT_SELECTOR_VISIBLE`, `SETTLE_STABLE`, `EXTRACT_HTML` — JS source strings
  - `browser.BrowserManager(cfg, machine_id, logger)` with `async ensure_browser()`, `async navigate_and_extract(job: dict) -> dict`, `async close()`, `is_ready() -> bool`
  - `browser.NAVIGATION_TIMEOUT_MS = 30000`, `browser.MAX_HTML_CHARS = 20_000_000`
  - `browser.fingerprint_seed(machine_id) -> int`, `browser.wait_branch(mode) -> str`, `browser.failure_result(...) -> dict`, `browser.describe_error(err) -> str`

`navigate_and_extract` returns the **result-frame shape**: `{"success", "finalUrl", "title", "html", "error", "loadedMs", "waitTimedOut", "matchedRule", "httpStatus"}`. `jobId` is added by the gateway client.

Three engine choices worth stating once, because they look like things to "improve":

- **`humanize=False`.** The library routes pointer events through a Bezier generator costing up to 1.5s per call. We never click.
- **`headless` comes from config, and the container sets `HEADLESS=false`.** The library's own `headless=True` spawns an Xvfb with `-ac -listen tcp` — an unauthenticated X server on the network. Our entrypoint provides a `DISPLAY` from an Xvfb started with `-nolisten tcp` instead.
- **A stable seed derived from the machine ID.** The library generates a fresh fingerprint per session otherwise; a site that saw this worker yesterday should not meet a stranger at the same IP today.

The wait snippets run in the page's **main world**. `invisible_playwright` drives Firefox over Juggler, which has no CDP domains and therefore no isolated-context guarantee. `exec_js`'s outer deadline is what stops a hostile page from stalling a job — it is load-bearing here, not defensive decoration.

- [ ] **Step 1: Write the failing snippet tests**

`tests/test_snippets.py`:

```python
from meerkly_headless import snippets


def test_settle_observer_excludes_attributes():
    """The single most load-bearing detail: attribute churn (CSS animations,
    class toggles) must not keep resetting the quiet timer forever."""
    assert "childList: true" in snippets.SETTLE_STABLE
    assert "characterData: true" in snippets.SETTLE_STABLE
    assert "attributes" not in snippets.SETTLE_STABLE


def test_settle_quiet_window_is_500ms():
    assert "const QUIET = 500;" in snippets.SETTLE_STABLE


def test_selector_snippets_do_observe_attributes():
    """Visibility usually changes via an attribute, so these must watch them."""
    assert "attributes: true" in snippets.PROBE_RULES
    assert "attributes: true" in snippets.WAIT_SELECTOR_VISIBLE


def test_visibility_predicate_is_shared():
    marker = "getComputedStyle(el).visibility !== 'hidden'"
    assert marker in snippets.PROBE_RULES
    assert marker in snippets.WAIT_SELECTOR_VISIBLE


def test_throwing_selector_asymmetry():
    # A bad guard should simply never match, so probing continues.
    assert "catch (e) { el = null; }" in snippets.PROBE_RULES
    # A bad target selector can never become visible, so give up at once.
    assert "catch (e) { finish(true); return; }" in snippets.WAIT_SELECTOR_VISIBLE


def test_all_snippets_are_arrow_functions():
    for snippet in (
        snippets.PROBE_RULES,
        snippets.WAIT_SELECTOR_VISIBLE,
        snippets.SETTLE_STABLE,
        snippets.EXTRACT_HTML,
    ):
        assert "=>" in snippet
        assert snippet.strip().startswith("(")


def test_extract_html_caps_output():
    assert "documentElement.outerHTML" in snippets.EXTRACT_HTML
    assert "slice(0, cap)" in snippets.EXTRACT_HTML
```

- [ ] **Step 2: Implement `meerkly_headless/snippets.py`**

```python
"""JavaScript injected into the page to implement the wait conditions.

The semantics here are the protocol's, not this worker's: api-gateway/spec
defines what `stable`, a selector wait, and a wait rule mean, and the desktop
and Android workers implement the same behavior. Changing anything here means
changing the spec first.

These run in the page's MAIN WORLD — invisible_playwright drives Firefox over
Juggler, which has no isolated-context guarantee. browser.exec_js's outer
deadline is what keeps a hostile page from stalling a job.
"""

# Probe every guard selector at once; resolve the index of the first visible
# one BY LIST ORDER, or -1 at the budget. A selector that throws is treated as
# not-found and polling continues.
PROBE_RULES = """
({ sels, budget }) =>
  new Promise((resolve) => {
    const vis = (el) =>
      !!(
        el &&
        (el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
        getComputedStyle(el).visibility !== 'hidden'
      );
    let done = false;
    const finish = (idx) => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearInterval(iv);
      clearTimeout(to);
      resolve(idx);
    };
    const check = () => {
      for (let i = 0; i < sels.length; i++) {
        let el;
        try { el = document.querySelector(sels[i]); } catch (e) { el = null; }
        if (vis(el)) { finish(i); return; }
      }
    };
    const mo = new MutationObserver(check);
    const iv = setInterval(check, 200);
    const to = setTimeout(() => finish(-1), budget);
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    } catch (e) { /* ignore */ }
    check();
  })
"""

# Resolve false once the selector is visible, true on timeout. A selector that
# throws resolves true immediately — it can never become visible.
WAIT_SELECTOR_VISIBLE = """
({ sel, budget }) =>
  new Promise((resolve) => {
    const vis = (el) =>
      !!(
        el &&
        (el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
        getComputedStyle(el).visibility !== 'hidden'
      );
    let done = false;
    const finish = (timedOut) => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearInterval(iv);
      clearTimeout(to);
      resolve(timedOut);
    };
    const check = () => {
      let el;
      try { el = document.querySelector(sel); } catch (e) { finish(true); return; }
      if (vis(el)) finish(false);
    };
    const mo = new MutationObserver(check);
    const iv = setInterval(check, 200);
    const to = setTimeout(() => finish(true), budget);
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    } catch (e) { /* ignore */ }
    check();
  })
"""

# Resolve once the DOM has been structurally quiet for QUIET ms, or at `cap`.
# childList + characterData + subtree, and deliberately NOT attributes: that
# exclusion is what stops CSS animations and class churn from blocking settle
# forever. Always resolves, so it never reports a timeout.
SETTLE_STABLE = """
(cap) =>
  new Promise((resolve) => {
    const QUIET = 500;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try { mo.disconnect(); } catch (e) { /* ignore */ }
      clearTimeout(quiet);
      clearTimeout(capTimer);
      resolve();
    };
    const mo = new MutationObserver(() => {
      clearTimeout(quiet);
      quiet = setTimeout(finish, QUIET);
    });
    try {
      mo.observe(document.documentElement || document, { childList: true, subtree: true, characterData: true });
    } catch (e) { /* ignore */ }
    let quiet = setTimeout(finish, QUIET);
    const capTimer = setTimeout(finish, cap);
  })
"""

EXTRACT_HTML = """
(cap) => {
  const h = document.documentElement.outerHTML;
  return h.length > cap ? h.slice(0, cap) : h;
}
"""
```

- [ ] **Step 3: Write the failing browser tests**

These are unit-level and must not launch a real browser; the engine is exercised in Task 8.

`tests/test_browser.py`:

```python
import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from meerkly_headless import browser as browser_module
from meerkly_headless.browser import (
    MAX_HTML_CHARS,
    NAVIGATION_TIMEOUT_MS,
    clear_profile_locks,
    describe_error,
    failure_result,
    fingerprint_seed,
    is_interrupted,
    wait_branch,
)
from meerkly_headless.log import get_logger

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"


@pytest.fixture
def log():
    return get_logger("error")


def test_budget_constants():
    assert NAVIGATION_TIMEOUT_MS == 30000
    assert MAX_HTML_CHARS == 20_000_000


def test_seed_is_stable_deterministic_and_int31():
    seed = fingerprint_seed(MACHINE)
    assert seed == fingerprint_seed(MACHINE)
    assert 0 <= seed <= 0x7FFFFFFF
    assert seed != fingerprint_seed("3f2b7c1e-0000-4000-8000-000000000002")


@pytest.mark.parametrize(
    "mode,branch",
    [
        ("stable", "settle"),
        ("", "settle"),
        ("domcontentloaded", "none"),
        ("networkidle", "networkidle"),
        ("#result", "selector"),
        (".card > a[href]", "selector"),
    ],
)
def test_wait_branch_selection(mode, branch):
    assert wait_branch(mode) == branch


def test_timeout_error_message():
    err = PlaywrightTimeout("Timeout 30000ms exceeded.\nCall log:\n  - navigating")
    assert describe_error(err) == "Navigation timeout after 30000ms"


def test_other_errors_use_only_the_first_line():
    err = PlaywrightError("NS_ERROR_UNKNOWN_HOST at https://nope.invalid/\nCall log:\n  - x")
    assert describe_error(err) == "Failed to load: NS_ERROR_UNKNOWN_HOST at https://nope.invalid/"


def test_interrupted_navigation_detection():
    assert is_interrupted(PlaywrightError("Navigation interrupted by another one"))
    assert is_interrupted(PlaywrightError("navigation interrupted by another navigation"))
    assert not is_interrupted(PlaywrightError("NS_ERROR_CONNECTION_REFUSED"))
    assert not is_interrupted(PlaywrightTimeout("Timeout 30000ms exceeded."))


def test_failure_result_is_the_wire_shape():
    result = failure_result(error="Failed to load: boom", final_url=None, loaded_ms=12, status=0)
    assert set(result) == {
        "success", "finalUrl", "title", "html", "error",
        "loadedMs", "waitTimedOut", "matchedRule", "httpStatus",
    }
    assert result["success"] is False
    assert result["waitTimedOut"] is False
    assert result["matchedRule"] == -1
    assert result["title"] is None and result["html"] is None


def test_failure_result_keeps_a_live_status():
    """A failure after a committed navigation still reports the real status."""
    result = failure_result(
        error="HTML extraction failed", final_url="https://example.com/", loaded_ms=900, status=404
    )
    assert result["httpStatus"] == 404
    assert result["finalUrl"] == "https://example.com/"


def test_clear_profile_locks_removes_firefox_locks(tmp_path, log):
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("lock", ".parentlock", "parent.lock"):
        (profile / name).write_text("")
    (profile / "prefs.js").write_text("keep")

    clear_profile_locks(profile, log)

    assert not (profile / "lock").exists()
    assert not (profile / ".parentlock").exists()
    assert (profile / "prefs.js").exists()


def test_clear_profile_locks_removes_dangling_symlinks(tmp_path, log):
    """Firefox's lock is often a symlink to a dead target, so exists() is False
    and an existence guard would skip it."""
    profile = tmp_path / "profile"
    profile.mkdir()
    link = profile / "lock"
    link.symlink_to(tmp_path / "gone")
    assert not link.exists() and link.is_symlink()

    clear_profile_locks(profile, log)
    assert not link.is_symlink()


def test_clear_profile_locks_tolerates_a_missing_dir(tmp_path, log):
    clear_profile_locks(tmp_path / "nope", log)  # must not raise
```

- [ ] **Step 4: Run both test files to verify they fail**

```bash
python3 -m pytest tests/test_snippets.py tests/test_browser.py -v
```

Expected: `ModuleNotFoundError` for both modules.

- [ ] **Step 5: Implement `meerkly_headless/browser.py`**

```python
"""The crawl engine: invisible_playwright (patched Firefox).

One page, one job at a time, behind a lock. The wait semantics come from
api-gateway/spec; see snippets.py.

Deliberate engine choices, do not "improve" without re-measuring:
  * No custom user agent, headers, viewport, or client-hint overrides. The
    library's engine-level patches are the whole fingerprint story and it
    actively fights UA overrides.
  * humanize=False — we never click, and the default adds up to 1.5s per
    pointer call.
  * headless comes from config; the container sets HEADLESS=false and supplies
    its own DISPLAY, because the library's headless=True starts an Xvfb with
    `-ac -listen tcp` (an unauthenticated X server on the network).
  * A stable seed from the machine id, so this worker presents one consistent
    fingerprint across restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path

from . import snippets
from .config import Config
from .fetch_spec import effective_detect_probe, effective_settle_cap
from .identity import set_browser_version

NAVIGATION_TIMEOUT_MS = 30000
MAX_HTML_CHARS = 20_000_000
CRASH_RELAUNCH_DELAY_SEC = 1.0

# Firefox's profile lock files (Chromium's are SingletonLock/Socket/Cookie).
PROFILE_LOCKS = ("lock", ".parentlock", "parent.lock")


def fingerprint_seed(machine_id: str) -> int:
    """A stable int31 fingerprint seed derived from the machine id."""
    return int.from_bytes(hashlib.sha256(machine_id.encode()).digest()[:4], "big") & 0x7FFFFFFF


def wait_branch(mode: str) -> str:
    """Which wait implementation a mode string selects."""
    if mode == "networkidle":
        return "networkidle"
    if mode == "domcontentloaded":
        return "none"
    if mode == "stable" or not mode:
        return "settle"
    return "selector"


def describe_error(err: BaseException) -> str:
    message = str(err)
    if re.search(r"Timeout .* exceeded", message, re.IGNORECASE):
        return f"Navigation timeout after {NAVIGATION_TIMEOUT_MS}ms"
    return f"Failed to load: {message.splitlines()[0]}"


def is_interrupted(err: BaseException) -> bool:
    """A goto aborted by a still-committing navigation from the previous job."""
    return re.search(r"interrupted by another", str(err), re.IGNORECASE) is not None


def failure_result(*, error: str, final_url: str | None, loaded_ms: int, status: int) -> dict:
    return {
        "success": False,
        "finalUrl": final_url,
        "title": None,
        "html": None,
        "error": error,
        "loadedMs": loaded_ms,
        "waitTimedOut": False,
        "matchedRule": -1,
        "httpStatus": status,
    }


def clear_profile_locks(profile_dir: Path, logger) -> None:
    """Remove lock files a previous run left behind.

    Unlink unconditionally: these are often DANGLING symlinks, so exists()
    reports False and a guard would skip them. A volume outliving its container
    otherwise makes Firefox believe another process owns the profile.
    """
    for name in PROFILE_LOCKS:
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as err:
            logger.warn("Could not clear a stale profile lock", file=name, error=str(err))


class BrowserManager:
    def __init__(self, cfg: Config, machine_id: str, logger) -> None:
        self._cfg = cfg
        self._logger = logger
        self._profile_dir = cfg.home / "profile"
        self._seed = fingerprint_seed(machine_id)

        self._stack: AsyncExitStack | None = None
        self._context = None
        self._page = None
        self._closed = False
        self._launching: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def is_ready(self) -> bool:
        return (
            not self._closed
            and self._context is not None
            and self._page is not None
            and not self._page.is_closed()
        )

    async def ensure_browser(self) -> None:
        if self._closed:
            raise RuntimeError("Browser manager is closed")
        if self.is_ready():
            return
        if self._launching is None or self._launching.done():
            self._launching = asyncio.create_task(self._launch())
        await self._launching

    async def _launch(self) -> None:
        from invisible_playwright.async_api import InvisiblePlaywright

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        clear_profile_locks(self._profile_dir, self._logger)

        options: dict = {
            "seed": self._seed,
            "headless": self._cfg.headless,
            "humanize": False,
            "profile_dir": str(self._profile_dir),
        }
        if self._cfg.locale:
            options["locale"] = self._cfg.locale
        if self._cfg.timezone:
            options["timezone"] = self._cfg.timezone

        stack = AsyncExitStack()
        try:
            # profile_dir makes this yield a BrowserContext, not a Browser.
            context = await stack.enter_async_context(InvisiblePlaywright(**options))
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._context = context

        try:
            browser = context.browser
            if browser is not None:
                set_browser_version(browser.version)
        except Exception:
            pass  # not exposed on every persistent-context path

        context.on("page", self._on_extra_page)
        context.on("close", self._on_context_closed)

        pages = context.pages
        self._page = pages[0] if pages else await context.new_page()
        self._page.on("crash", self._on_crash)
        self._page.set_default_timeout(NAVIGATION_TIMEOUT_MS)

        self._logger.info("Browser ready", headless=self._cfg.headless)

    def _on_extra_page(self, page) -> None:
        # Block window.open / target=_blank: one page, one job.
        if page is not self._page:
            asyncio.create_task(self._close_quietly(page))

    async def _close_quietly(self, page) -> None:
        try:
            await page.close()
        except Exception:
            pass

    def _on_context_closed(self, _context=None) -> None:
        self._context = None
        self._page = None
        if not self._closed:
            self._logger.warn("Browser context closed unexpectedly; relaunching on the next job")

    def _on_crash(self, _page=None) -> None:
        self._logger.error("Browser page crashed; recreating")
        self._page = None
        asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        await self._teardown()
        # The delay keeps a crash loop from becoming a tight loop.
        await asyncio.sleep(CRASH_RELAUNCH_DELAY_SEC)
        if self._closed:
            return
        try:
            await self.ensure_browser()
        except Exception as err:
            self._logger.error("Relaunch after crash failed", error=str(err))

    async def _teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._context = None
        self._page = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    # --- the crawl ---------------------------------------------------------

    async def navigate_and_extract(self, job: dict) -> dict:
        async with self._lock:
            return await self._fetch(job)

    async def _fetch(self, job: dict) -> dict:
        started = time.monotonic()
        deadline = started + NAVIGATION_TIMEOUT_MS / 1000
        elapsed_ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        remaining_ms = lambda: max(0, int((deadline - time.monotonic()) * 1000))  # noqa: E731

        status = 0

        def fail(error: str, final_url: str | None) -> dict:
            # `status` is read live, so a failure after a committed navigation
            # still carries the real value.
            return failure_result(
                error=error, final_url=final_url, loaded_ms=elapsed_ms(), status=status
            )

        try:
            await self.ensure_browser()
        except Exception as err:
            return fail(f"Failed to launch browser: {err}", None)

        page = self._page
        if page is None:
            return fail("Failed to launch browser", None)

        def on_response(response) -> None:
            nonlocal status
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    status = response.status
            except Exception:
                pass  # detached-frame race

        page.on("response", on_response)
        try:
            nav_error = await self._goto(page, job["url"], remaining_ms)
            if nav_error is not None:
                await self._drain_error_page(page)
                return fail(nav_error, self._safe_url(page))

            matched_rule = -1
            rules = job["waitRules"]
            if rules:
                probe_ms = effective_detect_probe(job["detectMs"], remaining_ms())
                matched_rule = await self.exec_js(
                    page,
                    snippets.PROBE_RULES,
                    {"sels": [rule["if"] for rule in rules], "budget": probe_ms},
                    probe_ms,
                    -1,
                )

            mode = rules[matched_rule]["then"] if matched_rule >= 0 else job["waitFor"]
            branch = wait_branch(mode)
            wait_timed_out = False

            if branch == "networkidle":
                wait_timed_out = await self._network_idle(page, remaining_ms())
            elif branch == "settle":
                await self._settle(page, job["settleMs"], remaining_ms())
            elif branch == "selector":
                wait_timed_out = await self.exec_js(
                    page,
                    snippets.WAIT_SELECTOR_VISIBLE,
                    {"sel": mode, "budget": remaining_ms()},
                    remaining_ms(),
                    True,
                )
                if not wait_timed_out:
                    await self._settle(page, job["settleMs"], remaining_ms())
            # branch == "none": domcontentloaded, capture right away

            html = await self.exec_js(
                page, snippets.EXTRACT_HTML, MAX_HTML_CHARS, remaining_ms(), None
            )
            if not isinstance(html, str):
                result = fail("HTML extraction failed or timed out", self._safe_url(page))
                result["title"] = await self._safe_title(page)
                return result
            if len(html) >= MAX_HTML_CHARS:
                self._logger.warn("Extracted HTML truncated at cap", cap=MAX_HTML_CHARS)

            return {
                "success": True,
                "finalUrl": self._safe_url(page),
                "title": await self._safe_title(page),
                "html": html,
                "error": None,
                "loadedMs": elapsed_ms(),
                "waitTimedOut": wait_timed_out,
                "matchedRule": matched_rule,
                "httpStatus": status,
            }
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass  # the page may be gone after a crash

    async def exec_js(self, page, expression: str, arg, budget_ms: int, fallback):
        """Evaluate in the page, resolving to `fallback` instead of raising.

        The +1s grace lets the in-page cap timers win while the renderer is
        healthy. Load-bearing: the snippets run in the page's main world, so
        without this deadline a hostile page could stall a job indefinitely.
        """
        try:
            return await asyncio.wait_for(
                page.evaluate(expression, arg), timeout=(budget_ms + 1000) / 1000
            )
        except Exception:
            return fallback

    async def _settle(self, page, settle_ms: int, budget_ms: int) -> None:
        cap = effective_settle_cap(settle_ms, budget_ms)
        await self.exec_js(page, snippets.SETTLE_STABLE, cap, cap, None)

    async def _network_idle(self, page, budget_ms: int) -> bool:
        try:
            await page.wait_for_load_state("networkidle", timeout=budget_ms)
            return False
        except Exception:
            return True

    async def _goto(self, page, url: str, remaining_ms) -> str | None:
        """None on success, otherwise a caller-facing error string."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms())
            return None
        except Exception as err:
            if not is_interrupted(err) or remaining_ms() < 1000:
                return describe_error(err)
            self._logger.debug("Navigation interrupted by a stale one; retrying once", url=url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms())
            return None
        except Exception as err:
            return describe_error(err)

    async def _drain_error_page(self, page) -> None:
        """Absorb the error-page commit that lands AFTER goto rejects.

        The browser commits its network-error page after the goto has already
        failed; left in flight, that commit interrupts the NEXT job's
        navigation. Task 8 verifies this empirically — one unreachable URL must
        not poison the following crawls.
        """
        try:
            await page.wait_for_url(re.compile(r"^about:neterror"), timeout=500)
        except Exception:
            pass  # nothing to drain

    def _safe_url(self, page) -> str | None:
        try:
            return page.url
        except Exception:
            return None

    async def _safe_title(self, page) -> str | None:
        try:
            return await page.title()
        except Exception:
            return None
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_snippets.py tests/test_browser.py -v
```

Expected: 7 + 11 passed.

- [ ] **Step 7: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/snippets.py meerkly_headless/browser.py tests/test_snippets.py tests/test_browser.py
git commit -m "feat: crawl engine with spec wait semantics"
```

---

### Task 6: `gateway.py` — the WebSocket client (SPEC CONFORMANCE)

**Files:**
- Create: `meerkly_headless/gateway.py`
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `identity.device_info`, `fetch_spec.parse_fetch_frame`, `urls.check_url`, `urls.resolves_to_private`, `browser.BrowserManager`
- Produces:
  - `gateway.GatewayClient(cfg, machine_id, read_token, browser, logger)` with `async start()`, `async stop()`, `is_registered() -> bool`, `jobs_served: int`
  - `gateway.build_register(machine_id, device_token) -> dict`
  - `gateway.build_result(job_id, result) -> dict`
  - `gateway.next_backoff_ms(current) -> int`
  - `MAX_PENDING_JOBS = 3`, `INITIAL_BACKOFF_MS = 1000`, `MAX_BACKOFF_MS = 30000`, `BACKOFF_RESET_AFTER_MS = 10000`

**This is the second conformance task**, validating every frame against `ws-frames.schema.json`.

Protocol facts that are easy to get wrong:

- The socket carries **no query string, headers, or subprotocol**. Authentication is the `deviceToken` field of the first `register` frame, and the key is **omitted entirely** when absent — never `null`, never `""`.
- `region` is always `""`. The gateway derives it from the observed IP and ignores what a worker claims.
- Every field of a `result` frame is always present, with explicit nulls.
- The gateway pings; `websockets` pongs at the protocol layer automatically. There is no application-level heartbeat.
- `read_token` is a **callable invoked on every connect**, not a value captured once, so a rotated token is picked up without a restart.

- [ ] **Step 1: Write the failing tests**

`tests/test_gateway.py`:

```python
import json

import pytest
from jsonschema import Draft202012Validator

from meerkly_headless import gateway as gw

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"


@pytest.fixture(scope="session")
def ws_schema(spec_dir):
    return json.loads((spec_dir / "ws-frames.schema.json").read_text())


def validator_for(schema, name):
    return Draft202012Validator({**schema["$defs"][name], "$defs": schema["$defs"]})


# --- conformance against the canonical vectors ------------------------------

def test_all_ws_frame_vectors(load_vector, ws_schema):
    cases = load_vector("ws-frames.json")["cases"]
    assert cases, "vector file is empty"
    for case in cases:
        actual = validator_for(ws_schema, case["def"]).is_valid(case["frame"])
        assert actual == case["valid"], case["name"]


def test_our_register_frames_validate(ws_schema):
    validator = validator_for(ws_schema, "register")
    validator.validate(gw.build_register(MACHINE, "dt_example"))
    validator.validate(gw.build_register(MACHINE, None))


def test_our_success_result_validates(ws_schema):
    frame = gw.build_result(
        "j1",
        {
            "success": True,
            "finalUrl": "https://example.com/",
            "title": "Example",
            "html": "<html></html>",
            "error": None,
            "loadedMs": 812,
            "waitTimedOut": False,
            "matchedRule": 0,
            "httpStatus": 200,
        },
    )
    validator_for(ws_schema, "result").validate(frame)


def test_our_failure_result_validates(ws_schema):
    """Nulls on the failure path were a real source of schema drift."""
    frame = gw.build_result(
        "j2",
        {
            "success": False,
            "finalUrl": None,
            "title": None,
            "html": None,
            "error": "Navigation timeout after 30000ms",
            "loadedMs": None,
            "waitTimedOut": False,
            "matchedRule": -1,
            "httpStatus": 0,
        },
    )
    validator_for(ws_schema, "result").validate(frame)


# --- frame construction -----------------------------------------------------

def test_register_frame_fields():
    frame = gw.build_register(MACHINE, "dt_x")
    assert frame["type"] == "register"
    assert frame["machineId"] == MACHINE
    assert frame["platform"] == "server"
    assert frame["capabilities"] == ["fetch"]
    # The gateway derives region from the observed IP and ignores this.
    assert frame["region"] == ""
    assert frame["deviceToken"] == "dt_x"


def test_device_token_key_is_omitted_when_absent():
    assert "deviceToken" not in gw.build_register(MACHINE, None)


def test_result_frame_always_carries_every_field():
    frame = gw.build_result("j1", {"success": True})
    assert set(frame) == {
        "type", "jobId", "success", "finalUrl", "title", "html",
        "error", "loadedMs", "waitTimedOut", "matchedRule", "httpStatus",
    }
    assert frame["finalUrl"] is None
    assert frame["waitTimedOut"] is False
    assert frame["matchedRule"] == -1
    assert frame["httpStatus"] == 0


# --- backoff ----------------------------------------------------------------

def test_backoff_doubles_to_a_ceiling():
    assert gw.next_backoff_ms(gw.INITIAL_BACKOFF_MS) == 2000
    assert gw.next_backoff_ms(16000) == 30000
    assert gw.next_backoff_ms(30000) == 30000


def test_backoff_constants():
    assert gw.INITIAL_BACKOFF_MS == 1000
    assert gw.MAX_BACKOFF_MS == 30000
    assert gw.BACKOFF_RESET_AFTER_MS == 10000
    assert gw.MAX_PENDING_JOBS == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_gateway.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.gateway'`.

- [ ] **Step 3: Implement `meerkly_headless/gateway.py`**

```python
"""WebSocket client for api-gateway.

The frame schema is api-gateway/spec/ws-frames.schema.json (and the Go structs
in api-gateway/internal/model/device.go). This module is conformance-tested
against it.

The socket carries no query string, headers, or subprotocol: authentication is
entirely the deviceToken field of the first register frame. The gateway pings;
`websockets` pongs automatically at the protocol layer.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import websockets

from .config import Config
from .fetch_spec import parse_fetch_frame
from .identity import HOST_LOCAL, device_info
from .urls import PRIVATE_HOST_ERROR, check_url, resolves_to_private

MAX_PENDING_JOBS = 3
INITIAL_BACKOFF_MS = 1000
MAX_BACKOFF_MS = 30000
BACKOFF_RESET_AFTER_MS = 10000


def next_backoff_ms(current: int) -> int:
    return min(current * 2, MAX_BACKOFF_MS)


def build_register(machine_id: str, device_token: str | None) -> dict:
    frame = {
        "type": "register",
        "machineId": machine_id,
        "platform": "server",
        "capabilities": ["fetch"],
        # Ignored by the gateway, which derives region from the observed IP.
        "region": "",
        "device": device_info(),
    }
    # Omit the key entirely when absent — never null, never "".
    if device_token:
        frame["deviceToken"] = device_token
    return frame


def build_result(job_id: str, result: dict) -> dict:
    """Every field is always present; nulls are explicit."""
    return {
        "type": "result",
        "jobId": job_id,
        "success": bool(result.get("success")),
        "finalUrl": result.get("finalUrl"),
        "title": result.get("title"),
        "html": result.get("html"),
        "error": result.get("error"),
        "loadedMs": result.get("loadedMs"),
        "waitTimedOut": bool(result.get("waitTimedOut", False)),
        "matchedRule": result.get("matchedRule", -1),
        "httpStatus": result.get("httpStatus", 0),
    }


class GatewayClient:
    def __init__(self, cfg: Config, machine_id: str, read_token, browser, logger) -> None:
        self._cfg = cfg
        self._url = cfg.gateway_url
        self._machine_id = machine_id
        # A callable, not a value: a rotated token is picked up on reconnect.
        self._read_token = read_token
        self._browser = browser
        self._logger = logger

        self._socket = None
        self._registered = False
        self._paused = False
        self._stopped = False
        self._backoff_ms = INITIAL_BACKOFF_MS
        self._pending = 0
        self.jobs_served = 0
        self._task: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()

    def is_registered(self) -> bool:
        # An open socket alone is not readiness: an unpaired worker is accepted
        # and then rejected.
        return self._registered and self._socket is not None

    async def start(self) -> None:
        if not self._transport_ok():
            self._logger.error(
                "Refusing to connect over plaintext ws:// to a remote host",
                url=self._url,
            )
            return
        self._task = asyncio.create_task(self._run_forever())

    def _transport_ok(self) -> bool:
        try:
            parts = urlsplit(self._url)
        except ValueError:
            return False
        if parts.scheme != "ws":
            return True
        return parts.hostname in HOST_LOCAL or self._cfg.allow_insecure

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
        for job in list(self._jobs):
            job.cancel()
        if self._socket is not None:
            await self._socket.close()

    async def _run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopped and not self._paused:
            opened_at = loop.time()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._logger.warn("Gateway connection failed", error=str(err))

            self._registered = False
            self._socket = None
            if self._stopped or self._paused:
                return

            # Reset only after a connection that STAYED UP, so an
            # accept-then-reject loop does not become a 1s hammer.
            if (loop.time() - opened_at) * 1000 >= BACKOFF_RESET_AFTER_MS:
                self._backoff_ms = INITIAL_BACKOFF_MS

            await asyncio.sleep(self._backoff_ms / 1000)
            self._backoff_ms = next_backoff_ms(self._backoff_ms)

    async def _connect_once(self) -> None:
        async with websockets.connect(self._url, max_size=None) as socket:
            self._socket = socket
            token = await self._read_token()
            await socket.send(json.dumps(build_register(self._machine_id, token)))
            self._logger.info("Sent registration", machineId=self._machine_id)

            async for raw in socket:
                self._dispatch(raw)

    def _dispatch(self, raw) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            self._logger.warn("Gateway sent a non-JSON message")
            return
        if not isinstance(message, dict):
            self._logger.warn("Gateway sent a non-object message")
            return

        kind = message.get("type")
        if kind == "fetch":
            job = parse_fetch_frame(message)
            if job is None:
                # No result is sent; the job times out gateway-side.
                self._logger.warn("Gateway sent a malformed fetch frame")
                return
            task = asyncio.create_task(self._handle_fetch(job))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)
        elif kind == "registered":
            self._registered = True
            self._logger.info(
                "Registered with gateway",
                connectionId=message.get("connectionId"),
                heartbeatSec=message.get("heartbeatSec"),
            )
        elif kind == "error":
            self._handle_error(message)
        else:
            self._logger.debug("Unhandled frame", type=kind)

    def _handle_error(self, message: dict) -> None:
        code = message.get("code")
        detail = {"code": code, "message": message.get("message")}
        if code == "device_auth_failed":
            # Terminal: reconnecting with the same rejected token is pointless.
            self._paused = True
            self._logger.error("Device authentication failed; pausing reconnects", **detail)
        elif code == "verification_unavailable":
            self._logger.warn("Gateway could not verify this device; will retry", **detail)
        else:
            self._logger.warn("Gateway reported an error", **detail)

    async def _handle_fetch(self, job: dict) -> None:
        job_id = job["jobId"]

        # Bounds what a flooding gateway can queue. Checked before validation;
        # a rejected job does not count as served.
        if self._pending >= MAX_PENDING_JOBS:
            await self._send(job_id, {"success": False, "error": "Worker busy"})
            return

        self._pending += 1
        try:
            # block_private: these URLs come from a remote caller and must not
            # be able to probe this worker's own network.
            checked = check_url(job["url"], block_private=True)
            if not checked.valid:
                await self._send(job_id, {"success": False, "error": checked.error})
                return

            if await resolves_to_private(checked.url):
                await self._send(job_id, {"success": False, "error": PRIVATE_HOST_ERROR})
                return

            result = await self._browser.navigate_and_extract({**job, "url": checked.url})
            await self._send(job_id, result)
            self.jobs_served += 1
        except Exception as err:
            self._logger.error("Fetch job failed", jobId=job_id, error=str(err))
            await self._send(job_id, {"success": False, "error": f"Worker error: {err}"})
        finally:
            self._pending -= 1

    async def _send(self, job_id: str, result: dict) -> None:
        socket = self._socket
        if socket is None:
            return  # results for a dropped socket are discarded
        try:
            await socket.send(json.dumps(build_result(job_id, result)))
        except Exception as err:
            self._logger.warn("Could not send result", jobId=job_id, error=str(err))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_gateway.py -v
```

Expected: all pass, with `test_all_ws_frame_vectors` covering 14 cases.

- [ ] **Step 5: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/gateway.py tests/test_gateway.py
git commit -m "feat: gateway WebSocket client with ws-frames conformance"
```

---

### Task 7: `health.py` and `cli.py`

**Files:**
- Create: `meerkly_headless/health.py`, `meerkly_headless/cli.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces:
  - `health.HealthServer(port, machine_id, browser, gateway, logger)` with `async start()`, `async stop()`, `port: int`
  - `cli.main(argv=None) -> int`

`/healthz` asks "is the browser alive"; `/readyz` also requires gateway registration. The server starts **before** the browser so a probe sees an honest 503 rather than connection-refused.

- [ ] **Step 1: Write the failing health tests**

`tests/test_health.py`:

```python
import json

import httpx
import pytest

from meerkly_headless.health import HealthServer
from meerkly_headless.log import get_logger

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"


class FakeBrowser:
    def __init__(self, ready=True):
        self.ready = ready

    def is_ready(self):
        return self.ready


class FakeGateway:
    def __init__(self, registered=True, jobs_served=7):
        self.registered = registered
        self.jobs_served = jobs_served

    def is_registered(self):
        return self.registered


@pytest.fixture
def log():
    return get_logger("error")


async def serve(browser, gateway, log):
    server = HealthServer(0, MACHINE, browser, gateway, log)
    await server.start()
    return server


async def fetch(server, path):
    async with httpx.AsyncClient() as client:
        return await client.get(f"http://127.0.0.1:{server.port}{path}")


async def test_healthz_reflects_the_browser(log):
    server = await serve(FakeBrowser(True), FakeGateway(False), log)
    try:
        response = await fetch(server, "/healthz")
        assert response.status_code == 200
        body = json.loads(response.text)
        assert body["status"] == "ok"
        assert body["browser"] == "up"
        assert body["machineId"] == MACHINE
    finally:
        await server.stop()


async def test_healthz_503_when_browser_is_down(log):
    server = await serve(FakeBrowser(False), FakeGateway(True), log)
    try:
        response = await fetch(server, "/healthz")
        assert response.status_code == 503
        assert json.loads(response.text)["browser"] == "down"
    finally:
        await server.stop()


async def test_root_is_an_alias(log):
    server = await serve(FakeBrowser(True), FakeGateway(True), log)
    try:
        assert (await fetch(server, "/")).status_code == 200
    finally:
        await server.stop()


async def test_readyz_needs_registration_too(log):
    server = await serve(FakeBrowser(True), FakeGateway(False), log)
    try:
        response = await fetch(server, "/readyz")
        assert response.status_code == 503
        assert json.loads(response.text)["gateway"] == "disconnected"
    finally:
        await server.stop()


async def test_readyz_ok_when_both_are_up(log):
    server = await serve(FakeBrowser(True), FakeGateway(True), log)
    try:
        body = json.loads((await fetch(server, "/readyz")).text)
        assert body["gateway"] == "connected"
        assert body["jobsServed"] == 7
        assert body["uptimeSec"] >= 0
    finally:
        await server.stop()


async def test_unknown_path_is_404(log):
    server = await serve(FakeBrowser(True), FakeGateway(True), log)
    try:
        response = await fetch(server, "/nope")
        assert response.status_code == 404
    finally:
        await server.stop()


async def test_body_shape_is_stable(log):
    server = await serve(FakeBrowser(True), FakeGateway(True), log)
    try:
        body = json.loads((await fetch(server, "/healthz")).text)
        assert set(body) == {
            "status", "machineId", "browser", "gateway", "jobsServed", "uptimeSec"
        }
    finally:
        await server.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_health.py -v
```

Expected: `ModuleNotFoundError: No module named 'meerkly_headless.health'`.

- [ ] **Step 3: Implement `meerkly_headless/health.py`**

```python
"""Liveness and readiness endpoints for container probes.

Hand-rolled on asyncio.start_server: three routes do not justify a web
framework in an image that already carries a browser.
"""

from __future__ import annotations

import asyncio
import json
import time

REASON = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}


class HealthServer:
    def __init__(self, port: int, machine_id: str, browser, gateway, logger) -> None:
        self._requested_port = port
        self._machine_id = machine_id
        self._browser = browser
        self._gateway = gateway
        self._logger = logger
        self._started_at = time.monotonic()
        self._server: asyncio.AbstractServer | None = None
        self.port = port

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, "0.0.0.0", self._requested_port
            )
        except OSError as err:
            # A busy port must never take the worker down.
            self._logger.error("Health server failed to bind", error=str(err))
            return
        self.port = self._server.sockets[0].getsockname()[1]
        self._logger.info("Health server listening", port=self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            path = line.decode("latin-1").split(" ")[1].split("?")[0] if line else "/"

            if path in ("/healthz", "/"):
                ok = self._browser.is_ready()
            elif path == "/readyz":
                ok = self._browser.is_ready() and self._gateway.is_registered()
            else:
                await self._respond(writer, 404, "Not found", "text/plain")
                return

            await self._respond(writer, 200 if ok else 503, json.dumps(self._body(ok)))
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _body(self, ok: bool) -> dict:
        return {
            "status": "ok" if ok else "unavailable",
            "machineId": self._machine_id,
            "browser": "up" if self._browser.is_ready() else "down",
            "gateway": "connected" if self._gateway.is_registered() else "disconnected",
            "jobsServed": getattr(self._gateway, "jobs_served", 0),
            "uptimeSec": int(time.monotonic() - self._started_at),
        }

    async def _respond(self, writer, status: int, body: str, content_type="application/json"):
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {REASON[status]}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()
```

- [ ] **Step 4: Implement `meerkly_headless/cli.py`**

```python
"""Entry point: `meerkly-headless run`."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from . import __version__
from .browser import BrowserManager
from .config import load_config
from .gateway import GatewayClient
from .health import HealthServer
from .identity import DeviceTokenStore, obtain_device_token, resolve_machine_id
from .log import get_logger

SHUTDOWN_TIMEOUT_SEC = 10.0

UNPAIRED_MESSAGE = (
    "This worker is not paired. Set MEERKLY_API_KEY to a worker key from your "
    "Meerkly dashboard (/devices) and start it again."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meerkly-headless", description="Meerkly server worker")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", choices=["run"], help="command to run")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage()
        return 2
    return asyncio.run(run())


async def run() -> int:
    cfg = load_config()
    logger = get_logger(cfg.log_level)

    if os.path.exists("/.dockerenv") and os.environ.get("INVISIBLE_CORE_AUTOFIX") != "off":
        # The library's import-time pin check can shell out to pip, which in an
        # immutable image means a mutated site-packages or a 5-minute stall.
        logger.warn("INVISIBLE_CORE_AUTOFIX is not 'off'; importing the engine may run pip")

    try:
        machine_id = resolve_machine_id(cfg, logger)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 1

    logger.info("Starting meerkly-headless", machineId=machine_id, version=__version__)

    store = DeviceTokenStore(cfg.home, machine_id, logger)
    token = await obtain_device_token(cfg, machine_id, store, logger)
    if not token:
        print(UNPAIRED_MESSAGE, file=sys.stderr)
        return 1

    browser = BrowserManager(cfg, machine_id, logger)

    async def read_token():
        return store.read()

    gateway = GatewayClient(cfg, machine_id, read_token, browser, logger)

    health = None
    if cfg.health_port > 0:
        # Before the browser, so a probe sees an honest 503 rather than
        # connection-refused.
        health = HealthServer(cfg.health_port, machine_id, browser, gateway, logger)
        await health.start()

    stopping = asyncio.Event()

    def request_stop() -> None:
        if not stopping.is_set():
            logger.info("Shutting down")
            stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    try:
        await browser.ensure_browser()
    except Exception as err:
        logger.error("Could not start the browser", error=str(err))
        if health:
            await health.stop()
        return 1

    await gateway.start()
    await stopping.wait()

    try:
        # A wedged browser must not hang shutdown.
        await asyncio.wait_for(_shutdown(gateway, health, browser), SHUTDOWN_TIMEOUT_SEC)
    except TimeoutError:
        logger.warn("Shutdown timed out; exiting anyway")
    return 0


async def _shutdown(gateway, health, browser) -> None:
    await gateway.stop()
    if health:
        await health.stop()
    await browser.close()
```

- [ ] **Step 5: Run tests and check the console script**

```bash
python3 -m pytest tests/test_health.py -v
python3 -m pip install -e '.[dev]' >/dev/null && meerkly-headless --version
MEERKLY_HOME=/tmp/mw-probe meerkly-headless run; echo "exit=$?"
```

Expected: 7 passed; the version prints; the unpaired run prints the pairing message and `exit=1`.

- [ ] **Step 6: Verify lint and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -q
git add meerkly_headless/health.py meerkly_headless/cli.py tests/test_health.py
git commit -m "feat: health endpoints and CLI entry point"
```

---

### Task 8: Docker and compose

**Files:**
- Create: `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`, `docker-compose.yml`

No seccomp profile is needed: that existed for Chromium's user-namespace sandbox, and this is Firefox.

- [ ] **Step 1: Create `.dockerignore`**

```
.git
.venv
__pycache__
*.egg-info
.pytest_cache
.ruff_cache
docs
tests
.env
```

- [ ] **Step 2: Create `docker-entrypoint.sh`**

```bash
#!/bin/sh
set -e

DISPLAY_NUM=${DISPLAY_NUM:-99}
SCREEN_GEOMETRY=${SCREEN_GEOMETRY:-1920x1080x24}

# -nolisten tcp keeps the display off the network; the browser uses the unix
# socket. Deliberately NOT xvfb-run: that wrapper does not forward signals, so
# SIGTERM would kill an in-flight crawl instead of draining it.
Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
XVFB_PID=$!
export DISPLAY=":${DISPLAY_NUM}"

i=0
while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; do
  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "Xvfb exited before the display was ready" >&2
    exit 1
  fi
  i=$((i + 1))
  if [ "$i" -ge 100 ]; then
    echo "Timed out waiting for Xvfb display :${DISPLAY_NUM}" >&2
    exit 1
  fi
  sleep 0.1
done

exec "$@"
```

- [ ] **Step 3: Create the `Dockerfile`**

```dockerfile
FROM python:3.12-slim-bookworm AS build

WORKDIR /app
COPY pyproject.toml README.md ./
COPY meerkly_headless ./meerkly_headless
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INVISIBLE_PLAYWRIGHT_CACHE_DIR=/opt/engine \
    MEERKLY_HOME=/data \
    HEALTH_PORT=9090 \
    HEADLESS=false \
    INVISIBLE_CORE_AUTOFIX=off

# tini reaps the browser's children and forwards SIGTERM — Python as PID 1
# does neither. The rest are the patched Firefox's runtime libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini ca-certificates xvfb libgtk-3-0 libdbus-glib-1-2 libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY --from=build /install /usr/local

# Fetch the engine at build time (~238MB download, ~544MB unpacked,
# sha256-verified), then make it readable by the unprivileged runtime user.
RUN python -m invisible_playwright fetch && chmod -R a+rX /opt/engine

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Crawled pages are untrusted — never run the browser as root.
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /data && chown -R worker:worker /data
USER worker

VOLUME ["/data"]
EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh", "meerkly-headless"]
CMD ["run"]
```

`HEADLESS=false` is deliberate: a truly headless browser is detectable in ways no flag fixes, so the container brings its own display.

- [ ] **Step 4: Create `docker-compose.yml` — one service, nothing clever**

```yaml
services:
  worker:
    build: .
    # Without this, compose tries Docker Hub first and every `up` starts with
    # a failed pull.
    pull_policy: build
    restart: unless-stopped
    environment:
      # Empty default (not :?) so `down`, `logs`, and `ps` work without a key.
      MEERKLY_API_KEY: ${MEERKLY_API_KEY:-}
      MEERKLY_WORKER_ID: ${MEERKLY_WORKER_ID:-worker-1}
      LOG_LEVEL: ${LOG_LEVEL:-info}
      APP_ENV: ${APP_ENV:-}
      GATEWAY_URL: ${GATEWAY_URL:-}
      ACCOUNT_BASE_URL: ${ACCOUNT_BASE_URL:-}
      ALLOW_INSECURE_GATEWAY: ${ALLOW_INSECURE_GATEWAY:-}
    volumes:
      - worker-data:/data
    # Needed on Linux so the container can reach a gateway on the host.
    extra_hosts:
      - "host.docker.internal:host-gateway"
    shm_size: 2gb
    # An in-flight crawl has a 30s budget; Docker's default 10s SIGKILL would
    # cut it off mid-job.
    stop_grace_period: 45s
    healthcheck:
      # /readyz here (is it actually serving?) vs /healthz in the image (is it
      # alive?).
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/readyz', timeout=4).status==200 else 1)"]
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
  worker-data:
```

Empty-string defaults are safe: `config.py` treats a blank environment variable as unset, so the built-in defaults still apply.

- [ ] **Step 5: Build the image**

```bash
docker build -t meerkly-headless:dev .
```

Expected: a successful build. If apt fails on `libasound2`, the base has moved to the `t64` naming — use `libasound2t64` and record it in `CLAUDE.md`.

- [ ] **Step 6: Verify the image internals**

```bash
docker run --rm meerkly-headless:dev --version
docker run --rm --entrypoint sh meerkly-headless:dev -c 'id && ls /opt/engine && echo "autofix=$INVISIBLE_CORE_AUTOFIX"'
docker run --rm meerkly-headless:dev run; echo "exit=$?"
```

Expected: the version prints; `id` shows `uid=10001(worker)` — **not root**; `/opt/engine` holds a firefox directory; `autofix=off`; and the unpaired run prints the pairing message with `exit=1` rather than crashing or hanging.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile docker-entrypoint.sh .dockerignore docker-compose.yml
git commit -m "feat: docker image with Xvfb and single-service compose"
```

---

### Task 9: End-to-end verification and documentation

**Files:**
- Create: `README.md`, `CLAUDE.md`, `docs/verification.md`

No product code. This proves the worker actually conforms in practice and settles the one thing the unit tests cannot: how Firefox behaves after a failed navigation.

- [ ] **Step 1: Bring up the gateway and account app, and get a worker key**

```bash
cd ../api-gateway && make run
```

Expected: the gateway listening on `:8080`. Start the account app the same way you would for `meerkly-headless`, and create a worker key (`mk_wk_...`) in its dashboard.

- [ ] **Step 2: Run the worker against them**

```bash
APP_ENV=development MEERKLY_API_KEY=mk_wk_yourkey HEADLESS=true meerkly-headless run
```

Expected log lines, in order: `Starting meerkly-headless`, `Enrolled with the account service`, `Browser ready`, `Sent registration`, `Registered with gateway`.

- [ ] **Step 3: Confirm the gateway sees it and serve a crawl**

```bash
curl -s localhost:8080/v1/devices | python3 -m json.tool

curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}' | python3 -c "
import json,sys
d = json.load(sys.stdin)
for key in ('final_url','title','http_status','loaded_ms','wait_rule'):
    print(f'{key:12}', d[key])
print('html_bytes  ', len(d['html']))
assert d['http_status'] == 200
assert 'Example Domain' in d['html']
print('OK')
"
```

Expected: one device with `"platform": "server"` and an `engineVersion` starting `Invisible Playwright`; then `http_status 200` and `OK`.

- [ ] **Step 4: Verify every wait mode**

```bash
for mode in stable domcontentloaded networkidle; do
  printf '%-18s' "$mode"
  curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
    -d "{\"url\":\"https://example.com\",\"wait_for\":\"$mode\"}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('status', d['http_status'], 'timed_out', d['wait_timed_out'])"
done
```

Expected: all three report `status 200 timed_out False`.

- [ ] **Step 5: Verify selector waits, hit and miss**

```bash
curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
  -d '{"url":"https://example.com","wait_for":"h1"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['wait_timed_out'] is False; print('hit  OK')"

curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
  -d '{"url":"https://example.com","wait_for":"#never-exists","settle_ms":2000}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['wait_timed_out'] is True; assert d['html']; print('miss OK — timed out but still returned HTML')"
```

Expected: both print `OK`. A selector timeout is best-effort capture, not a failure.

- [ ] **Step 6: Verify conditional wait rules — order and fallback**

```bash
curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' -d '{
  "url": "https://example.com",
  "wait_rules": [{"if": "#not-present", "then": "networkidle"}, {"if": "h1", "then": "stable"}],
  "detect_ms": 1000
}' | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['wait_rule'] == 'h1'; print('first-match-by-order OK')"

curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
  -d '{"url":"https://example.com","wait_rules":[{"if":"#nope","then":"networkidle"}],"detect_ms":500}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['wait_rule'] == 'none'; assert d['wait_timed_out'] is False; print('no-match fallback OK')"
```

Expected: both print `OK`. The first proves the **second** rule matched (order, not definition), and the second proves no match is a fallback rather than a timeout.

- [ ] **Step 7: Verify the SSRF guard end to end**

```bash
for target in http://localhost:8080/ http://169.254.169.254/ http://2130706433/; do
  printf '%-28s' "$target"
  curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
    -d "{\"url\":\"$target\"}" | grep -q "Private, loopback" && echo "blocked" || echo "*** LEAKED ***"
done
```

Expected: three `blocked` lines. The third is the alternate-notation case — a leak there means `as_ipv4` is not on the job path.

- [ ] **Step 8: Verify the http_status capture that decides credits**

```bash
for code in 404 503; do
  printf '%s -> ' "$code"
  curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
    -d "{\"url\":\"https://httpbin.org/status/$code\"}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['http_status'])"
done
```

Expected: `404 -> 404` and `503 -> 503`. A page that loads with an error status is still a successful crawl; only the status decides whether it earns.

- [ ] **Step 9: THE POISONING CHECK — the regression most likely to appear here**

This is the empirical test of `_drain_error_page`. Chromium commits its error page *after* `goto` rejects, and left in flight that commit interrupts the next job. Firefox's behavior is assumed, not measured — this settles it.

```bash
echo "--- job 1: unreachable"
curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
  -d '{"url":"https://this-host-does-not-exist-9f3a.invalid/"}' | head -c 300; echo
for n in 2 3; do
  printf 'job %s: ' "$n"
  curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' \
    -d '{"url":"https://example.com"}' \
    | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['http_status']==200, 'POISONED'; print('200 OK')"
done
echo "POISONING CHECK PASSED"
```

Expected: job 1 fails with a `Failed to load: ...` error, jobs 2 and 3 both return 200.

**If job 2 or 3 fails**, find Firefox's real error-page URL and fix the regex in `_drain_error_page`:

```bash
python3 -c "
import asyncio
from invisible_playwright.async_api import InvisiblePlaywright

async def main():
    async with InvisiblePlaywright(headless=True) as browser:
        page = await browser.new_page()
        try:
            await page.goto('https://this-host-does-not-exist-9f3a.invalid/', timeout=8000)
        except Exception as err:
            print('goto raised:', str(err).splitlines()[0])
        await asyncio.sleep(1)
        print('page.url after failure:', page.url)

asyncio.run(main())
"
```

Record the real value in `CLAUDE.md`.

- [ ] **Step 10: Verify graceful shutdown and the compose healthcheck**

```bash
docker compose up -d --build
sleep 100
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
curl -s -X POST localhost:8080/v1/html -H 'content-type: application/json' -d '{"url":"https://example.com"}' >/dev/null &
sleep 1
time docker compose stop
docker compose logs worker | tail -5
```

Expected: `worker` reaches `(healthy)`; `stop` returns well inside the 45s grace period; the logs end with `Shutting down` rather than a kill. If it stays `starting`, read the logs — that almost always means enrollment or registration failed, not a broken probe.

- [ ] **Step 11: Write `docs/verification.md`**

Record what you actually observed: the commands above, Firefox's real error-page URL, the observed `engineVersion`, and anything that differed from this plan. This is what the next person runs before trusting a dependency bump.

- [ ] **Step 12: Write `README.md`**

Cover: what the worker is (a Meerkly worker that crawls with stealth-patched Firefox and speaks the `api-gateway` protocol); quick start with Docker Compose (`.env` with `MEERKLY_API_KEY`, then `docker compose up -d`); quick start from source (`pip install -e .`, `python -m invisible_playwright fetch`, `meerkly-headless run`); the full environment-variable table from `config.py`; the note that `$MEERKLY_HOME` holds plaintext 0600 secrets and why; and troubleshooting for the unpaired message, the `libasound2` naming, and the container loopback hint.

- [ ] **Step 13: Write `CLAUDE.md`**

Keep it short and load-bearing. It must cover:

- The module table from this plan's File Structure section.
- **`api-gateway/spec/` is the contract**, and `tests/test_fetch_spec.py` plus `tests/test_gateway.py` are what enforce it. A protocol change means updating the spec, the vectors, and every worker together.
- **Things not to "improve":** the `SETTLE_STABLE` observer's exclusion of `attributes`; the throwing-selector asymmetry between the two selector snippets; `exec_js`'s deadline and why main-world evaluation makes it load-bearing; `humanize=False`; `headless=False` plus our own Xvfb rather than the library's `-ac -listen tcp` one; no custom UA, headers, or viewport, ever; `as_ipv4` and why a naive `urlsplit` port would silently weaken the SSRF guard; the error-page URL confirmed in Step 9.
- **Identity:** the resolution order, and that rotating the worker key changes derived machine IDs — so identity that must survive rotation needs a volume or an explicit `MEERKLY_MACHINE_ID`.
- **The `invisible-playwright` pin** and that a bump means re-running `docs/verification.md`.

- [ ] **Step 14: Add the repo to the root `../CLAUDE.md`**

Under "Repository layout", after the `meerkly-headless/` entry:

```markdown
- `meerkly-headless/` — a Python worker for headless servers, using
  [invisible_playwright](https://github.com/feder-cr/invisible_playwright) (stealth-patched
  **Firefox**) as the engine. Speaks the same gateway protocol and the same wait semantics as the
  other workers (spec-enforced), pairs by worker key, and registers as `platform: "server"`.
  Independent of `meerkly-headless` — not a port of it.
```

Add `meerkly-headless/CLAUDE.md` to the "deeper docs" list as well.

- [ ] **Step 15: Final verification and commit**

```bash
ruff check . && ruff format --check . && python3 -m pytest -v
git add README.md CLAUDE.md docs/
git commit -m "docs: README, CLAUDE.md, and verification runbook"
```

Expected: the whole suite passes, including both conformance suites against `../api-gateway/spec`.

Note: `../CLAUDE.md` lives in the parent directory, which is not a git repository. Edit it in place and mention the change rather than trying to commit it here.

---

## Plan Self-Review

**Coverage against the request.** Docker with a single-worker compose (Task 8), API key via environment variable (Tasks 1 and 4), connects to the gateway and serves requests (Tasks 5–7), follows the wait/selector spec (Task 3 for parsing, Task 5 for behavior, Task 9 for empirical proof), and conforms to the gateway API — the two conformance suites in Tasks 3 and 6 validate directly against `api-gateway/spec` and fail loudly if the sibling checkout is missing.

**What "simple" bought us.** Nine tasks and eleven modules, down from nineteen tasks and sixteen. Dropped entirely: OAuth sign-in, `config.json`, log-file rotation and the ring buffer, `status`/`diagnostics` commands, the seccomp profile, and a separate transport-guard module. The parts kept from `meerkly-headless` are the ones that encode behavior the spec depends on (the wait snippets, the 30s budget, the settle algorithm) or that protect the worker (the SSRF guard, enrollment's retry-versus-terminal split).

**One thing this plan adds that no sibling has.** `as_ipv4()` exists because JavaScript's `URL` normalizes `2130706433` and `0x7f000001` to `127.0.0.1` natively and Python's `urlsplit` does not. Without it, this worker's SSRF guard would be quietly weaker than the other workers'. Task 2's Step 5 and Task 9's Step 7 both check it.

**Type consistency.** `parse_fetch_frame` returns camelCase keys (`job["waitFor"]`, `job["settleMs"]`, `job["waitRules"]`, `job["detectMs"]`), consumed unchanged in `browser._fetch` and `gateway._handle_fetch`. `navigate_and_extract` returns the result-frame shape consumed unchanged by `build_result`. `BrowserManager.is_ready()` and `GatewayClient.is_registered()` are exactly the names `HealthServer` calls and the fakes in `tests/test_health.py` implement. `identity.HOST_LOCAL` is defined in `identity.py` and imported by `gateway.py` — one definition, two callers.
