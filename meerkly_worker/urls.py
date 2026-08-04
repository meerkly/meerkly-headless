"""URL validation and the SSRF guard for gateway-dispatched jobs.

block_private=True is applied to every fetch job, because those URLs come from
a remote caller and must not be able to probe this worker's own network
(169.254.169.254 being the obvious target).

Hostnames are not resolved by check_url itself; resolves_to_private adds a
best-effort DNS pre-check on top. The browser resolves independently, so a
rebinding window remains by design — the real fix is network-level.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

FORBIDDEN_PREFIXES = ("file:", "chrome:", "chrome-extension:", "about:")
PRIVATE_HOST_ERROR = "Private, loopback, and link-local addresses are not allowed"

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


@dataclass(frozen=True)
class UrlCheck:
    valid: bool
    url: str | None
    error: str | None


def check_url(value, *, block_private: bool = False) -> UrlCheck:
    # Jobs arrive as untyped JSON, so `value` may not be a string at all.
    if not isinstance(value, str) or not value.strip():
        return UrlCheck(False, None, "URL cannot be empty")

    candidate = value.strip()

    # On the RAW string, before the https:// prepend — this is what catches
    # "about:blank", which has no "//" and would otherwise become
    # "https://about:blank".
    lowered = candidate.lower()
    for prefix in FORBIDDEN_PREFIXES:
        if lowered.startswith(prefix):
            return UrlCheck(False, None, f"Protocol {prefix} is not allowed")

    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"

    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        _port = parts.port  # raises ValueError on a malformed/out-of-range port
    except ValueError as err:
        return UrlCheck(False, None, f"Invalid URL: {err}")

    if parts.scheme not in ("http", "https"):
        return UrlCheck(False, None, f"Only http and https are allowed, got: {parts.scheme}:")
    if not host:
        return UrlCheck(False, None, "URL must have a hostname")
    if block_private and _is_private_host(host):
        return UrlCheck(False, None, PRIVATE_HOST_ERROR)

    # Note: href is built from the *original* parts, not the normalized host
    # used for the blocking decision above. Normalization folds percent-encoding
    # and Unicode compatibility forms so the guard can classify what a browser
    # would actually dial; it must never leak into the URL that gets fetched.
    href = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, parts.fragment))
    return UrlCheck(True, href, None)


def as_ipv4(host: str) -> str | None:
    """Parse a host as IPv4 the way the WHATWG URL spec does.

    urlsplit leaves "2130706433", "0x7f000001", "127.1" and "0177.0.0.1" as
    opaque strings, but a browser resolves every one to 127.0.0.1. Without this
    the private-host guard is trivially bypassable.
    """
    if not host:
        return None

    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts = parts[:-1]  # one trailing dot is allowed
    if len(parts) > 4:
        return None

    numbers: list[int] = []
    for part in parts:
        if not part:
            return None
        lowered = part.lower()
        # WHATWG: 0x/0X prefix -> hex digits only; a leading 0 with more
        # characters -> octal digits only; otherwise -> decimal digits only.
        # Validating the character set (rather than trusting int() to reject
        # junk) is what catches a sign slipped in after the radix prefix,
        # e.g. "0x-1" or "0-1", which int(part[2:], 16) would otherwise accept.
        if lowered.startswith("0x"):
            digits = lowered[2:]
            if not digits or any(c not in "0123456789abcdef" for c in digits):
                return None
            numbers.append(int(digits, 16))
        elif len(part) > 1 and part[0] == "0":
            digits = part[1:]
            if any(c not in "01234567" for c in digits):
                return None
            numbers.append(int(digits, 8))
        else:
            if any(c not in "0123456789" for c in part):
                return None  # contains a letter or sign: a domain, not an address
            numbers.append(int(part, 10))

    if any(number > 255 for number in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None

    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def is_private_ip(address: str) -> bool:
    ip = address.lower()

    if ":" in ip:
        # Parse rather than pattern-match: a string-prefix test only
        # recognizes one spelling of each address, so expanded/non-canonical
        # forms like "0:0:0:0:0:0:0:1" bypassed the old regexes. IPv6Address
        # normalizes every valid spelling before we classify it.
        try:
            parsed = ipaddress.IPv6Address(ip)
        except ValueError:
            return False
        if parsed.ipv4_mapped is not None:
            return is_private_ip(str(parsed.ipv4_mapped))
        # ::/96 is the deprecated "IPv4-compatible" form: the low 32 bits are
        # a plain IPv4 address (e.g. ::127.0.0.1 == ::7f00:1 == 127.0.0.1
        # embedded). :: and ::1 are technically inside ::/96 too (value 0 and
        # 1), but they're already correctly classified below via
        # is_unspecified/is_loopback, so leave those two alone here.
        if (
            parsed in ipaddress.IPv6Network("::/96")
            and parsed != ipaddress.IPv6Address("::")
            and parsed != ipaddress.IPv6Address("::1")
        ):
            embedded = ipaddress.IPv4Address(int(parsed))
            return is_private_ip(str(embedded))
        if parsed.is_unspecified or parsed.is_loopback:
            return True
        return parsed in ipaddress.IPv6Network("fe80::/10") or parsed in ipaddress.IPv6Network(
            "fc00::/7"
        )

    match = _IPV4_RE.match(ip)
    if not match:
        return False
    a, b = int(match.group(1)), int(match.group(2))
    return (
        a in (0, 10, 127)
        or (a == 100 and 64 <= b <= 127)  # CGNAT
        or (a == 169 and b == 254)  # link-local, incl. cloud metadata
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
    )


def _normalize_host(hostname: str) -> str:
    """Fold a hostname the way a browser's host parser would, for blocking.

    urlsplit hands back the raw host: still percent-encoded, and not
    Unicode-folded. A browser's host parser percent-decodes *once* and then
    applies UTS46 mapping (domain-to-ASCII) before it resolves anything -- it
    does not iteratively re-decode. The bounded loop below decodes more than
    that on inputs like double-encoded "%2541", but that only ever makes this
    function block a host a real browser would still allow through; since
    this is a blocklist, over-blocking is the safe direction, so the extra
    rounds are kept for defense in depth rather than to mirror browser
    behavior exactly.
    """
    host = hostname
    for _ in range(3):
        decoded = unquote(host)
        if decoded == host:
            break
        host = decoded

    # A decoded host may now be an IPv6 literal (e.g. from "%5B::1%5D"); strip
    # a surrounding bracket pair so the rest of this module sees the same
    # bracket-free form urlsplit normally hands it.
    if len(host) >= 2 and host[0] == "[" and host[-1] == "]":
        host = host[1:-1]

    host = unicodedata.normalize("NFKC", host)
    host = host.casefold()
    return host.rstrip(".")


def _is_private_host(hostname: str) -> bool:
    host = _normalize_host(hostname)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # _normalize_host already stripped the brackets from an IPv6 literal
    # (urlsplit does for a literal one, and normalization does for one that
    # only became a bracketed literal after percent-decoding).
    if ":" in host:
        return is_private_ip(host)
    ipv4 = as_ipv4(host)
    return is_private_ip(ipv4) if ipv4 is not None else False


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


async def resolves_to_private(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    # Normalize before classifying or resolving, exactly as _is_private_host
    # does: a percent-encoded or Unicode-folded host must be judged (and
    # resolved) by what it decodes to, not by its raw spelling, or this
    # DNS pre-check is trivially bypassed by the same encodings check_url
    # already had to handle.
    host = _normalize_host(host)
    if not host or ":" in host or as_ipv4(host) is not None:
        return False  # literals are already screened by check_url

    try:
        addresses = await asyncio.get_running_loop().run_in_executor(None, _resolve, host)
    except Exception:
        return False  # unresolvable names proceed and fail naturally
    return any(is_private_ip(address) for address in addresses)
