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
            self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._requested_port)
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
