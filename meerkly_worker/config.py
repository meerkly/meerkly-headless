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
        home=Path(_env("MEERKLY_HOME") or "~/.meerkly-worker").expanduser(),
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
