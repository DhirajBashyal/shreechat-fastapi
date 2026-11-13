"""Inbound pipeline (MESSAGE_PROCESSING.md §1.2–1.3, CHANNEL_ADAPTER_ARCHITECTURE.md §4).

    claim event → route lookup → ordering lock (inbox, customer) →
    adapter.parse_incoming → handlers → commit → media downloads + AI wake-up

Idempotency, three layers: ``inbound_webhook_events (provider, dedup_key)``,
``messages (inbox_id, external_id)`` with ON CONFLICT DO NOTHING, and
``contact_identities (inbox_id, source_id)`` with ON CONFLICT DO NOTHING.
The ordering lock makes a burst of first messages (a 5-photo album) land in
one conversation, in order.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.queue import enqueue_incoming
from app.channels import queue
from app.channels.base import BaseChannelAdapter
from app.channels.registry import load_adapter
from app.channels.types import (
    DeletionEvent,
    DeliveryUpdate,
    InboundEvent,
    InboundMessage,
    ReactionEvent,
    ReferralEvent,
    SenderProfile,
)
from app.core.config import settings
from app.core.logger import get_logger
from app.core.security import utcnow
from app.core.tenancy import bind_tenant
from app.models import (
    Attachment,
    ChannelConnection,
    ChannelRoute,
    Contact,
    ContactIdentity,
    Conversation,
    EventOutbox,
    InboundWebhookEvent,
    Inbox,
    Message,
    WhatsappBusinessAccount,
    WhatsappMessageTemplate,
)
from app.models.enums import (
    AttachmentKind,
    ConnectionStatus,
    ConversationStatus,
    DeliveryStatus,
    InboundEventState,
    MessageContentType,
    MessageDirection,
    SenderKind,
)
from app.services import conversation_ai
from app.services.widget import inbox_ai_agent

logger = get_logger()

LOCK_TTL = 30
LOCK_RETRY_SECONDS = 1.5
LOCK_MAX_WAITS = 20
MAX_FAILURES = 3
STATUS_RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}
DOWNLOADABLE = {"image", "audio", "video", "file", "sticker"}


class RequeueLaterError(Exception):
    """Not a failure: try again after ``delay`` seconds (lock busy, echo or
    status that arrived before our own send result)."""

    def __init__(self, delay: float, reason: str) -> None:
        super().__init__(reason)
        self.delay = delay
        self.reason = reason


class IgnoredError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class AfterCommit:
    ai_wake: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    media: list[uuid.UUID] = field(default_factory=list)


@dataclass
class Ctx:
    session: AsyncSession
    redis: Redis
    inbox: Inbox
    connection: ChannelConnection
    adapter: BaseChannelAdapter
    event_id: uuid.UUID
    after: AfterCommit = field(default_factory=AfterCommit)
    ignored: list[str] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.connection.provider.value


async def _bump(redis: Redis, key: str) -> int:
    n = int(await redis.incr(key))
    await redis.expire(key, 3600)
    return n


def _outbox(ctx: Ctx, conv: Conversation, event: str, payload: dict[str, Any]) -> None:
    ctx.session.add(
        EventOutbox(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            event_name=event,
            aggregate_type="conversation",
            aggregate_id=conv.id,
            payload={"conversation_id": str(conv.id), **payload},
        )
    )


# =============================================================================
# Contacts and conversations
# =============================================================================


def _fallback_name(provider: str, profile: SenderProfile) -> str:
    if profile.name:
        return profile.name[:255]
    if profile.username:
        return profile.username[:255]
    if profile.phone_number:
        return profile.phone_number
    label = {"facebook_messenger": "Messenger", "instagram": "Instagram"}.get(
        provider, "Customer"
    )
    return f"{label} user {profile.source_id[-4:]}"


async def resolve_contact(
    ctx: Ctx, sender: SenderProfile
) -> tuple[Contact, ContactIdentity]:
    s = ctx.session
    ids = [sender.source_id, *sender.alternate_source_ids]
    found = list(
        await s.scalars(
            select(ContactIdentity).where(
                ContactIdentity.inbox_id == ctx.inbox.id,
                ContactIdentity.source_id.in_(ids),
            )
        )
    )
    if found:
        identity = next((i for i in found if i.source_id == sender.source_id), found[0])
        if identity.source_id != sender.source_id:  # wa_id → BSUID upgrade
            identity.profile = {
                **(identity.profile or {}),
                "previous_source_id": identity.source_id,
            }
            identity.source_id = sender.source_id
        contact = await s.get(Contact, identity.contact_id)
        assert contact is not None
        return contact, identity

    profile = sender
    if not sender.name and ctx.provider != "whatsapp_cloud":
        fetched = await ctx.adapter.fetch_profile(sender.source_id)  # best effort
        if fetched is not None:
            profile = fetched.model_copy(
                update={"alternate_source_ids": sender.alternate_source_ids}
            )
    contact = None
    if profile.phone_number:  # WhatsApp only: conservative cross-channel match
        contact = await s.scalar(
            select(Contact)
            .where(
                Contact.workspace_id == ctx.inbox.workspace_id,
                Contact.phone_number == profile.phone_number,
                Contact.deleted_at.is_(None),
            )
            .limit(1)
        )
    if contact is None:
        extra = {
            k: v
            for k, v in (
                ("avatar_url", profile.avatar_url),
                ("username", profile.username),
            )
            if v
        }
        contact = Contact(
            id=uuid.uuid4(),
            organization_id=ctx.inbox.organization_id,
            workspace_id=ctx.inbox.workspace_id,
            name=_fallback_name(ctx.provider, profile),
            phone_number=profile.phone_number,
            contact_type="lead",
            additional_attributes=extra,
            last_activity_at=utcnow(),
        )
        s.add(contact)
        await s.flush()
        s.add(
            EventOutbox(
                organization_id=contact.organization_id,
                workspace_id=contact.workspace_id,
                event_name="contact.created",
                aggregate_type="contact",
                aggregate_id=contact.id,
                payload={"contact_id": str(contact.id)},
            )
        )
    identity_profile: dict[str, Any] = {
        k: v
        for k, v in (
            ("name", profile.name),
            ("username", profile.username),
            ("avatar_url", profile.avatar_url),
            ("alternate_source_ids", sender.alternate_source_ids or None),
        )
        if v
    }
    if ctx.provider == "whatsapp_cloud" and profile.phone_number:
        identity_profile["wa_id"] = profile.phone_number.lstrip("+")
    new_id = await s.scalar(
        insert(ContactIdentity)
        .values(
            id=uuid.uuid4(),
            organization_id=ctx.inbox.organization_id,
            workspace_id=ctx.inbox.workspace_id,
            contact_id=contact.id,
            inbox_id=ctx.inbox.id,
            source_id=sender.source_id,
            profile=identity_profile,
        )
        .on_conflict_do_nothing(index_elements=["inbox_id", "source_id"])
        .returning(ContactIdentity.id)
    )
    created = await s.scalar(
        select(ContactIdentity).where(
            ContactIdentity.inbox_id == ctx.inbox.id,
            ContactIdentity.source_id == sender.source_id,
        )
    )
    assert created is not None
    if new_id is None:  # lost a race: use the winner's contact
        winner = await s.get(Contact, created.contact_id)
        assert winner is not None
        return winner, created
    return contact, created


async def _set_status(ctx: Ctx, conv: Conversation, status: ConversationStatus) -> None:
    previous = conv.status
    conv.status = status
    conv.status_changed_at = utcnow()
    _outbox(
        ctx,
        conv,
        "conversation.status_changed",
        {"status": status.value, "previous_status": previous.value},
    )


async def resolve_conversation(
    ctx: Ctx, contact: Contact, identity: ContactIdentity
) -> tuple[Conversation, bool]:
    """Returns (conversation, stored_after_resolution). §1.2 ConversationResolver."""
    s = ctx.session
    latest = await s.scalar(
        select(Conversation)
        .where(
            Conversation.inbox_id == ctx.inbox.id,
            Conversation.contact_identity_id == identity.id,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is not None and latest.status != ConversationStatus.RESOLVED:
        if latest.status == ConversationStatus.SNOOZED:
            latest.snoozed_until = None
            await _set_status(ctx, latest, ConversationStatus.OPEN)
        return latest, False
    if latest is not None:
        if ctx.inbox.lock_to_single_conversation:
            await _set_status(
                ctx,
                latest,
                ConversationStatus.PENDING
                if latest.ai_active
                else ConversationStatus.OPEN,
            )
            return latest, False
        if not ctx.inbox.allow_messages_after_resolved:
            return latest, True
    agent = await inbox_ai_agent(s, ctx.inbox.id)
    number = await s.scalar(
        select(func.next_workspace_counter(ctx.inbox.workspace_id, "conversation"))
    )
    conv = Conversation(
        id=uuid.uuid4(),
        organization_id=ctx.inbox.organization_id,
        workspace_id=ctx.inbox.workspace_id,
        inbox_id=ctx.inbox.id,
        contact_id=contact.id,
        contact_identity_id=identity.id,
        display_number=int(number or 0),
        status=ConversationStatus.PENDING if agent else ConversationStatus.OPEN,
        ai_agent_id=agent.id if agent else None,
        ai_active=agent is not None,
        additional_attributes={},
    )
    s.add(conv)
    await s.flush()
    _outbox(
        ctx,
        conv,
        "conversation.created",
        {"status": conv.status.value, "ai_active": conv.ai_active},
    )
    return conv, False


async def _by_external_id(ctx: Ctx, external_id: str | None) -> Message | None:
    if not external_id:
        return None
    return await ctx.session.scalar(
        select(Message).where(
            Message.inbox_id == ctx.inbox.id, Message.external_id == external_id
        )
    )


def _content_type(value: str) -> MessageContentType:
    try:
        return MessageContentType(value)
    except ValueError:
        return MessageContentType.UNSUPPORTED


async def _insert_message(
    ctx: Ctx,
    conv: Conversation,
    item: InboundMessage,
    *,
    direction: MessageDirection,
    sender_kind: SenderKind,
    sender_contact_id: uuid.UUID | None,
    extra_attributes: dict[str, Any] | None = None,
) -> Message | None:
    """INSERT … ON CONFLICT (inbox_id, external_id) DO NOTHING. None = duplicate."""
    s = ctx.session
    reply_to = await _by_external_id(ctx, item.in_reply_to_external_id)
    latest = await s.scalar(
        select(func.max(Message.created_at)).where(Message.conversation_id == conv.id)
    )
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "organization_id": conv.organization_id,
        "workspace_id": conv.workspace_id,
        "inbox_id": conv.inbox_id,
        "conversation_id": conv.id,
        "direction": direction,
        "content_type": _content_type(item.content_type),
        "content": item.text,
        "content_attributes": {**item.content_attributes, **(extra_attributes or {})},
        "sender_kind": sender_kind,
        "sender_contact_id": sender_contact_id,
        "external_id": item.external_id,
        "in_reply_to_id": reply_to.id if reply_to else None,
        "delivery_status": DeliveryStatus.SENT
        if direction == MessageDirection.OUTGOING
        else DeliveryStatus.DELIVERED,
    }
    # Keep customer order: a late message with an earlier provider timestamp
    # is placed at that timestamp.
    if latest is not None and item.sent_at < latest:
        values["created_at"] = item.sent_at
    new_id = await s.scalar(
        insert(Message)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["inbox_id", "external_id"],
            index_where=text("external_id IS NOT NULL"),
        )
        .returning(Message.id)
    )
    if new_id is None:
        return None
    msg = await s.get(Message, new_id)
    assert msg is not None
    for att in item.attachments:
        external_url = att.url
        if external_url is None and att.provider_media_id:
            external_url = f"{settings.META_GRAPH_URL}/{settings.META_GRAPH_API_VERSION}/{att.provider_media_id}"
        meta = {
            k: v
            for k, v in (
                ("provider_media_id", att.provider_media_id),
                ("mime_type", att.mime_type),
                ("sha256", att.sha256),
                ("is_voice", att.is_voice or None),
                ("filename", att.filename),
            )
            if v
        }
        row = Attachment(
            id=uuid.uuid4(),
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            message_id=msg.id,
            kind=AttachmentKind(att.kind),
            external_url=external_url,
            fallback_title=(att.title or att.filename),
            latitude=att.latitude,
            longitude=att.longitude,
            meta=meta,
        )
        s.add(row)
        if att.kind in DOWNLOADABLE and external_url:
            ctx.after.media.append(row.id)
    _outbox(
        ctx,
        conv,
        "message.created",
        {
            "message_id": str(msg.id),
            "private": False,
            **(
                {"external_echo": True}
                if direction == MessageDirection.OUTGOING
                else {}
            ),
        },
    )
    return msg


# =============================================================================
# Handlers
# =============================================================================


async def handle_message(ctx: Ctx, item: InboundMessage) -> None:
    if item.direction == "outgoing_echo":
        await handle_echo(ctx, item)
        return
    if await _by_external_id(ctx, item.external_id) is not None:
        return  # duplicate (different batching of the same message)
    contact, identity = await resolve_contact(ctx, item.sender)
    if contact.blocked:
        ctx.ignored.append("blocked_contact")
        return
    conv, after_resolution = await resolve_conversation(ctx, contact, identity)
    extra = {"ignored": True} if after_resolution else None
    msg = await _insert_message(
        ctx,
        conv,
        item,
        direction=MessageDirection.INCOMING,
        sender_kind=SenderKind.CONTACT,
        sender_contact_id=contact.id,
        extra_attributes=extra,
    )
    if msg is None:
        return
    now = utcnow()
    contact.last_activity_at = now
    conv.last_activity_at = now
    if item.conversation_attributes:
        conv.additional_attributes = {
            **(conv.additional_attributes or {}),
            **item.conversation_attributes,
        }
    if after_resolution:
        conversation_ai.add_activity(
            ctx.session, conv, "Customer wrote after the conversation was resolved"
        )
        return
    conv.last_incoming_at = now
    conv.waiting_since = conv.waiting_since or now
    if conv.ai_active:
        ctx.after.ai_wake.append((conv.id, msg.id))


async def handle_echo(ctx: Ctx, item: InboundMessage) -> None:
    """§5 echoes: ours (match external_id or Messenger metadata) or a reply typed
    in Meta's apps (stored as an external outgoing message, never re-sent)."""
    existing = await _by_external_id(ctx, item.external_id)
    if existing is not None:
        return
    if item.echo_metadata:
        try:
            ours_id = uuid.UUID(item.echo_metadata)
        except ValueError:
            ours_id = None
        if ours_id is not None:
            ours = await ctx.session.scalar(
                select(Message)
                .where(Message.id == ours_id, Message.inbox_id == ctx.inbox.id)
                .with_for_update()
            )
            if ours is not None:
                if ours.external_id is None:
                    ours.external_id = item.external_id
                    if ours.delivery_status == DeliveryStatus.PENDING:
                        ours.delivery_status = DeliveryStatus.SENT
                return
    if (
        ctx.provider == "instagram"
        and await _bump(ctx.redis, f"inbound:echo:{ctx.event_id}") == 1
    ):
        # Instagram echoes carry no metadata: give our send call 3 s to store
        # its external_id first.
        raise RequeueLaterError(3.0, "echo_wait")
    contact, identity = await resolve_contact(ctx, item.sender)
    conv, _ = await resolve_conversation(ctx, contact, identity)
    msg = await _insert_message(
        ctx,
        conv,
        item,
        direction=MessageDirection.OUTGOING,
        sender_kind=SenderKind.SYSTEM,
        sender_contact_id=None,
        extra_attributes={"external_echo": True},
    )
    if msg is None:
        return
    conv.last_activity_at = utcnow()
    conv.waiting_since = None
    if conv.ai_active:  # a human replied from the provider's app
        conv.ai_active = False
        _outbox(
            ctx,
            conv,
            "conversation.updated",
            {"ai_active": False, "reason": "external_echo"},
        )


async def handle_delivery(ctx: Ctx, item: DeliveryUpdate) -> None:
    s = ctx.session
    if item.external_id:
        targets = list(
            await s.scalars(
                select(Message)
                .where(
                    Message.inbox_id == ctx.inbox.id,
                    Message.external_id == item.external_id,
                )
                .with_for_update()
            )
        )
        if not targets:
            if await _bump(ctx.redis, f"inbound:nf:{ctx.event_id}") <= 3:
                raise RequeueLaterError(10.0, "message_not_found")
            ctx.ignored.append("message_not_found")
            return
    else:
        identity_ids = select(ContactIdentity.id).where(
            ContactIdentity.inbox_id == ctx.inbox.id,
            ContactIdentity.source_id == item.recipient_source_id,
        )
        conv_ids = select(Conversation.id).where(
            Conversation.contact_identity_id.in_(identity_ids)
        )
        query = select(Message).where(
            Message.conversation_id.in_(conv_ids),
            Message.direction == MessageDirection.OUTGOING,
            Message.is_private.is_(False),
            Message.delivery_status.in_(["pending", "sent", "delivered"]),
        )
        if item.watermark is not None:
            query = query.where(Message.created_at <= item.watermark)
        targets = list(await s.scalars(query.with_for_update()))
    seen_convs: set[uuid.UUID] = set()
    for msg in targets:
        changed = False
        current = msg.delivery_status.value
        if item.status == "failed":
            if current in ("pending", "sent"):
                msg.delivery_status = DeliveryStatus.FAILED
                msg.delivery_error = f"{item.error_code}: {item.error_message}"[:1000]
                changed = True
        elif STATUS_RANK.get(item.status, 0) > STATUS_RANK.get(current, 4):
            msg.delivery_status = DeliveryStatus(item.status)
            changed = True
        if item.billing:
            msg.content_attributes = {
                **(msg.content_attributes or {}),
                "whatsapp_billing": item.billing,
            }
        if changed:
            conv = await s.get(Conversation, msg.conversation_id)
            assert conv is not None
            _outbox(
                ctx,
                conv,
                "message.updated",
                {
                    "message_id": str(msg.id),
                    "delivery_status": msg.delivery_status.value,
                },
            )
            if item.status == "read" and conv.id not in seen_convs:
                seen_convs.add(conv.id)
                if (
                    conv.contact_last_seen_at is None
                    or conv.contact_last_seen_at < item.occurred_at
                ):
                    conv.contact_last_seen_at = item.occurred_at


async def handle_reaction(ctx: Ctx, item: ReactionEvent) -> None:
    target = await _by_external_id(ctx, item.target_external_id)
    if target is None:
        ctx.ignored.append("reaction_target_not_found")
        return
    attrs = dict(target.content_attributes or {})
    reactions = [r for r in attrs.get("reactions", []) if r.get("by") != "contact"]
    if item.emoji:
        reactions.append(
            {"emoji": item.emoji, "by": "contact", "at": item.occurred_at.isoformat()}
        )
    attrs["reactions"] = reactions
    target.content_attributes = attrs
    conv = await ctx.session.get(Conversation, target.conversation_id)
    assert conv is not None
    _outbox(
        ctx,
        conv,
        "message.updated",
        {"message_id": str(target.id), "changes": ["reactions"]},
    )


async def handle_deletion(ctx: Ctx, item: DeletionEvent) -> None:
    target = await _by_external_id(ctx, item.external_id)
    if target is None or target.deleted_at is not None:
        return
    target.deleted_at = utcnow()
    target.content = None
    target.content_attributes = {**(target.content_attributes or {}), "deleted": True}
    conv = await ctx.session.get(Conversation, target.conversation_id)
    assert conv is not None
    _outbox(
        ctx,
        conv,
        "message.updated",
        {"message_id": str(target.id), "changes": ["deleted"]},
    )


async def handle_referral(ctx: Ctx, item: ReferralEvent) -> None:
    contact, identity = await resolve_contact(ctx, item.sender)
    conv = await ctx.session.scalar(
        select(Conversation)
        .where(
            Conversation.contact_identity_id == identity.id,
            Conversation.status != ConversationStatus.RESOLVED,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    if conv is not None:
        conv.additional_attributes = {
            **(conv.additional_attributes or {}),
            "meta_referral": item.referral,
        }
    else:
        contact.additional_attributes = {
            **(contact.additional_attributes or {}),
            "meta_referral": item.referral,
        }


HANDLERS = {
    "message": handle_message,
    "delivery": handle_delivery,
    "reaction": handle_reaction,
    "deletion": handle_deletion,
    "referral": handle_referral,
}


# =============================================================================
# Template status (routed by WABA, not by number)
# =============================================================================


async def handle_template_status(session: AsyncSession, unit: dict[str, Any]) -> int:
    value = unit.get("value") or {}
    template_id = str(value.get("message_template_id", ""))
    event = str(value.get("event", "")).upper()
    if not template_id or not event:
        return 0
    waba_ids = select(WhatsappBusinessAccount.id).where(
        WhatsappBusinessAccount.waba_id == str(unit.get("waba_id"))
    )
    result = await session.execute(
        update(WhatsappMessageTemplate)
        .where(
            WhatsappMessageTemplate.waba_id.in_(waba_ids),
            WhatsappMessageTemplate.external_template_id == template_id,
        )
        .values(status=event, updated_at=utcnow())
    )
    await session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# =============================================================================
# Driver
# =============================================================================


async def _mark(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: uuid.UUID,
    state: InboundEventState,
    *,
    error: str | None = None,
    route: ChannelRoute | None = None,
) -> None:
    async with session_factory() as s:
        values: dict[str, Any] = {"state": state, "last_error": error}
        if state in (InboundEventState.PROCESSED, InboundEventState.IGNORED):
            values["processed_at"] = utcnow()
        if route is not None:
            values["organization_id"] = route.organization_id
            values["inbox_id"] = route.inbox_id
        await s.execute(
            update(InboundWebhookEvent)
            .where(InboundWebhookEvent.id == event_id)
            .values(**values)
        )
        await s.commit()


async def process_event(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    http: httpx.AsyncClient,
    event_id: uuid.UUID,
) -> str:
    """Process one recorded webhook unit. Returns the outcome state."""
    async with session_factory() as s:
        event = await s.scalar(
            select(InboundWebhookEvent)
            .where(
                InboundWebhookEvent.id == event_id,
                InboundWebhookEvent.state.in_(
                    [InboundEventState.RECEIVED, InboundEventState.FAILED]
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return "skipped"
        event.state = InboundEventState.PROCESSING
        provider = event.provider.value
        payload = dict(event.payload)
        event_type = event.event_type
        attempts = event.attempts
        await s.commit()

    route: ChannelRoute | None = None
    try:
        if event_type == "template_status":
            async with session_factory() as s:
                await handle_template_status(s, payload.get("unit") or {})
            await _mark(session_factory, event_id, InboundEventState.PROCESSED)
            return "processed"
        route, ignored = await _process(
            session_factory, redis, http, event_id, provider, payload
        )
    except RequeueLaterError as exc:
        await _mark(
            session_factory,
            event_id,
            InboundEventState.RECEIVED,
            error=f"waiting: {exc.reason}",
        )
        await queue.enqueue(redis, queue.INBOUND, str(event_id), delay=exc.delay)
        return "requeued"
    except IgnoredError as exc:
        await _mark(
            session_factory,
            event_id,
            InboundEventState.IGNORED,
            error=exc.reason,
            route=route,
        )
        return "ignored"
    except Exception as exc:
        attempts += 1
        dead = attempts >= MAX_FAILURES
        async with session_factory() as s:
            await s.execute(
                update(InboundWebhookEvent)
                .where(InboundWebhookEvent.id == event_id)
                .values(
                    state=InboundEventState.DEAD if dead else InboundEventState.FAILED,
                    attempts=attempts,
                    last_error=repr(exc)[:1000],
                )
            )
            await s.commit()
        logger.error(
            "Inbound event failed",
            extra={
                "event_id": str(event_id),
                "provider": provider,
                "dead": dead,
                "error": repr(exc)[:300],
            },
        )
        if not dead:
            await queue.enqueue(
                redis, queue.INBOUND, str(event_id), delay=5.0 * 2**attempts
            )
        return "dead" if dead else "failed"
    if ignored:
        await _mark(
            session_factory,
            event_id,
            InboundEventState.IGNORED,
            error=",".join(ignored),
            route=route,
        )
        return "ignored"
    await _mark(session_factory, event_id, InboundEventState.PROCESSED, route=route)
    return "processed"


async def _process(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    http: httpx.AsyncClient,
    event_id: uuid.UUID,
    provider: str,
    payload: dict[str, Any],
) -> tuple[ChannelRoute, list[str]]:
    routing_id = str(payload.get("routing_id", ""))
    async with session_factory() as s:
        route = await s.get(ChannelRoute, (provider, routing_id))
        if route is None:
            raise IgnoredError("no_active_route")
        connection = await s.get(ChannelConnection, route.channel_connection_id)
        if connection is None or connection.status == ConnectionStatus.DISCONNECTED:
            raise IgnoredError("connection_disconnected")
        s.expunge(route)

    lock_key = f"lock:inbound:{route.inbox_id}:{payload.get('ordering_key', '')}"
    locked = await queue.acquire_lock(redis, lock_key, LOCK_TTL)
    if not locked:
        waits = await _bump(redis, f"inbound:lockwait:{event_id}")
        if waits <= LOCK_MAX_WAITS:
            raise RequeueLaterError(LOCK_RETRY_SECONDS, "ordering_lock_busy")
        logger.warning(
            "Processing without the ordering lock", extra={"event_id": str(event_id)}
        )
    try:
        async with session_factory() as s:
            await bind_tenant(s, route.organization_id)
            connection = await s.get(ChannelConnection, route.channel_connection_id)
            inbox = await s.get(Inbox, route.inbox_id)
            if connection is None or inbox is None or inbox.deleted_at is not None:
                raise IgnoredError("inbox_gone")
            adapter = await load_adapter(s, connection, http)
            events: list[InboundEvent] = adapter.parse_incoming(
                payload.get("unit") or {}
            )
            ctx = Ctx(
                session=s,
                redis=redis,
                inbox=inbox,
                connection=connection,
                adapter=adapter,
                event_id=event_id,
            )
            for item in events:
                await HANDLERS[item.kind](ctx, item)  # type: ignore[operator]
            await s.commit()
    finally:
        if locked:
            await queue.release_lock(redis, lock_key)

    for attachment_id in ctx.after.media:
        await queue.enqueue(redis, queue.MEDIA, str(attachment_id))
    for conv_id, message_id in ctx.after.ai_wake:
        try:
            await enqueue_incoming(redis, conv_id, message_id)
        except Exception as exc:
            logger.error("AI enqueue failed", extra={"error": repr(exc)[:200]})
    return route, ctx.ignored


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    older_than: timedelta = timedelta(minutes=1),
) -> int:
    """Re-enqueue ``received`` events a lost Redis entry stranded, and reset
    ``processing`` rows of a crashed worker (MESSAGE_PROCESSING.md §1.1)."""
    now = utcnow()
    async with session_factory() as s:
        await s.execute(
            update(InboundWebhookEvent)
            .where(
                InboundWebhookEvent.state == InboundEventState.PROCESSING,
                InboundWebhookEvent.received_at < now - timedelta(minutes=10),
            )
            .values(state=InboundEventState.RECEIVED)
        )
        ids = list(
            await s.scalars(
                select(InboundWebhookEvent.id)
                .where(
                    InboundWebhookEvent.state.in_(
                        [InboundEventState.RECEIVED, InboundEventState.FAILED]
                    ),
                    InboundWebhookEvent.received_at < now - older_than,
                )
                .limit(1000)
            )
        )
        await s.commit()
    for event_id in ids:
        # NX: don't pull a scheduled retry forward.
        await redis.zadd(
            queue.INBOUND, {str(event_id): datetime.now().timestamp()}, nx=True
        )
    return len(ids)
