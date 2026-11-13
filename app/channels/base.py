"""The adapter contract every channel implements
(docs/integrations/CHANNEL_ADAPTER_ARCHITECTURE.md §3).

Adapters are async (our workers are asyncio, not Celery) and stateless apart
from the injected connection, decrypted secrets and HTTP client. They never
touch the database: everything comes in as arguments and goes out as return
values, so they're unit-tested with recorded payloads and a mock transport.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from app.channels.types import (
    ConnectionView,
    HealthResult,
    InboundAttachment,
    InboundEvent,
    OutboundAttachment,
    OutboundMessage,
    RouteKey,
    SenderProfile,
    SendResult,
)


class ChannelError(Exception):
    """Base for adapter errors. ``retryable`` decides the worker's behaviour."""

    retryable: bool = False
    code: str = "channel_error"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        self.retry_after = retry_after


class RetryableChannelError(ChannelError):
    retryable = True  # timeouts, 5xx, provider rate limits
    code = "temporary_error"


class PermanentChannelError(ChannelError):
    retryable = False  # invalid recipient, policy block
    code = "permanent_error"


class ReauthorizationRequiredError(PermanentChannelError):
    code = "reauthorization_required"  # connection → reauthorization_required


class ConnectionBrokenError(PermanentChannelError):
    code = "connection_error"  # account locked / policy block → connection error


class WindowClosedError(PermanentChannelError):
    code = "window_closed"  # outside the messaging window, no template/tag


class BaseChannelAdapter(ABC):
    provider: ClassVar[str]
    capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        connection: ConnectionView,
        secrets: Mapping[str, str],
        http: httpx.AsyncClient,
    ) -> None:
        self.connection = connection
        self.secrets = secrets  # decrypted on demand, never logged
        self.http = http

    # --- connection lifecycle ---------------------------------------------
    async def disconnect(self) -> None:
        """Unsubscribe at the provider. Safe to call twice."""

    async def refresh_credentials(self) -> dict[str, Any] | None:
        """Renew expiring tokens: {"secrets": {...}, "token_expires_at": dt}."""
        return None

    async def health_check(self) -> HealthResult:
        raise NotImplementedError

    # --- inbound ------------------------------------------------------------
    @classmethod
    def verify_webhook(
        cls, headers: Mapping[str, str], raw_body: bytes, secrets: list[bytes]
    ) -> bool:
        raise NotImplementedError

    @classmethod
    def route_keys(cls, payload: Mapping[str, Any]) -> list[RouteKey]:
        raise NotImplementedError

    def parse_incoming(self, unit: Mapping[str, Any]) -> list[InboundEvent]:
        raise NotImplementedError

    async def fetch_profile(self, source_id: str) -> SenderProfile | None:
        return None

    async def download_media(
        self, attachment: InboundAttachment, max_bytes: int
    ) -> tuple[bytes, str | None]:
        """(bytes, mime type)."""
        raise NotImplementedError

    # --- outbound -------------------------------------------------------------
    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> SendResult: ...

    async def send_attachment(
        self, message: OutboundMessage, attachment: OutboundAttachment
    ) -> SendResult:
        raise NotImplementedError

    async def mark_read(
        self, recipient_source_id: str, external_id: str | None
    ) -> None:
        return None
