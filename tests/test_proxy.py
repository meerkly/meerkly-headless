import re

import pytest

from meerkly_headless.proxy import build_proxy_options, generate_sid


def test_build_proxy_options_splits_credentials_out_of_server():
    opts = build_proxy_options("http://user:pass@proxy.example:1337", "000000")
    assert opts == {
        "server": "http://proxy.example:1337",
        "username": "user",
        "password": "pass",
    }
    assert "@" not in opts["server"]
    assert "user" not in opts["server"]
    assert "pass" not in opts["server"]


def test_sid_placeholder_replaced_in_username_and_password():
    opts = build_proxy_options("http://u-sid-<sid>-ttl-60:p-<sid>@h:1337", "123456")
    assert opts["username"] == "u-sid-123456-ttl-60"
    assert opts["password"] == "p-123456"


def test_credentials_are_percent_decoded():
    opts = build_proxy_options("http://u%40ser:p%3Ass@h:1337", "000000")
    assert opts["username"] == "u@ser"
    assert opts["password"] == "p:ss"


def test_no_credentials_yields_server_only():
    opts = build_proxy_options("http://proxy.example:1337", "000000")
    assert opts == {"server": "http://proxy.example:1337"}


def test_ipv6_host_keeps_brackets():
    opts = build_proxy_options("http://[::1]:1337", "000000")
    assert opts["server"] == "http://[::1]:1337"


def test_socks5_scheme_is_accepted():
    opts = build_proxy_options("socks5://user:pass@h:1080", "000000")
    assert opts["server"] == "socks5://h:1080"


@pytest.mark.parametrize(
    "url",
    [
        "http://proxy.example",  # missing port
        "http://:1337",  # missing host
        "ftp://proxy.example:1337",  # unsupported scheme
        "proxy.example:1337",  # no scheme
        "http://proxy.example:notaport",  # non-numeric port
        "http://proxy.example:1337/path",  # trailing path
        "http://proxy.example:1337?q=1",  # query
    ],
)
def test_malformed_urls_raise(url):
    with pytest.raises(ValueError):
        build_proxy_options(url, "000000")


def test_error_never_leaks_credentials():
    with pytest.raises(ValueError) as excinfo:
        build_proxy_options("ftp://secretuser:secretpass@h:1337", "000000")
    message = str(excinfo.value)
    assert "secretuser" not in message
    assert "secretpass" not in message


def test_generate_sid_is_six_digits():
    for _ in range(20):
        assert re.fullmatch(r"\d{6}", generate_sid())
    assert len({generate_sid() for _ in range(50)}) > 1
