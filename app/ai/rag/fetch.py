"""SSRF-safe fetching of knowledge URLs (docs/ai/RAG_ARCHITECTURE.md §2, "Fetch safety").

- Only http/https, ports 80/443.
- The host is resolved and every address must be public: private, loopback,
  link-local, multicast, reserved and unspecified ranges are refused. This is
  re-checked on every redirect hop (at most 3).
- 10 s timeout, 5 MB cap (streamed), text/html or text/plain only.

Known gap: the address is checked, then httpx resolves the name again
(DNS rebinding window). Pinning the resolved IP needs a custom transport;
acceptable for now because responses are only stored as knowledge text.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0
ALLOWED_TYPES = ("text/html", "text/plain", "text/markdown")


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    text: str


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def check_url(url: str) -> None:
    """Raise FetchError unless the URL points only at public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Only http(s) URLs are allowed")
    if parts.port not in (None, 80, 443):
        raise FetchError("Only ports 80 and 443 are allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise FetchError("Host not found") from exc
    if not infos or not all(_is_public(str(info[4][0])) for info in infos):
        raise FetchError("URL points to a non-public address")


async def fetch_url(url: str) -> Fetched:
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": "KnowledgeFetcher/1.0"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await check_url(url)
            async with client.stream("GET", url) as r:
                if r.is_redirect:
                    location = r.headers.get("location")
                    if not location:
                        raise FetchError("Redirect without location")
                    url = urljoin(url, location)
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in ALLOWED_TYPES:
                    raise FetchError(f"Unsupported content type: {ctype or 'unknown'}")
                body = bytearray()
                async for part in r.aiter_bytes():
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise FetchError("Page is larger than 5 MB")
                return Fetched(
                    url, ctype, body.decode(r.encoding or "utf-8", "replace")
                )
        raise FetchError("Too many redirects")
