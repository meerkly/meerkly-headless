import json

import httpx
import pytest

from meerkly_worker.health import HealthServer
from meerkly_worker.log import get_logger

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
        assert set(body) == {"status", "machineId", "browser", "gateway", "jobsServed", "uptimeSec"}
    finally:
        await server.stop()
