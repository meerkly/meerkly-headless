"""Proxy URL parsing and rotating session ids.

A single PROXY_URL env var carries the full upstream proxy, credentials and
all, e.g. http://user-sid-<sid>-ttl-60:password@host:1337. The literal token
``<sid>`` is replaced with a fresh 6-digit session id: for session-based
proxies the sid pins the exit IP for its TTL, so rotating the sid rotates the
egress address.

SECURITY: the URL and the credentials in it are secrets. Nothing in this
module logs them, and every error message names only the offending component
(scheme / host / port), never the URL or the userinfo. Callers must log only
the credential-free ``server`` this returns.
"""

from __future__ import annotations

import secrets
from urllib.parse import unquote, urlsplit

# http(s) go through Playwright's own proxy= kwarg; socks5 is patched into
# Firefox by invisible_playwright. All three are Playwright-style dicts.
PROXY_SCHEMES = ("http", "https", "socks5")
SID_PLACEHOLDER = "<sid>"


def generate_sid() -> str:
    """A random 6-digit session id, zero-padded.

    Digits only, so it is safe to splice into the raw URL before parsing (no
    percent-encoding concerns).
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def build_proxy_options(proxy_url: str, sid: str) -> dict[str, str]:
    """Turn PROXY_URL into invisible_playwright's ``proxy=`` dict.

    Returns ``{"server": ..., "username"?: ..., "password"?: ...}`` with the
    credentials split out of the server (an embedded ``user:pass@`` breaks the
    library's socks parser and Playwright wants them separate anyway). Raises
    ``ValueError`` on anything malformed — callers rely on this to fail the
    launch rather than silently crawl from the unproxied host IP.
    """
    # Substitute on the raw string so a <sid> in the username, password, or
    # anywhere else is covered before the structure is parsed away.
    substituted = proxy_url.replace(SID_PLACEHOLDER, sid)

    try:
        parts = urlsplit(substituted)
        # .port validates and raises here (not lazily) on a non-numeric port.
        port = parts.port
    except ValueError as err:
        raise ValueError(f"PROXY_URL is not a valid URL: {err}") from err

    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        allowed = ", ".join(PROXY_SCHEMES)
        raise ValueError(f"PROXY_URL scheme must be one of {allowed}, got '{scheme or '(none)'}'")

    host = parts.hostname
    if not host:
        raise ValueError("PROXY_URL is missing a host")
    if port is None:
        raise ValueError("PROXY_URL must include an explicit port")
    if parts.path or parts.query or parts.fragment:
        raise ValueError("PROXY_URL must not include a path, query, or fragment")

    # urlsplit strips IPv6 brackets from hostname; restore them for the server.
    server_host = f"[{host}]" if ":" in host else host
    options: dict[str, str] = {"server": f"{scheme}://{server_host}:{port}"}

    # Credentials come back percent-encoded from urlsplit; Playwright wants raw.
    if parts.username:
        options["username"] = unquote(parts.username)
    if parts.password is not None:
        options["password"] = unquote(parts.password)

    return options
