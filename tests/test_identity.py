import json
import stat
import uuid

import httpx
import pytest

from meerkly_worker import identity
from meerkly_worker.config import Config
from meerkly_worker.identity import DeviceTokenStore, EnrollResult
from meerkly_worker.log import get_logger

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
        "deviceModel",
        "os",
        "arch",
        "appVersion",
        "engineVersion",
        "cpuCores",
        "memoryMb",
        "screen",
        "timezone",
        "locale",
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
        "machine_id",
        "platform",
        "name",
        "device_model",
        "os",
        "arch",
        "app_version",
        "engine_version",
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
