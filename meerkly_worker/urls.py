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
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

FORBIDDEN_PREFIXES = ("file:", "chrome:", "chrome-extension:", "about:")
PRIVATE_HOST_ERROR = "Private, loopback, and link-local addresses are not allowed"

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_MAPPED_HEX_RE = re.compile(r"^([0-9a-f]{1,4}):([0-9a-f]{1,4})$")


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
    except ValueError as err:
        return UrlCheck(False, None, f"Invalid URL: {err}")

    if parts.scheme not in ("http", "https"):
        return UrlCheck(False, None, f"Only http and https are allowed, got: {parts.scheme}:")
    if not host:
        return UrlCheck(False, None, "URL must have a hostname")
    if block_private and _is_private_host(host):
        return UrlCheck(False, None, PRIVATE_HOST_ERROR)

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
        if part[0] in "+-":
            return None  # WHATWG parser only accepts digits (plus 0x/0 prefixes)
        try:
            if part.lower().startswith("0x"):
                if len(part) == 2:
                    return None
                numbers.append(int(part[2:], 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part[1:], 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None  # contains a letter: it is a domain, not an address

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
        if ip in ("::", "::1"):
            return True
        if re.match(r"^fe[89ab]", ip):  # link-local fe80::/10
            return True
        if re.match(r"^f[cd]", ip):  # unique-local fc00::/7
            return True
        if ip.startswith("::ffff:"):
            # IPv4-mapped: DNS gives ::ffff:127.0.0.1, URL parsers give
            # ::ffff:7f00:1 — handle both.
            rest = ip[7:]
            if ":" in rest:
                match = _MAPPED_HEX_RE.match(rest)
                if not match:
                    return False
                hi, lo = int(match.group(1), 16), int(match.group(2), 16)
                return is_private_ip(
                    f"{(hi >> 8) & 0xFF}.{hi & 0xFF}.{(lo >> 8) & 0xFF}.{lo & 0xFF}"
                )
            return is_private_ip(rest)
        return False

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


def _is_private_host(hostname: str) -> bool:
    host = hostname.lower()
    if host.endswith("."):
        host = host[:-1]
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # urlsplit already stripped the brackets from an IPv6 literal.
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
    if not host or ":" in host or as_ipv4(host) is not None:
        return False  # literals are already screened by check_url

    try:
        addresses = await asyncio.get_running_loop().run_in_executor(None, _resolve, host)
    except Exception:
        return False  # unresolvable names proceed and fail naturally
    return any(is_private_ip(address) for address in addresses)
