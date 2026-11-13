"""Graph API client shared by the Meta adapters and the connect services."""

from typing import Any

import httpx

from app.channels.base import PermanentChannelError, RetryableChannelError
from app.channels.meta.common.errors import map_graph_error
from app.core.config import settings

TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class GraphClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str | None,
        *,
        product: str,
        base_url: str | None = None,
        version: str | None = None,
    ) -> None:
        self.http = http
        self.token = token
        self.product = product
        self.base = (base_url or settings.META_GRAPH_URL).rstrip("/")
        self.version = version or settings.META_GRAPH_API_VERSION

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        prefix = f"{self.base}/{self.version}" if self.version else self.base
        return f"{prefix}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        token: str | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {}
        bearer = token or self.token
        if auth and bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            response = await self.http.request(
                method,
                self.url(path),
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableChannelError(f"network: {exc!r}"[:300]) from exc
        try:
            body: Any = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise map_graph_error(
                self.product,
                response.status_code,
                body,
                response.headers.get("retry-after"),
            )
        return body if isinstance(body, dict) else {"data": body}

    async def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> dict[str, Any]:
        return await self.request("POST", path, **kw)

    async def delete(self, path: str, **kw: Any) -> dict[str, Any]:
        return await self.request("DELETE", path, **kw)

    async def paginate(
        self, path: str, *, params: dict[str, Any] | None = None, limit_pages: int = 20
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_url: str | None = path
        query = params
        for _ in range(limit_pages):
            if not next_url:
                break
            page = await self.get(next_url, params=query)
            out.extend(page.get("data", []))
            next_url = page.get("paging", {}).get("next")
            query = None  # the next URL carries its own query
        return out

    async def get_bytes(self, url: str, *, max_bytes: int) -> tuple[bytes, str | None]:
        """Download media (WhatsApp needs the bearer token; CDN URLs don't care)."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with self.http.stream(
                "GET", url, headers=headers, timeout=TIMEOUT
            ) as r:
                if r.status_code >= 400:
                    if r.status_code in (403, 404, 410):
                        raise PermanentChannelError(
                            f"media HTTP {r.status_code}", code="media_unavailable"
                        )
                    raise RetryableChannelError(f"media HTTP {r.status_code}")
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PermanentChannelError(
                        "media too large", code="media_too_large"
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise PermanentChannelError(
                            "media too large", code="media_too_large"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), r.headers.get("content-type")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableChannelError(f"network: {exc!r}"[:300]) from exc
