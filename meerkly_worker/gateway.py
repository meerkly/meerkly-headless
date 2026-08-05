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
