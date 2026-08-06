"""Entry point: `meerkly-headless run`."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

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


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


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

    if _running_in_container() and os.environ.get("INVISIBLE_CORE_AUTOFIX") != "off":
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
