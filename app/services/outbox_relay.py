"""Outbox relay: committed domain events → realtime + channel delivery.

docs/backend/REALTIME_COMMUNICATION.md §6, docs/integrations/WEBSITE_CHAT.md §7.
Every ``event_outbox`` row is published to the organization's agent channel
(``rt:org:{org}``; agent gateways filter per socket). For website-widget
inboxes, non-private messages and status changes also go to the visitor's
channel through ``WebWidgetAdapter``. For WhatsApp / Messenger / Instagram
inboxes, new outgoing messages are marked ``pending`` and queued for
``app/channels/pipeline/outbound.deliver`` (worker: ``make channel-worker``).

Rows are claimed with ``FOR UPDATE SKIP LOCKED``, so several relays can run.
Failures are retried with backoff; after MAX_ATTEMPTS the row is ``failed``.
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import PermanentChannelError
from app.channels.pipeline.outbound import mark_pending_and_enqueue
from app.channels.registry import META_PROVIDERS
from app.channels.types import OutboundMessage
from app.channels.web_widget import WebWidgetAdapter
from app.core.logger import get_logger
from app.core.realtime import contact_channel, org_channel, publish
from app.core.security import utcnow
from app.models import ContactIdentity, Conversation, EventOutbox, Inbox, Message
from app.models.enums import ChannelProvider, MessageDirection, OutboxState
from app.services.widget import VISIBLE_DIRECTIONS, serialize_messages

logger = get_logger()

MAX_ATTEMPTS = 8
MESSAGE_EVENTS = ("message.created", "message.updated")
STATUS_EVENTS = (
    "conversation.status_changed",
    "conversation.updated",
    "conversation.handoff",
)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, 300))


async def _widget_recipients(
    session: AsyncSession, conv: Conversation
) -> tuple[str, ...]:
    """Realtime tokens of the contact's identities in this inbox: after a
    verified identify, every device of the customer gets the message."""
    rows = await session.scalars(
        select(ContactIdentity.realtime_token).where(
            ContactIdentity.inbox_id == conv.inbox_id,
            ContactIdentity.contact_id == conv.contact_id,
        )
    )
    return tuple(rows)


async def _to_widget(
    session: AsyncSession, redis: Redis, event: EventOutbox, conv: Conversation
) -> None:
    adapter = WebWidgetAdapter(redis)
    recipients = await _widget_recipients(session, conv)
    if not recipients:
        return
    if event.event_name in MESSAGE_EVENTS:
        msg = await session.get(Message, uuid.UUID(event.payload["message_id"]))
        if (
            msg is None
            or msg.is_private
            or msg.deleted_at is not None
            or msg.direction not in VISIBLE_DIRECTIONS
        ):
            return
        payload = (await serialize_messages(session, [msg]))[0].model_dump(mode="json")
        result = await adapter.send_message(
            OutboundMessage(
                message_id=msg.id,
                conversation_id=conv.id,
                recipients=recipients,
                payload=payload,
                event=event.event_name,
            )
        )
        if msg.direction == MessageDirection.OUTGOING and msg.external_id is None:
            msg.external_id = result.external_id
    elif event.event_name in STATUS_EVENTS:
        for token in recipients:
            await publish(
                redis,
                contact_channel(token),
                "conversation.status_changed",
                {"conversation_id": str(conv.id), "status": conv.status.value},
                audience="contact",
            )


async def dispatch(session: AsyncSession, redis: Redis, event: EventOutbox) -> None:
    await publish(
        redis,
        org_channel(event.organization_id),
        event.event_name,
        event.payload,
        workspace_id=event.workspace_id,
    )
    conv_id = event.payload.get("conversation_id")
    if not conv_id:
        return
    conv = await session.get(Conversation, uuid.UUID(conv_id))
    if conv is None:
        return
    inbox = await session.get(Inbox, conv.inbox_id)
    if inbox is None:
        return
    if inbox.provider == ChannelProvider.WEB_WIDGET:
        await _to_widget(session, redis, event, conv)
    elif (
        inbox.provider.value in META_PROVIDERS and event.event_name == "message.created"
    ):
        # Outbound delivery to WhatsApp / Messenger / Instagram (queue ch:outbound).
        msg = await session.get(Message, uuid.UUID(event.payload["message_id"]))
        if msg is not None:
            await mark_pending_and_enqueue(session, redis, msg)


async def dispatch_pending(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    batch: int = 100,
    now: datetime | None = None,
) -> int:
    """Dispatch one batch. Returns how many events were handled."""
    now = now or utcnow()
    async with session_factory() as session:
        events = list(
            await session.scalars(
                select(EventOutbox)
                .where(
                    EventOutbox.state == OutboxState.PENDING,
                    EventOutbox.available_at <= now,
                )
                .order_by(EventOutbox.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            try:
                async with session.begin_nested():
                    await dispatch(session, redis, event)
                event.state = OutboxState.DISPATCHED
                event.dispatched_at = now
                event.last_error = None
            except Exception as exc:
                event.attempts += 1
                event.last_error = repr(exc)[:500]
                if (
                    isinstance(exc, PermanentChannelError)
                    or event.attempts >= MAX_ATTEMPTS
                ):
                    event.state = OutboxState.FAILED
                else:
                    event.available_at = now + _backoff(event.attempts)
                logger.warning(
                    "Outbox dispatch failed",
                    extra={
                        "event_id": event.id,
                        "event": event.event_name,
                        "error": repr(exc)[:200],
                    },
                )
        await session.commit()
    return len(events)


async def run_relay(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stop: asyncio.Event,
    *,
    poll_seconds: float = 0.25,
) -> None:
    while not stop.is_set():
        handled = 0
        try:
            handled = await dispatch_pending(session_factory, redis)
        except Exception as exc:  # DB/Redis hiccup: keep the relay alive
            logger.error("Outbox relay poll failed", extra={"error": repr(exc)[:300]})
        if handled:
            continue  # more may be waiting
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass
