import pytest

from meerkly_worker import urls as urls_module
from meerkly_worker.urls import (
    _normalize_host,
    as_ipv4,
    check_url,
    is_private_ip,
    resolves_to_private,
)

# --- Scheme / shape validation -----------------------------------------------
#
# check_url's basic contract: reject non-strings/empties, prepend https://
# when no scheme is present, allow only http/https, and require a hostname.


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


# --- Fix round 4: port validation --------------------------------------------
#
# check_url guarded urlsplit() construction but never touched parts.port. A
# malformed authority like "%5B::1%5D" makes urlsplit mis-split on the first
# literal ':' and hand back a corrupted hostname fragment while parts.port
# would raise ValueError -- so the blocking decision was being made on data
# that doesn't correspond to what would actually be dialed.


def test_malformed_authority_with_bad_port_is_rejected():
    result = check_url("https://%5B::1%5D/", block_private=True)
    assert not result.valid
    assert "Invalid URL" in result.error


def test_valid_port_is_still_accepted():
    result = check_url("https://example.com:8443/p", block_private=True)
    assert result.valid
    assert result.url == "https://example.com:8443/p"


# --- as_ipv4 notations --------------------------------------------------------
#
# urlsplit leaves alternate IPv4 spellings (decimal, hex, octal, short forms)
# as opaque strings; as_ipv4 parses them the way the WHATWG URL spec (and a
# real browser) would, so the private-host guard can't be bypassed by spelling.


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


@pytest.mark.parametrize("host", ["0x-1", "0-1", "0x+1", "08"])
def test_as_ipv4_rejects_sign_after_radix_prefix_and_bad_octal_digits(host):
    """A sign placed *after* a 0x/0 radix prefix still parses under
    int(part, base) (e.g. int("-1", 16) == -1), so the position-0 check alone
    missed it. Explicit character-set validation catches it; "08" exercises
    the same validation for a bad octal digit (8 is not 0-7)."""
    assert as_ipv4(host) is None


# --- Private / public classification -----------------------------------------
#
# check_url(block_private=True) end-to-end over a broad set of private and
# public hosts, plus is_private_ip's own IPv4/IPv6 classification rules.

PRIVATE = [
    "localhost",
    "localhost.",
    "localhost..",
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
    "[::127.0.0.1]",
    "[::169.254.169.254]",
    "[0:0:0:0:0:0:0:1]",
    "[0:0:0:0:0:0:0:0]",
    "[0:0:0:0:0:ffff:127.0.0.1]",
    "[fe80:0:0:0:0:0:0:1]",
    "[fc00:0:0:0:0:0:0:1]",
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
        # Expanded/non-canonical spellings: the old regex-based branch only
        # recognized one spelling of each address (e.g. "::1", "fe80..."),
        # so these bypassed it entirely. IPv6Address normalizes all of them.
        ("0:0:0:0:0:0:0:1", True),
        ("0:0:0:0:0:0:0:0", True),
        ("0:0:0:0:0:ffff:127.0.0.1", True),
        ("fe80:0:0:0:0:0:0:1", True),
        ("fc00:0:0:0:0:0:0:1", True),
        # Expanded *public* address must not be over-blocked by the fix.
        ("2606:4700:0:0:0:0:0:1111", False),
        # ::/96 "IPv4-compatible" form: low 32 bits are a plain IPv4 address.
        ("::127.0.0.1", True),
        ("::7f00:1", True),  # same address, canonical hex-group spelling
        ("::169.254.169.254", True),
        ("::8.8.8.8", False),
        # :: and ::1 are technically inside ::/96 too, but must keep their
        # existing is_unspecified/is_loopback classification (both True),
        # not fall through to "embedded IPv4 0.0.0.0 / 0.0.0.1".
        ("::", True),
        ("::1", True),
    ],
)
def test_is_private_ip(address, expected):
    assert is_private_ip(address) is expected


# --- Host normalization (Fix round 3: normalization bypasses) ----------------
#
# check_url classifies urlsplit(...).hostname directly, but a browser's host
# parser percent-decodes and Unicode-folds (UTS46) *before* resolving. Every
# gap between the raw string we classify and the string the browser actually
# dials is a bypass. These tests reproduce each confirmed bypass class and
# then guard against the fix over-blocking or leaking into the fetch URL.


@pytest.mark.parametrize(
    "host",
    [
        "127%2E0%2E0%2E1",  # percent-decodes to 127.0.0.1
        "%6C%6F%63%61%6C%68%6F%73%74",  # percent-decodes to "localhost"
        "%6c%6f%63%61%6c%68%6f%73%74.",  # same, mixed case + trailing dot
        "%256C%256F%2563%2561%256C%2568%256F%2573%2574",  # double-encoded "localhost"
        "%5B%3A%3A1%5D",  # fully percent-encoded "[::1]" (colons encoded too,
        # so urlsplit's authority parser never sees a literal ':' or '[' and
        # hands back the whole blob as an opaque "hostname")
    ],
)
def test_percent_encoded_private_hosts_are_blocked(host):
    result = check_url(f"https://{host}/", block_private=True)
    assert not result.valid, host
    assert result.error == "Private, loopback, and link-local addresses are not allowed"


@pytest.mark.parametrize(
    "host",
    [
        "ｌｏｃａｌｈｏｓｔ",  # fullwidth Latin, NFKC-folds to "localhost"
        "１２７．０．０．１",  # fullwidth digits + fullwidth full stop, NFKC-folds to "127.0.0.1"
    ],
)
def test_unicode_folded_private_hosts_are_blocked(host):
    result = check_url(f"https://{host}/", block_private=True)
    assert not result.valid, host
    assert result.error == "Private, loopback, and link-local addresses are not allowed"


def test_percent_encoded_public_host_is_still_allowed():
    """Normalization is a blocklist widening, not a general rewrite -- a
    percent-encoded *public* host must still pass block_private=True."""
    result = check_url("https://ex%61mple.com/p", block_private=True)
    assert result.valid
    assert result.error is None


def test_percent_encoded_public_host_url_is_returned_unmodified():
    """check_url must return the original href, not the normalized-for-blocking
    host -- normalization is for the blocking decision only and must never
    leak into the URL that actually gets fetched."""
    result = check_url("https://ex%61mple.com/p", block_private=True)
    assert result.url == "https://ex%61mple.com/p"
    assert "%61" in result.url


@pytest.mark.parametrize(
    "raw,expected",
    [
        # One round: plain percent-encoding decodes fully on the first pass.
        ("127%2E0%2E0%2E1", "127.0.0.1"),
        # Two rounds: "%25" is itself the encoding for "%", so this only
        # becomes "localhost" after decoding twice.
        ("%256C%256F%2563%2561%256C%2568%256F%2573%2574", "localhost"),
        # NFKC + casefold + trailing-dot stripping, no percent-decoding involved.
        ("LOCALHOST..", "localhost"),
        # A fully percent-encoded IPv6 literal: brackets only appear after
        # decoding, and must be stripped so callers see the bracket-free form
        # urlsplit normally hands them.
        ("%5B%3A%3A1%5D", "::1"),
    ],
)
def test_normalize_host_decodes_and_folds(raw, expected):
    assert _normalize_host(raw) == expected


def test_normalize_host_decode_round_trip_is_capped():
    """A pathological run of "%25"s could in principle re-decode forever
    (each round just peels off one layer of escaped '%'); the 3-round cap
    must keep this fast rather than hanging or scanning unboundedly."""
    pathological = "%25" * 5000 + "41"
    result = _normalize_host(pathological)
    assert isinstance(result, str)
    assert len(result) > 0


# --- DNS pre-check (resolves_to_private) -------------------------------------
#
# check_url deliberately doesn't resolve hostnames; resolves_to_private is the
# best-effort DNS pre-check layered on top, used for gateway-dispatched jobs.


async def test_literals_skip_dns(monkeypatch):
    monkeypatch.setattr(urls_module, "_resolve", lambda h: pytest.fail("must not resolve"))
    assert await resolves_to_private("https://8.8.8.8/") is False


async def test_invalid_radix_literal_does_not_skip_dns(monkeypatch):
    """Regression: as_ipv4("0x-7f000001") must return None (not a recognized
    literal), so resolves_to_private has to fall through to DNS instead of
    treating the host as already-screened and skipping the pre-check."""
    calls = []

    def fake_resolve(host):
        calls.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(urls_module, "_resolve", fake_resolve)
    assert await resolves_to_private("https://0x-7f000001/") is False
    assert calls == ["0x-7f000001"]


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


# --- Fix round 4: resolves_to_private never normalized the host -------------
#
# resolves_to_private extracted urlsplit(url).hostname and both classified
# and resolved it *raw*. A percent-encoded or Unicode-folded hostname still
# contains '%' escapes (or non-ASCII), so socket.getaddrinfo can't resolve it,
# raises, and the broad except swallowed that as "unresolvable, proceed" --
# functionally identical to skipping the DNS check for exactly the inputs
# round 3 was built to handle. check_url deliberately *allows* a
# percent-encoded public-looking domain through, so resolves_to_private is the
# only remaining check that the name doesn't resolve somewhere private.


async def test_percent_encoded_host_resolving_to_loopback_is_flagged(monkeypatch):
    monkeypatch.setattr(
        urls_module,
        "_resolve",
        lambda h: ["127.0.0.1"] if h == "evil.example.com" else ["93.184.216.34"],
    )
    # "ev%69l.example.com" percent-decodes to "evil.example.com".
    assert await resolves_to_private("https://ev%69l.example.com/") is True


async def test_unicode_folded_host_resolving_to_loopback_is_flagged(monkeypatch):
    monkeypatch.setattr(
        urls_module,
        "_resolve",
        lambda h: ["127.0.0.1"] if h == "evil.example.com" else ["93.184.216.34"],
    )
    # Fullwidth Latin spelling of "evil.example.com", NFKC-folds to it.
    folded = "ｅｖｉｌ.example.com"
    assert await resolves_to_private(f"https://{folded}/") is True


async def test_resolve_is_called_with_normalized_host_not_raw(monkeypatch):
    calls = []

    def fake_resolve(host):
        calls.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(urls_module, "_resolve", fake_resolve)
    assert await resolves_to_private("https://ev%69l.example.com/") is False
    assert calls == ["evil.example.com"]


async def test_percent_encoded_literal_still_skips_dns(monkeypatch):
    """A percent-encoded IPv6/IPv4 literal is already screened by check_url,
    so after normalization it must still short-circuit before DNS -- the
    fix must not turn every already-screened literal into a DNS lookup."""
    monkeypatch.setattr(urls_module, "_resolve", lambda h: pytest.fail("must not resolve"))
    assert await resolves_to_private("https://%5B%3A%3A1%5D/") is False  # "[::1]"
    assert await resolves_to_private("https://127%2E0%2E0%2E1/") is False  # "127.0.0.1"
