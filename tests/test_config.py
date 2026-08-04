import pytest

from meerkly_worker.config import load_config

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


@pytest.mark.parametrize(
    "value,expected", [("0", 0), ("8080", 8080), ("nope", 9090), ("70000", 9090)]
)
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
