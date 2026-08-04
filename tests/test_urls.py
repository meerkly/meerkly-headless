import pytest

from meerkly_worker import urls as urls_module
from meerkly_worker.urls import as_ipv4, check_url, is_private_ip, resolves_to_private


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


@pytest.mark.parametrize("host", ["-1", "-2164260863", "+1", "+2130706433", "1.2.3.-4"])
def test_as_ipv4_rejects_signed_parts(host):
    """int(part, base) accepts a leading +/-, but the WHATWG parser only
    accepts ASCII digits (plus the 0x/0 radix prefixes) — a signed part must
    fall through to "this is a domain, not an address", same as letters do."""
    assert as_ipv4(host) is None


PRIVATE = [
    "localhost",
    "localhost.",
    "foo.localhost",
    "foo.localhost.",
    "127.0.0.1",
    "127.0.0.1.",
    "127.53.1.9",
    "0.0.0.0",
    "10.1.2.3",
    "100.64.0.1",
    "169.254.169.254",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "[::1]",
    "[::]",
    "[fe80::1]",
    "[fc00::1]",
    "[fd12::1]",
    "[::ffff:127.0.0.1]",
    "[::ffff:7f00:1]",
    "2130706433",
    "0x7f000001",
    "127.1",
    "0177.0.0.1",
]

PUBLIC = [
    "example.com",
    "8.8.8.8",
    "1.1.1.1",
    "172.15.0.1",
    "172.32.0.1",
    "192.169.1.1",
    "100.63.255.255",
    "169.253.0.1",
    "11.0.0.1",
    "[2606:4700::1111]",
    "notlocalhost",
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


def test_trailing_dot_on_public_host_is_not_over_blocked():
    """The localhost-guard fix strips a trailing FQDN dot before comparing —
    make sure that normalization doesn't start blocking public hosts too."""
    assert check_url("https://example.com./", block_private=True).valid


@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", True),
        ("10.0.0.1", True),
        ("169.254.169.254", True),
        ("8.8.8.8", False),
        ("::1", True),
        ("::", True),
        ("fe80::1", True),
        ("fec0::1", False),
        ("fc00::1", True),
        ("fd00::1", True),
        ("2606:4700::1111", False),
        ("::ffff:127.0.0.1", True),
        ("::ffff:7f00:1", True),
        ("::ffff:8.8.8.8", False),
        ("not-an-ip", False),
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
