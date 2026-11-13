"""Adapter registry + connection/secret loading (CHANNEL_ADAPTER_ARCHITECTURE.md §3).

Secrets are stored encrypted as JSON: ``channel_connections.credentials_ciphertext``
(page / Instagram tokens, optional per-connection ``app_secret``), and for
WhatsApp on the WABA row (shared by its numbers) unless the connection
overrides it.
"""

import json
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter
from app.channels.meta.instagram.adapter import InstagramAdapter
from app.channels.meta.messenger.adapter import MessengerAdapter
from app.channels.meta.whatsapp.adapter import WhatsAppCloudAdapter
from app.channels.types import ConnectionView
from app.core.config import settings
from app.core.crypto import KEY_VERSION, decrypt_secret, encrypt_secret
from app.models import (
    ChannelConnection,
    ChannelRoute,
    WhatsappBusinessAccount,
    WhatsappPhoneNumber,
)

ADAPTERS: dict[str, type[BaseChannelAdapter]] = {
    "whatsapp_cloud": WhatsAppCloudAdapter,
    "facebook_messenger": MessengerAdapter,
    "instagram": InstagramAdapter,
}
# URL segment of /webhooks/meta/{product} → provider
META_PRODUCTS = {
    "whatsapp": "whatsapp_cloud",
    "messenger": "facebook_messenger",
    "instagram": "instagram",
}
META_PROVIDERS = frozenset(ADAPTERS)


def adapter_class(provider: str) -> type[BaseChannelAdapter]:
    return ADAPTERS[provider]  # KeyError = misconfiguration: fail loudly


def platform_secrets(provider: str) -> list[bytes]:
    if provider == "instagram":
        secret = settings.META_INSTAGRAM_APP_SECRET or settings.META_APP_SECRET
    else:
        secret = settings.META_APP_SECRET
    return [secret.encode()] if secret else []


def verify_token(provider: str) -> str:
    return {
        "whatsapp_cloud": settings.META_WHATSAPP_VERIFY_TOKEN,
        "facebook_messenger": settings.META_MESSENGER_VERIFY_TOKEN,
        "instagram": settings.META_INSTAGRAM_VERIFY_TOKEN,
    }[provider]


def encrypt_json(data: dict[str, Any]) -> tuple[bytes, int]:
    return encrypt_secret(json.dumps(data)), KEY_VERSION


def decrypt_json(ciphertext: bytes | None) -> dict[str, Any]:
    if not ciphertext:
        return {}
    value = json.loads(decrypt_secret(ciphertext))
    return value if isinstance(value, dict) else {}


async def connection_secrets(
    session: AsyncSession, connection: ChannelConnection
) -> dict[str, str]:
    secrets = decrypt_json(connection.credentials_ciphertext)
    if connection.provider == "whatsapp_cloud" and "access_token" not in secrets:
        waba = await session.scalar(
            select(WhatsappBusinessAccount)
            .join(
                WhatsappPhoneNumber,
                WhatsappPhoneNumber.waba_id == WhatsappBusinessAccount.id,
            )
            .where(WhatsappPhoneNumber.channel_connection_id == connection.id)
        )
        if waba is not None:
            secrets = {**decrypt_json(waba.credentials_ciphertext), **secrets}
    return {k: str(v) for k, v in secrets.items()}


async def routing_id(session: AsyncSession, connection: ChannelConnection) -> str:
    rid = connection.config.get("external_id") if connection.config else None
    if rid:
        return str(rid)
    route = await session.scalar(
        select(ChannelRoute.external_id).where(
            ChannelRoute.channel_connection_id == connection.id
        )
    )
    return str(route or "")


async def load_adapter(
    session: AsyncSession, connection: ChannelConnection, http: httpx.AsyncClient
) -> BaseChannelAdapter:
    view = ConnectionView(
        id=connection.id,
        organization_id=connection.organization_id,
        workspace_id=connection.workspace_id,
        inbox_id=connection.inbox_id,
        provider=connection.provider.value,
        status=connection.status.value,
        external_id=await routing_id(session, connection),
        config=dict(connection.config or {}),
        token_expires_at=connection.token_expires_at,
    )
    secrets = await connection_secrets(session, connection)
    return adapter_class(view.provider)(view, secrets, http)


async def connection_for_inbox(
    session: AsyncSession, inbox_id: uuid.UUID
) -> ChannelConnection | None:
    return await session.scalar(
        select(ChannelConnection).where(ChannelConnection.inbox_id == inbox_id)
    )
