"""Outbound delivery (MESSAGE_PROCESSING.md §2, CHANNEL_ADAPTER_ARCHITECTURE.md §5).

The outbox relay enqueues ``deliver`` for every outgoing, non-private message
on a Meta inbox. Under ``lock:outbound:{conversation}`` older pending messages
go first, so two quick replies arrive in order.

Retryable errors back off 10 s, 30 s, 90 s, 4 min, 10 min, 20 min (6 tries),
then the message is ``failed``. Permanent errors fail at once with
``"<code>: <message>"`` in ``delivery_error``. ``ReauthorizationRequiredError``
also moves the connection to ``reauthorization_required`` (admins re-connect);
``ConnectionBrokenError`` to ``error``.
"""

import uuid
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels import queue
from app.channels.base import (
    ChannelError,
    ConnectionBrokenError,
    PermanentChannelError,
    ReauthorizationRequiredError,
)
from app.channels.meta.whatsapp.templates import TemplateError, validate_and_build
from app.channels.registry import META_PROVIDERS, load_adapter
from app.channels.types import OutboundAttachment, OutboundMessage
from app.core.logger import get_logger
from app.core.security import utcnow
from app.core.tenancy import bind_tenant
from app.models import (
    Attachment,
    ChannelConnection,
    ContactIdentity,
    Conversation,
    EventOutbox,
    Inbox,
    Message,
    WhatsappMessageTemplate,
    WhatsappPhoneNumber,
)
from app.models.enums import ConnectionStatus, DeliveryStatus, MessageDirection

logger = get_logger()

BACKOFF = (10, 30, 90, 240, 600, 1200)
LOCK_TTL = 120


def sendable(msg: Message) -> bool:
    attrs = msg.content_attributes or {}
    return (
        msg.direction == MessageDirection.OUTGOING
        and not msg.is_private
        and msg.deleted_at is None
        and msg.external_id is None
        and not attrs.get("external_echo")
        and msg.delivery_status in (DeliveryStatus.PENDING, DeliveryStatus.SENT)
    )


def _event(session: AsyncSession, msg: Message, payload: dict[str, Any]) -> None:
    session.add(
        EventOutbox(
            organization_id=msg.organization_id,
            workspace_id=msg.workspace_id,
            event_name="message.updated",
            aggregate_type="conversation",
            aggregate_id=msg.conversation_id,
            payload={
                "conversation_id": str(msg.conversation_id),
                "message_id": str(msg.id),
                **payload,
            },
        )
    )


async def _connection_problem(
    session: AsyncSession,
    connection: ChannelConnection,
    status: ConnectionStatus,
    error: str,
) -> None:
    if connection.status == status:
        return
    connection.status = status
    connection.last_error = error[:1000]
    connection.last_error_at = utcnow()
    session.add(
        EventOutbox(
            organization_id=connection.organization_id,
            workspace_id=connection.workspace_id,
            event_name="inbox.connection_changed",
            aggregate_type="inbox",
            aggregate_id=connection.inbox_id,
            payload={
                "inbox_id": str(connection.inbox_id),
                "status": status.value,
                "last_error": error[:300],
            },
        )
    )


async def _build(
    session: AsyncSession, msg: Message, conv: Conversation, provider: str
) -> OutboundMessage:
    identity = await session.get(ContactIdentity, conv.contact_identity_id)
    assert identity is not None
    attrs = dict(msg.content_attributes or {})
    if msg.content_type.value == "template" and provider == "whatsapp_cloud":
        tpl = dict(attrs.get("template") or {})
        row = await session.scalar(
            select(WhatsappMessageTemplate)
            .join(
                WhatsappPhoneNumber,
                WhatsappPhoneNumber.waba_id == WhatsappMessageTemplate.waba_id,
            )
            .join(
                ChannelConnection,
                ChannelConnection.id == WhatsappPhoneNumber.channel_connection_id,
            )
            .where(
                ChannelConnection.inbox_id == conv.inbox_id,
                WhatsappMessageTemplate.name == tpl.get("name"),
                WhatsappMessageTemplate.language == tpl.get("language"),
            )
        )
        if row is None:
            raise TemplateError("Template not found (sync templates?)")
        components, rendered = validate_and_build(
            tpl,
            components=row.components,
            parameter_format=row.parameter_format,
            status=row.status,
        )
        tpl["graph_components"] = components
        attrs["template"] = tpl
        if not msg.content:
            msg.content = rendered
    attachments: list[OutboundAttachment] = []
    for a in await session.scalars(
        select(Attachment).where(Attachment.message_id == msg.id)
    ):
        if a.external_url and a.external_url.startswith("https://"):
            attachments.append(
                OutboundAttachment(
                    kind=a.kind.value,  # type: ignore[arg-type]
                    url=a.external_url,
                    filename=a.fallback_title,
                    mime_type=(a.meta or {}).get("mime_type"),
                )
            )
        else:
            raise PermanentChannelError(
                "Attachment has no public URL", code="attachment_unavailable"
            )
    reply_to = (
        await session.get(Message, msg.in_reply_to_id) if msg.in_reply_to_id else None
    )
    return OutboundMessage(
        message_id=msg.id,
        conversation_id=conv.id,
        recipient_source_id=identity.source_id,
        text=msg.content,
        content_type=msg.content_type.value,
        content_attributes=attrs,
        attachments=attachments,
        in_reply_to_external_id=reply_to.external_id if reply_to else None,
        last_incoming_at=conv.last_incoming_at,
        sender_kind=msg.sender_kind.value,
    )


async def deliver(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    http: httpx.AsyncClient,
    message_id: uuid.UUID,
) -> str:
    """Send one message (and older pending ones of its conversation first)."""
    async with session_factory() as s:
        msg = await s.get(Message, message_id)
        if msg is None or not sendable(msg):
            return "skipped"
        conversation_id = msg.conversation_id
    lock_key = f"lock:outbound:{conversation_id}"
    if not await queue.acquire_lock(redis, lock_key, LOCK_TTL):
        await queue.enqueue(redis, queue.OUTBOUND, str(message_id), delay=2.0)
        return "requeued"
    try:
        async with session_factory() as s:
            msg = await s.get(Message, message_id)
            assert msg is not None
            earlier = list(
                await s.scalars(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.direction == MessageDirection.OUTGOING,
                        Message.is_private.is_(False),
                        Message.external_id.is_(None),
                        Message.delivery_status == DeliveryStatus.PENDING,
                        Message.sequence_number < msg.sequence_number,
                    )
                    .order_by(Message.sequence_number)
                )
            )
        outcome = "skipped"
        for mid in [*earlier, message_id]:
            outcome = await _send_one(session_factory, redis, http, mid)
            if outcome == "retry" and mid != message_id:
                # Keep order: the later message waits for the earlier one.
                await queue.enqueue(
                    redis, queue.OUTBOUND, str(message_id), delay=BACKOFF[0]
                )
                return "waiting"
        return outcome
    finally:
        await queue.release_lock(redis, lock_key)


async def _send_one(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    http: httpx.AsyncClient,
    message_id: uuid.UUID,
) -> str:
    async with session_factory() as s:
        msg = await s.scalar(
            select(Message).where(Message.id == message_id).with_for_update()
        )
        if msg is None or not sendable(msg):
            return "skipped"
        await bind_tenant(s, msg.organization_id)
        conv = await s.get(Conversation, msg.conversation_id)
        inbox = await s.get(Inbox, msg.inbox_id)
        connection = await s.scalar(
            select(ChannelConnection).where(ChannelConnection.inbox_id == msg.inbox_id)
        )
        if (
            conv is None
            or inbox is None
            or connection is None
            or inbox.provider.value not in META_PROVIDERS
        ):
            return "skipped"
        attempts = (
            int((msg.content_attributes or {}).get("delivery", {}).get("attempts", 0))
            + 1
        )
        try:
            if connection.status != ConnectionStatus.ACTIVE:
                code = (
                    "reauthorization_required"
                    if connection.status == ConnectionStatus.REAUTHORIZATION_REQUIRED
                    else f"connection_{connection.status.value}"
                )
                raise PermanentChannelError("The channel isn't connected", code=code)
            adapter = await load_adapter(s, connection, http)
            outbound = await _build(s, msg, conv, connection.provider.value)
            result = await adapter.send_message(outbound)
        except ChannelError as exc:
            attrs = {
                **(msg.content_attributes or {}),
                "delivery": {"attempts": attempts, "last_error": exc.code},
            }
            msg.content_attributes = attrs
            if isinstance(exc, ReauthorizationRequiredError):
                await _connection_problem(
                    s,
                    connection,
                    ConnectionStatus.REAUTHORIZATION_REQUIRED,
                    exc.message,
                )
            elif isinstance(exc, ConnectionBrokenError):
                await _connection_problem(
                    s, connection, ConnectionStatus.ERROR, exc.message
                )
            if exc.retryable and attempts < len(BACKOFF):
                msg.delivery_status = DeliveryStatus.PENDING
                await s.commit()
                delay = max(BACKOFF[attempts - 1], exc.retry_after or 0)
                await queue.enqueue(redis, queue.OUTBOUND, str(msg.id), delay=delay)
                return "retry"
            msg.delivery_status = DeliveryStatus.FAILED
            msg.delivery_error = f"{exc.code}: {exc.message}"[:1000]
            _event(
                s,
                msg,
                {"delivery_status": "failed", "delivery_error": msg.delivery_error},
            )
            await s.commit()
            logger.warning(
                "Outbound message failed",
                extra={
                    "message_id": str(msg.id),
                    "code": exc.code,
                    "attempts": attempts,
                },
            )
            return "failed"
        # The echo may have attached the same id already; never fight it.
        if msg.external_id is None:
            clash = await s.scalar(
                select(Message.id).where(
                    Message.inbox_id == msg.inbox_id,
                    Message.external_id == result.external_id,
                )
            )
            if clash is None:
                msg.external_id = result.external_id
        msg.delivery_status = DeliveryStatus.SENT
        msg.delivery_error = None
        _event(s, msg, {"delivery_status": "sent", "external_id": result.external_id})
        await s.commit()
        return "sent"


async def mark_pending_and_enqueue(
    session: AsyncSession, redis: Redis, msg: Message
) -> bool:
    """Called by the outbox relay for message.created on a Meta inbox."""
    if not sendable(msg):
        return False
    if msg.delivery_status != DeliveryStatus.PENDING:
        msg.delivery_status = DeliveryStatus.PENDING
    await queue.enqueue(redis, queue.OUTBOUND, str(msg.id))
    return True
