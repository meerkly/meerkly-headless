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
