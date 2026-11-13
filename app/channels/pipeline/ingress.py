"""Webhook ingress (MESSAGE_PROCESSING.md §1.1): fast path, no provider calls.

parse JSON → route_keys → candidate secrets → verify signature over the raw
bytes → INSERT units ON CONFLICT DO NOTHING → enqueue new ids after commit.
Nothing is stored or enqueued before the signature is verified.
"""

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import queue
from app.channels.registry import decrypt_json, platform_secrets
from app.channels.types import RouteKey
from app.core.logger import get_logger
from app.models import ChannelConnection, ChannelRoute, InboundWebhookEvent

logger = get_logger()


async def candidate_secrets(
    session: AsyncSession, provider: str, units: list[RouteKey]
) -> list[bytes]:
    """The platform app secret + per-connection app secrets (tenants with their
    own Meta app) of the routed connections."""
    secrets = platform_secrets(provider)
    ids = {u.routing_id for u in units if not u.routing_id.startswith("waba:")}
    if ids:
        rows = await session.scalars(
            select(ChannelConnection.credentials_ciphertext)
            .join(
                ChannelRoute, ChannelRoute.channel_connection_id == ChannelConnection.id
            )
            .where(ChannelRoute.provider == provider, ChannelRoute.external_id.in_(ids))
        )
        for ciphertext in rows:
            try:
                own = decrypt_json(ciphertext).get("app_secret")
            except Exception:
                own = None
            if own:
                secrets.append(str(own).encode())
    return secrets


async def record_and_enqueue(
    session: AsyncSession, redis: Redis | None, provider: str, units: list[RouteKey]
) -> list[uuid.UUID]:
    """Returns the ids of new (not duplicate) events."""
    if not units:
        return []
    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(),
            "provider": provider,
            "dedup_key": u.dedup_key[:255],
            "event_type": u.event_type,
            "payload": {
                "routing_id": u.routing_id,
                "ordering_key": u.ordering_key,
                "unit": u.payload,
            },
            "signature_valid": True,
        }
        for u in units
    ]
    # One delivery can carry the same unit twice: keep the first.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["dedup_key"] not in seen:
            seen.add(row["dedup_key"])
            unique.append(row)
    stmt = (
        insert(InboundWebhookEvent)
        .values(unique)
        .on_conflict_do_nothing(index_elements=["provider", "dedup_key"])
        .returning(InboundWebhookEvent.id)
    )
    new_ids = list(await session.scalars(stmt))
    await session.commit()
    if redis is not None and new_ids:
        try:
            await queue.enqueue_many(redis, queue.INBOUND, [str(i) for i in new_ids])
        except Exception as exc:  # the sweeper picks them up
            logger.warning("Inbound enqueue failed", extra={"error": repr(exc)[:200]})
    return new_ids
