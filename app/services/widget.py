"""Website live chat: the visitor side (docs/integrations/WEBSITE_CHAT.md §3–§6).

Visitors are not users. A visitor is a ``contacts`` row (type ``visitor``) plus
a ``contact_identities`` row on the widget's inbox with a random
``source_id``. The visitor token is a JWT (typ ``widget_visitor``) naming
that identity, valid 30 days; it carries the identity's ``token_version``, so
``identify`` (which rotates it) and ``reset`` revoke older tokens.

The tenant is resolved from ``channel_routes`` (website_token → inbox), which
is readable before the tenant is known; a disconnected widget has no route
and answers 404.
"""

import fnmatch
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.working_hours import is_open
from app.core.config import settings
from app.core.crypto import SecretKeyMissingError, decrypt_secret
from app.core.exceptions import (
    APIError,
    ConflictError,
    NotFoundError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.core.security import decode_jwt, encode_jwt, utcnow
from app.core.tenancy import bind_tenant
from app.models import (
    AiAgent,
    AiAgentInbox,
    Attachment,
    ChannelConnection,
    ChannelRoute,
    Contact,
    ContactIdentity,
    Conversation,
    ConversationLabel,
    CustomAttributeDefinition,
    EventOutbox,
    Inbox,
    InboxMember,
    Label,
    Message,
    Organization,
    User,
    WebWidget,
)
from app.models.enums import (
    AttributeScope,
    AttributeValueType,
    ChannelProvider,
    ConnectionStatus,
    ConversationStatus,
    MessageDirection,
    OrgStatus,
    SenderKind,
)
from app.schemas.widget import (
    ContactUpdate,
    ConversationCreate,
    ConversationCreated,
    IdentifyRequest,
    MessageCreate,
    SessionResponse,
    WidgetConfig,
    WidgetContact,
    WidgetConversation,
    WidgetMessage,
    WidgetSender,
    WidgetSource,
)
from app.services import conversation_ai

TOKEN_TYPE = "widget_visitor"
VISIBLE_DIRECTIONS = (MessageDirection.INCOMING, MessageDirection.OUTGOING)
# Only these content_attributes reach the visitor (the rest is internal).
PUBLIC_CONTENT_ATTRIBUTES = ("items", "cards", "submitted_values")


class WidgetForbiddenError(APIError):
    status_code = 403
    code = "forbidden"
    message = "Not allowed"


class UploadsUnavailableError(APIError):
    status_code = 503
    code = "uploads_unavailable"
    message = "File uploads aren't available yet"


# =============================================================================
# Widget + visitor resolution
# =============================================================================


@dataclass
class WidgetContext:
    widget: WebWidget
    inbox: Inbox
    connection: ChannelConnection

    @property
    def organization_id(self) -> uuid.UUID:
        return self.inbox.organization_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.inbox.workspace_id


@dataclass
class Visitor:
    ctx: WidgetContext
    identity: ContactIdentity
    contact: Contact
    token: str
    expires_at: int


async def resolve_widget(session: AsyncSession, website_token: str) -> WidgetContext:
    route = await session.get(
        ChannelRoute, (ChannelProvider.WEB_WIDGET, website_token[:255])
    )
    if route is None:
        raise NotFoundError("Unknown website token", code="widget_not_found")
    await bind_tenant(session, route.organization_id)
    org = await session.get(Organization, route.organization_id)
    inbox = await session.get(Inbox, route.inbox_id)
    connection = await session.get(ChannelConnection, route.channel_connection_id)
    widget = await session.scalar(
        select(WebWidget).where(
            WebWidget.channel_connection_id == route.channel_connection_id
        )
    )
    if (
        org is None
        or org.status != OrgStatus.ACTIVE
        or org.deleted_at is not None
        or inbox is None
        or inbox.deleted_at is not None
        or connection is None
        or connection.status != ConnectionStatus.ACTIVE
        or widget is None
    ):
        raise NotFoundError("Unknown website token", code="widget_not_found")
    return WidgetContext(widget=widget, inbox=inbox, connection=connection)


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def trusted_origins() -> set[str]:
    """Our own widget app: requests from the iframe are always allowed."""
    return {
        o
        for o in (_origin(settings.WIDGET_APP_URL), _origin(settings.FRONTEND_URL))
        if o
    }


def _norm(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_allowed(
    widget: WebWidget, origin: str | None, parent_origin: str | None = None
) -> bool:
    """§8. ``origin`` is the browser's Origin header. When it is our own widget
    app (the iframe), the page that embeds the iframe is what matters: the
    iframe sends it as ``parent_origin`` (X-Parent-Origin / the socket's auth
    frame), taken from the browser-verified ``event.origin`` of the SDK's
    postMessage. No Origin (server-to-server) or no list configured: allowed."""
    if not widget.allowed_origins:
        return True
    if origin and _norm(origin) in trusted_origins():
        origin = parent_origin
    if not origin:
        return True
    o = _norm(origin)
    return any(
        fnmatch.fnmatchcase(o, pattern.lower()) for pattern in widget.allowed_origins
    )


def check_origin(
    widget: WebWidget, origin: str | None, parent_origin: str | None = None
) -> None:
    if not origin_allowed(widget, origin, parent_origin):
        raise WidgetForbiddenError(
            "This website isn't allowed to use the widget", code="origin_not_allowed"
        )


def create_visitor_token(identity: ContactIdentity) -> str:
    return encode_jwt(
        {
            "sub": str(identity.id),
            "typ": TOKEN_TYPE,
            "inb": str(identity.inbox_id),
            "tv": identity.token_version,
        },
        timedelta(days=settings.WIDGET_VISITOR_TOKEN_TTL_DAYS),
    )


async def resolve_visitor(
    session: AsyncSession, ctx: WidgetContext, token: str | None, *, lock: bool = False
) -> Visitor:
    if not token:
        raise UnauthenticatedError(
            "Visitor token required", code="visitor_token_required"
        )
    payload = decode_jwt(token, TOKEN_TYPE)
    invalid = UnauthenticatedError(
        "Invalid visitor token", code="invalid_visitor_token"
    )
    try:
        identity_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise invalid from exc
    if payload.get("inb") != str(ctx.inbox.id):
        raise invalid
    query = select(ContactIdentity).where(
        ContactIdentity.id == identity_id, ContactIdentity.inbox_id == ctx.inbox.id
    )
    identity = await session.scalar(query.with_for_update() if lock else query)
    if identity is None or identity.token_version != payload.get("tv"):
        raise invalid
    contact = await session.get(Contact, identity.contact_id)
    if contact is None or contact.deleted_at is not None:
        raise invalid
    return Visitor(
        ctx=ctx,
        identity=identity,
        contact=contact,
        token=token,
        expires_at=int(payload["exp"]),
    )


# =============================================================================
# Config
# =============================================================================


async def inbox_ai_agent(session: AsyncSession, inbox_id: uuid.UUID) -> AiAgent | None:
    """The active autonomous AI agent of the inbox, if any."""
    return await session.scalar(
        select(AiAgent)
        .join(AiAgentInbox, AiAgentInbox.ai_agent_id == AiAgent.id)
        .where(
            AiAgentInbox.inbox_id == inbox_id,
            AiAgentInbox.mode == "autonomous",
            AiAgent.is_active.is_(True),
            AiAgent.deleted_at.is_(None),
        )
    )


def _within_hours(inbox: Inbox) -> bool:
    if not inbox.working_hours_enabled:
        return True
    return is_open(inbox.working_hours, inbox.timezone, utcnow())


async def _agents_online(
    session: AsyncSession, redis: Redis | None, inbox: Inbox
) -> bool:
    """Presence lives in Redis (REALTIME_COMMUNICATION.md §8): any inbox member
    with an open dashboard socket. False when presence isn't available."""
    if redis is None:
        return False
    try:
        online = await redis.hgetall(f"presence:org:{inbox.organization_id}")  # type: ignore[misc]
    except Exception:
        return False
    ids = {
        (k.decode() if isinstance(k, bytes) else str(k))
        for k, v in online.items()
        if int(v or 0) > 0
    }
    if not ids:
        return False
    members = await session.scalars(
        select(InboxMember.user_id).where(InboxMember.inbox_id == inbox.id)
    )
    return any(str(m) in ids for m in members)


async def get_config(
    session: AsyncSession, ctx: WidgetContext, redis: Redis | None
) -> WidgetConfig:
    w, inbox = ctx.widget, ctx.inbox
    return WidgetConfig(
        website_token=w.website_token,
        inbox_name=inbox.name,
        widget_color=w.widget_color,
        welcome_title=w.welcome_title,
        welcome_tagline=w.welcome_tagline,
        reply_time=w.reply_time,
        pre_chat_form_enabled=w.pre_chat_form_enabled,
        pre_chat_form_options=w.pre_chat_form_options,
        feature_flags=list(w.feature_flags),
        identity_verification_required=w.identity_verification_required,
        working_hours_enabled=inbox.working_hours_enabled,
        working_hours=inbox.working_hours,
        timezone=inbox.timezone,
        is_online=_within_hours(inbox),
        out_of_office_message=inbox.out_of_office_message,
        agents_online=await _agents_online(session, redis, inbox),
        ai_assistant=await inbox_ai_agent(session, inbox.id) is not None,
    )


# =============================================================================
# Sessions, identify, contact
# =============================================================================


def _contact_read(visitor_contact: Contact, identity: ContactIdentity) -> WidgetContact:
    extra = visitor_contact.additional_attributes or {}
    return WidgetContact(
        name=visitor_contact.name,
        email=visitor_contact.email or extra.get("unverified_email"),
        phone_number=visitor_contact.phone_number,
        identity_verified=identity.identity_verified,
    )


async def create_session(
    session: AsyncSession, ctx: WidgetContext, visitor_token: str | None
) -> SessionResponse:
    """Resume a valid token, or create an anonymous visitor."""
    if visitor_token:
        try:
            v = await resolve_visitor(session, ctx, visitor_token)
        except APIError:
            pass  # expired / revoked / other inbox: start a new anonymous session
        else:
            return SessionResponse(
                visitor_token=visitor_token,
                realtime_token=v.identity.realtime_token,
                resumed=True,
                contact=_contact_read(v.contact, v.identity),
            )
    contact = Contact(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ctx.workspace_id,
        name="",
        contact_type="visitor",
        last_activity_at=utcnow(),
    )
    session.add(contact)
    await session.flush()
    identity = ContactIdentity(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ctx.workspace_id,
        contact_id=contact.id,
        inbox_id=ctx.inbox.id,
        source_id=uuid.uuid4().hex,
        realtime_token=secrets.token_urlsafe(32),
        identity_verified=False,
        token_version=0,
    )
    session.add(identity)
    _outbox(session, ctx, "contact.created", "contact", contact.id, {})
    await session.commit()
    return SessionResponse(
        visitor_token=create_visitor_token(identity),
        realtime_token=identity.realtime_token,
        resumed=False,
        contact=_contact_read(contact, identity),
    )


def _expected_hash(ctx: WidgetContext, identifier: str) -> str:
    ciphertext = ctx.widget.identity_hmac_secret_ciphertext
    if not ciphertext:
        raise ValidationFailedError(
            "Identity verification isn't set up for this widget",
            code="identity_verification_not_configured",
        )
    try:
        secret = decrypt_secret(ciphertext)
    except SecretKeyMissingError as exc:
        raise ValidationFailedError(
            "Identity verification isn't available",
            code="identity_verification_not_configured",
        ) from exc
    return hmac.new(secret.encode(), identifier.encode(), hashlib.sha256).hexdigest()


async def _email_taken(
    session: AsyncSession, ctx: WidgetContext, email: str, contact_id: uuid.UUID
) -> bool:
    other = await session.scalar(
        select(Contact.id).where(
            Contact.workspace_id == ctx.workspace_id,
            func.lower(Contact.email) == email.lower(),
            Contact.deleted_at.is_(None),
            Contact.id != contact_id,
        )
    )
    return other is not None


async def _apply_contact_fields(
    session: AsyncSession,
    ctx: WidgetContext,
    contact: Contact,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> None:
    """Never takes over another contact's email: an unverified visitor typing
    someone else's address keeps it only as ``unverified_email`` (no merge)."""
    if name:
        contact.name = name.strip()[:255]
    if phone:
        contact.phone_number = phone.strip()[:32]
    if email:
        email = email.strip().lower()
        if await _email_taken(session, ctx, email, contact.id):
            contact.additional_attributes = {
                **(contact.additional_attributes or {}),
                "unverified_email": email,
            }
        else:
            contact.email = email
        if contact.contact_type == "visitor":
            contact.contact_type = "lead"


async def identify(
    session: AsyncSession, ctx: WidgetContext, token: str | None, body: IdentifyRequest
) -> SessionResponse:
    """§5. Verified (valid identifier_hash): attach to the contact with that
    identifier, merging this visitor into it. Unverified: update this
    contact's fields only, never merge. The token is rotated either way."""
    v = await resolve_visitor(session, ctx, token, lock=True)
    identity, contact = v.identity, v.contact
    verified = False
    if body.identifier_hash:
        expected = _expected_hash(ctx, body.identifier)
        if not hmac.compare_digest(expected, body.identifier_hash.lower()):
            raise WidgetForbiddenError(
                "identifier_hash doesn't match", code="invalid_identifier_hash"
            )
        verified = True
    elif ctx.widget.identity_verification_required:
        raise WidgetForbiddenError(
            "This widget requires a verified identity (identifier_hash)",
            code="identity_verification_required",
        )

    if verified:
        if contact.identifier and contact.identifier != body.identifier:
            raise ConflictError(
                "This visitor is identified as someone else; call reset() first",
                code="reset_required",
            )
        existing = await session.scalar(
            select(Contact)
            .where(
                Contact.workspace_id == ctx.workspace_id,
                Contact.identifier == body.identifier,
                Contact.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if existing is not None and existing.id != contact.id:
            await _merge_into(session, ctx, source=contact, target=existing)
            contact = existing
        else:
            contact.identifier = body.identifier
        contact.contact_type = "customer"
        identity.identity_verified = True
    else:
        contact.additional_attributes = {
            **(contact.additional_attributes or {}),
            "unverified_identifier": body.identifier,
        }

    await _apply_contact_fields(
        session, ctx, contact, name=body.name, email=body.email, phone=body.phone
    )
    if body.avatar_url:
        contact.additional_attributes = {
            **(contact.additional_attributes or {}),
            "avatar_url": body.avatar_url,
        }
    contact.last_activity_at = utcnow()
    identity.token_version += 1
    _outbox(
        session, ctx, "contact.updated", "contact", contact.id, {"verified": verified}
    )
    await session.commit()
    return SessionResponse(
        visitor_token=create_visitor_token(identity),
        realtime_token=identity.realtime_token,
        resumed=False,
        contact=_contact_read(contact, identity),
    )


async def _merge_into(
    session: AsyncSession, ctx: WidgetContext, *, source: Contact, target: Contact
) -> None:
    """Move the visitor's identities and conversations to ``target`` and
    delete the (now empty) visitor contact. Only after HMAC verification."""
    ws = ctx.workspace_id
    await session.execute(
        update(ContactIdentity)
        .where(
            ContactIdentity.workspace_id == ws, ContactIdentity.contact_id == source.id
        )
        .values(contact_id=target.id)
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.workspace_id == ws, Conversation.contact_id == source.id)
        .values(contact_id=target.id)
    )
    await session.execute(
        update(Message)
        .where(Message.workspace_id == ws, Message.sender_contact_id == source.id)
        .values(sender_contact_id=target.id)
    )
    for key in ("name", "phone_number"):
        if not getattr(target, key) and getattr(source, key):
            setattr(target, key, getattr(source, key))
    target.custom_attributes = {
        **(source.custom_attributes or {}),
        **(target.custom_attributes or {}),
    }
    source.deleted_at = utcnow()
    source.email = None
    await session.flush()
    _outbox(
        session,
        ctx,
        "contact.merged",
        "contact",
        target.id,
        {"merged_contact_id": str(source.id)},
    )


async def update_contact(
    session: AsyncSession, ctx: WidgetContext, token: str | None, body: ContactUpdate
) -> WidgetContact:
    v = await resolve_visitor(session, ctx, token, lock=True)
    await _apply_contact_fields(
        session, ctx, v.contact, name=body.name, email=body.email, phone=body.phone
    )
    if body.custom_attributes is not None:
        v.contact.custom_attributes = await merge_attributes(
            session,
            ctx,
            AttributeScope.CONTACT,
            v.contact.custom_attributes,
            body.custom_attributes,
        )
    v.contact.last_activity_at = utcnow()
    _outbox(session, ctx, "contact.updated", "contact", v.contact.id, {})
    await session.commit()
    return _contact_read(v.contact, v.identity)


async def delete_contact_attribute(
    session: AsyncSession, ctx: WidgetContext, token: str | None, key: str
) -> WidgetContact:
    v = await resolve_visitor(session, ctx, token, lock=True)
    attrs = dict(v.contact.custom_attributes or {})
    if key in attrs:
        attrs.pop(key)
        v.contact.custom_attributes = attrs
        _outbox(session, ctx, "contact.updated", "contact", v.contact.id, {})
        await session.commit()
    return _contact_read(v.contact, v.identity)


async def reset(session: AsyncSession, ctx: WidgetContext, token: str | None) -> None:
    """converse.reset(): revoke this token. The SDK then starts a new session."""
    v = await resolve_visitor(session, ctx, token, lock=True)
    v.identity.token_version += 1
    await session.commit()


# =============================================================================
# Custom attributes
# =============================================================================


def _check_value(defn: CustomAttributeDefinition, value: Any) -> Any:
    t = defn.value_type
    bad = ValidationFailedError(
        f"Invalid value for {defn.attribute_key} ({t.value})",
        code="invalid_attribute_value",
        extra={"attribute": defn.attribute_key},
    )
    if t in (
        AttributeValueType.NUMBER,
        AttributeValueType.CURRENCY,
        AttributeValueType.PERCENT,
    ):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise bad
    elif t == AttributeValueType.CHECKBOX:
        if not isinstance(value, bool):
            raise bad
    elif t == AttributeValueType.LIST:
        if value not in defn.list_values:
            raise bad
    elif t == AttributeValueType.DATE:
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
            raise bad
    elif t == AttributeValueType.LINK:
        if not isinstance(value, str) or not re.match(r"^https?://", value):
            raise bad
    else:
        if not isinstance(value, str) or len(value) > 2000:
            raise bad
        if defn.regex_pattern and not re.fullmatch(defn.regex_pattern, value):
            raise bad
    return value


async def merge_attributes(
    session: AsyncSession,
    ctx: WidgetContext,
    scope: AttributeScope,
    current: dict[str, Any] | None,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Validate against the workspace's definitions. ``None`` removes a key."""
    defs = {
        d.attribute_key: d
        for d in await session.scalars(
            select(CustomAttributeDefinition).where(
                CustomAttributeDefinition.workspace_id == ctx.workspace_id,
                CustomAttributeDefinition.scope == scope,
            )
        )
    }
    out = dict(current or {})
    for key, value in changes.items():
        defn = defs.get(key)
        if defn is None:
            raise ValidationFailedError(
                f"Unknown {scope.value} attribute: {key}",
                code="unknown_attribute",
                extra={"attribute": key},
            )
        if value is None:
            out.pop(key, None)
        else:
            out[key] = _check_value(defn, value)
    return out


# =============================================================================
# Serialisation
# =============================================================================


async def serialize_messages(
    session: AsyncSession, messages: list[Message]
) -> list[WidgetMessage]:
    user_ids = {m.sender_user_id for m in messages if m.sender_user_id}
    agent_ids = {m.sender_ai_agent_id for m in messages if m.sender_ai_agent_id}
    users = (
        {
            u.id: u
            for u in await session.scalars(select(User).where(User.id.in_(user_ids)))
        }
        if user_ids
        else {}
    )
    agents = (
        {
            a.id: a
            for a in await session.scalars(
                select(AiAgent).where(AiAgent.id.in_(agent_ids))
            )
        }
        if agent_ids
        else {}
    )
    files: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if messages:
        for a in await session.scalars(
            select(Attachment).where(
                Attachment.message_id.in_([m.id for m in messages])
            )
        ):
            files.setdefault(a.message_id, []).append(
                {
                    "id": str(a.id),
                    "kind": a.kind.value,
                    "title": a.fallback_title,
                    "url": a.external_url,
                }
            )
    out: list[WidgetMessage] = []
    for m in messages:
        name: str | None = None
        sources: list[WidgetSource] = []
        if m.sender_kind == SenderKind.USER and m.sender_user_id in users:
            name = users[m.sender_user_id].name
        elif m.sender_kind == SenderKind.AI_AGENT and m.sender_ai_agent_id in agents:
            agent = agents[m.sender_ai_agent_id]
            name = (agent.persona or {}).get("name") or agent.name
            if (agent.guardrails or {}).get("show_sources"):
                citations = (
                    (m.content_attributes or {}).get("ai", {}).get("citations", [])
                )
                sources = [
                    WidgetSource(title=c.get("title", ""), url=c.get("url"))
                    for c in citations
                    if isinstance(c, dict)
                ]
        out.append(
            WidgetMessage(
                id=m.id,
                conversation_id=m.conversation_id,
                direction=m.direction.value,
                content_type=m.content_type.value,
                content=m.content,
                content_attributes={
                    k: v
                    for k, v in (m.content_attributes or {}).items()
                    if k in PUBLIC_CONTENT_ATTRIBUTES
                },
                sender=WidgetSender(kind=m.sender_kind.value, name=name),
                client_message_id=m.client_message_id,
                sequence_number=m.sequence_number,
                delivery_status=m.delivery_status.value,
                sources=sources,
                attachments=files.get(m.id, []),
                created_at=m.created_at,
            )
        )
    return out


def _visible_messages(conversation_id: uuid.UUID) -> Select[tuple[Message]]:
    return select(Message).where(
        Message.conversation_id == conversation_id,
        Message.is_private.is_(False),
        Message.deleted_at.is_(None),
        Message.direction.in_(VISIBLE_DIRECTIONS),
    )


async def _conversation_read(
    session: AsyncSession, conv: Conversation
) -> WidgetConversation:
    last = await session.scalar(
        _visible_messages(conv.id).order_by(Message.sequence_number.desc()).limit(1)
    )
    unread_q = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conv.id,
            Message.direction == MessageDirection.OUTGOING,
            Message.is_private.is_(False),
            Message.deleted_at.is_(None),
        )
    )
    if conv.contact_last_seen_at is not None:
        unread_q = unread_q.where(Message.created_at > conv.contact_last_seen_at)
    labels = list(
        await session.scalars(
            select(Label.title)
            .join(ConversationLabel, ConversationLabel.label_id == Label.id)
            .where(
                ConversationLabel.conversation_id == conv.id,
                Label.widget_settable.is_(True),
            )
            .order_by(Label.title)
        )
    )
    return WidgetConversation(
        id=conv.id,
        display_number=conv.display_number,
        status=conv.status.value,
        created_at=conv.created_at,
        last_activity_at=conv.last_activity_at,
        contact_last_seen_at=conv.contact_last_seen_at,
        agent_last_seen_at=conv.agent_last_seen_at,
        unread_count=int(await session.scalar(unread_q) or 0),
        custom_attributes=conv.custom_attributes or {},
        labels=labels,
        last_message=(await serialize_messages(session, [last]))[0] if last else None,
    )


# =============================================================================
# Conversations and messages
# =============================================================================


def _outbox(
    session: AsyncSession,
    ctx: WidgetContext,
    event: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    session.add(
        EventOutbox(
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            event_name=event,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload={f"{aggregate_type}_id": str(aggregate_id), **payload},
        )
    )


async def _own_conversation(
    session: AsyncSession, v: Visitor, conversation_id: uuid.UUID, *, lock: bool = False
) -> Conversation:
    """The visitor's contact in this inbox (all its identities after a merge)."""
    query = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.inbox_id == v.ctx.inbox.id,
        Conversation.contact_id == v.contact.id,
    )
    conv = await session.scalar(query.with_for_update() if lock else query)
    if conv is None:
        raise NotFoundError("Conversation not found")
    return conv


async def list_conversations(
    session: AsyncSession, ctx: WidgetContext, token: str | None
) -> list[WidgetConversation]:
    v = await resolve_visitor(session, ctx, token)
    convs = await session.scalars(
        select(Conversation)
        .where(
            Conversation.inbox_id == ctx.inbox.id,
            Conversation.contact_id == v.contact.id,
        )
        .order_by(Conversation.last_activity_at.desc())
        .limit(25)
    )
    return [await _conversation_read(session, c) for c in convs]


def _browser(user_agent: str) -> dict[str, str]:
    ua = user_agent or ""
    name, version = "Unknown", ""
    for label, pattern in (
        ("Edge", r"Edg/([\d.]+)"),
        ("Opera", r"OPR/([\d.]+)"),
        ("Chrome", r"Chrome/([\d.]+)"),
        ("Firefox", r"Firefox/([\d.]+)"),
        ("Safari", r"Version/([\d.]+).*Safari"),
    ):
        m = re.search(pattern, ua)
        if m:
            name, version = label, m.group(1)
            break
    platform = next(
        (
            p
            for p, needle in (
                ("Android", "Android"),
                ("iOS", "iPhone"),
                ("iOS", "iPad"),
                ("Windows", "Windows"),
                ("macOS", "Mac OS X"),
                ("Linux", "Linux"),
            )
            if needle in ua
        ),
        "Unknown",
    )
    return {"name": name, "version": version, "platform": platform}


async def _add_incoming(
    session: AsyncSession,
    v: Visitor,
    conv: Conversation,
    *,
    content: str,
    client_message_id: str | None,
    greeting: str | None = None,
) -> list[Message]:
    """The visitor's message (+ greeting on a new conversation, + out-of-office
    reply). Caller holds the row lock."""
    ctx, now = v.ctx, utcnow()
    added: list[Message] = []
    if conv.status == ConversationStatus.RESOLVED:
        if not ctx.inbox.allow_messages_after_resolved:
            raise ConflictError(
                "This conversation has ended", code="conversation_resolved"
            )
        _set_status(
            session,
            ctx,
            conv,
            ConversationStatus.PENDING if conv.ai_active else ConversationStatus.OPEN,
        )
    elif conv.status == ConversationStatus.SNOOZED:
        conv.snoozed_until = None
        _set_status(session, ctx, conv, ConversationStatus.OPEN)

    msg = Message(
        id=uuid.uuid4(),
        organization_id=conv.organization_id,
        workspace_id=conv.workspace_id,
        inbox_id=conv.inbox_id,
        conversation_id=conv.id,
        direction=MessageDirection.INCOMING,
        content=content,
        sender_kind=SenderKind.CONTACT,
        sender_contact_id=v.contact.id,
        client_message_id=client_message_id,
    )
    session.add(msg)
    added.append(msg)
    conv.last_activity_at = now
    conv.last_incoming_at = now
    conv.waiting_since = conv.waiting_since or now
    v.contact.last_activity_at = now
    _outbox(
        session,
        ctx,
        "message.created",
        "conversation",
        conv.id,
        {"message_id": str(msg.id), "private": False},
    )
    if greeting:
        added.append(
            conversation_ai.add_message(
                session,
                conv,
                content=greeting,
                sender_kind=SenderKind.SYSTEM,
                content_attributes={"auto_reply": "greeting"},
            )
        )
    # Out of office: once per conversation, and not when the AI answers
    # (its handoff policy decides what happens outside hours).
    attrs = conv.additional_attributes or {}
    if (
        not conv.ai_active
        and ctx.inbox.out_of_office_message
        and not _within_hours(ctx.inbox)
        and not attrs.get("out_of_office_sent")
    ):
        added.append(
            conversation_ai.add_message(
                session,
                conv,
                content=ctx.inbox.out_of_office_message,
                sender_kind=SenderKind.SYSTEM,
                content_attributes={"auto_reply": "out_of_office"},
            )
        )
        conv.additional_attributes = {**attrs, "out_of_office_sent": True}
    return added


def _set_status(
    session: AsyncSession,
    ctx: WidgetContext,
    conv: Conversation,
    status: ConversationStatus,
) -> None:
    previous = conv.status
    conv.status = status
    conv.status_changed_at = utcnow()
    _outbox(
        session,
        ctx,
        "conversation.status_changed",
        "conversation",
        conv.id,
        {"status": status.value, "previous_status": previous.value},
    )


async def create_conversation(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    body: ConversationCreate,
    *,
    user_agent: str,
    accept_language: str | None,
) -> tuple[ConversationCreated, bool, Conversation, uuid.UUID | None]:
    """§6. Returns (response, conversation created?, conversation, id of the
    new incoming message or None for a retry)."""
    v = await resolve_visitor(session, ctx, token, lock=True)
    if v.contact.blocked:
        raise WidgetForbiddenError("You can't send messages", code="contact_blocked")

    # Retry of the same first message: same conversation, 200.
    if body.client_message_id:
        earlier = await session.scalar(
            select(Message).where(
                Message.inbox_id == ctx.inbox.id,
                Message.sender_contact_id == v.contact.id,
                Message.client_message_id == body.client_message_id,
            )
        )
        if earlier is not None:
            conv = await _own_conversation(session, v, earlier.conversation_id)
            return (
                ConversationCreated(
                    conversation=await _conversation_read(session, conv),
                    messages=await serialize_messages(session, [earlier]),
                ),
                False,
                conv,
                None,
            )

    if body.contact is not None:
        await _apply_contact_fields(
            session,
            ctx,
            v.contact,
            name=body.contact.name,
            email=body.contact.email,
            phone=body.contact.phone,
        )
        if body.contact.custom_attributes:
            v.contact.custom_attributes = await merge_attributes(
                session,
                ctx,
                AttributeScope.CONTACT,
                v.contact.custom_attributes,
                body.contact.custom_attributes,
            )
    _check_pre_chat(ctx, v.contact)

    if ctx.inbox.lock_to_single_conversation:
        open_conv = await session.scalar(
            select(Conversation)
            .where(
                Conversation.inbox_id == ctx.inbox.id,
                Conversation.contact_id == v.contact.id,
                Conversation.status != ConversationStatus.RESOLVED,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if open_conv is not None:
            added = await _add_incoming(
                session,
                v,
                open_conv,
                content=body.content,
                client_message_id=body.client_message_id,
            )
            await session.commit()
            for m in added:
                await session.refresh(m)
            return (
                ConversationCreated(
                    conversation=await _conversation_read(session, open_conv),
                    messages=await serialize_messages(session, added),
                ),
                False,
                open_conv,
                added[0].id,
            )

    custom = (
        await merge_attributes(
            session, ctx, AttributeScope.CONVERSATION, {}, body.custom_attributes
        )
        if body.custom_attributes
        else {}
    )
    agent = await inbox_ai_agent(session, ctx.inbox.id)
    number = await session.scalar(
        select(func.next_workspace_counter(ctx.workspace_id, "conversation"))
    )
    language = body.browser_language or (accept_language or "").split(",")[0].strip()
    conv = Conversation(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ctx.workspace_id,
        inbox_id=ctx.inbox.id,
        contact_id=v.contact.id,
        contact_identity_id=v.identity.id,
        display_number=int(number or 0),
        status=ConversationStatus.PENDING if agent else ConversationStatus.OPEN,
        ai_agent_id=agent.id if agent else None,
        ai_active=agent is not None,
        additional_attributes={
            "referer_url": body.referer_url,
            "browser": _browser(user_agent),
            "browser_language": language[:35] or None,
            "initiated_at": utcnow().isoformat(),
        },
        custom_attributes=custom,
    )
    session.add(conv)
    await session.flush()
    _outbox(
        session,
        ctx,
        "conversation.created",
        "conversation",
        conv.id,
        {"status": conv.status.value, "ai_active": conv.ai_active},
    )
    added = await _add_incoming(
        session,
        v,
        conv,
        content=body.content,
        client_message_id=body.client_message_id,
        greeting=ctx.inbox.greeting_message if ctx.inbox.greeting_enabled else None,
    )
    await session.commit()
    for m in added:
        await session.refresh(m)
    return (
        ConversationCreated(
            conversation=await _conversation_read(session, conv),
            messages=await serialize_messages(session, added),
        ),
        True,
        conv,
        added[0].id,
    )


def _check_pre_chat(ctx: WidgetContext, contact: Contact) -> None:
    if not ctx.widget.pre_chat_form_enabled:
        return
    opts = ctx.widget.pre_chat_form_options or {}
    extra = contact.additional_attributes or {}
    missing = [
        field
        for field, required, present in (
            ("name", opts.get("require_name"), bool(contact.name)),
            (
                "email",
                opts.get("require_email"),
                bool(contact.email or extra.get("unverified_email")),
            ),
            ("phone", opts.get("require_phone"), bool(contact.phone_number)),
        )
        if required and not present
    ]
    if missing:
        raise ValidationFailedError(
            f"Please fill in: {', '.join(missing)}",
            code="pre_chat_required",
            extra={"fields": missing},
        )


async def list_messages(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
    *,
    before: int | None,
    after: int | None,
    limit: int,
) -> list[WidgetMessage]:
    """Oldest first. ``before``: older page (scroll up). ``after``: what was
    missed while disconnected."""
    v = await resolve_visitor(session, ctx, token)
    conv = await _own_conversation(session, v, conversation_id)
    query = _visible_messages(conv.id)
    if after is not None:
        rows = list(
            await session.scalars(
                query.where(Message.sequence_number > after)
                .order_by(Message.sequence_number)
                .limit(limit)
            )
        )
    else:
        if before is not None:
            query = query.where(Message.sequence_number < before)
        rows = list(
            await session.scalars(
                query.order_by(Message.sequence_number.desc()).limit(limit)
            )
        )
        rows.reverse()
    return await serialize_messages(session, rows)


async def send_message(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
    body: MessageCreate,
) -> tuple[WidgetMessage, bool, Conversation]:
    """Returns (message, created?, conversation). Same client_message_id → the
    same message (created = False)."""
    if body.attachment_file_ids:
        raise ValidationFailedError(
            "Attachments aren't available yet", code="attachments_not_supported"
        )
    v = await resolve_visitor(session, ctx, token)
    if v.contact.blocked:
        raise WidgetForbiddenError("You can't send messages", code="contact_blocked")
    conv = await _own_conversation(session, v, conversation_id, lock=True)

    async def existing() -> Message | None:
        return await session.scalar(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.client_message_id == body.client_message_id,
            )
        )

    found = await existing()
    if found is not None:
        return (await serialize_messages(session, [found]))[0], False, conv
    try:
        async with session.begin_nested():
            added = await _add_incoming(
                session,
                v,
                conv,
                content=body.content,
                client_message_id=body.client_message_id,
            )
    except IntegrityError:
        found = await existing()
        if found is None:
            raise
        return (await serialize_messages(session, [found]))[0], False, conv
    await session.commit()
    await session.refresh(added[0])
    return (await serialize_messages(session, [added[0]]))[0], True, conv


async def mark_seen(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
) -> WidgetConversation:
    v = await resolve_visitor(session, ctx, token)
    conv = await _own_conversation(session, v, conversation_id, lock=True)
    now = utcnow()
    conv.contact_last_seen_at = now
    await session.execute(
        update(Message)
        .where(
            Message.conversation_id == conv.id,
            Message.direction == MessageDirection.OUTGOING,
            Message.is_private.is_(False),
            Message.delivery_status.in_(["sent", "delivered"]),
            Message.created_at <= now,
        )
        .values(delivery_status="read")
    )
    _outbox(
        session,
        ctx,
        "conversation.contact_seen",
        "conversation",
        conv.id,
        {"contact_last_seen_at": now.isoformat()},
    )
    await session.commit()
    return await _conversation_read(session, conv)


async def resolve_conversation(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
) -> WidgetConversation:
    if "end_conversation" not in ctx.widget.feature_flags:
        raise WidgetForbiddenError(
            "Ending the chat is turned off for this widget", code="feature_disabled"
        )
    v = await resolve_visitor(session, ctx, token)
    conv = await _own_conversation(session, v, conversation_id, lock=True)
    if conv.status != ConversationStatus.RESOLVED:
        _set_status(session, ctx, conv, ConversationStatus.RESOLVED)
        conv.waiting_since = None
        conv.snoozed_until = None
        conversation_ai.add_activity(
            session, conv, "Conversation was ended by the contact"
        )
        await session.commit()
    return await _conversation_read(session, conv)


async def update_conversation_attributes(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
    changes: dict[str, Any],
) -> WidgetConversation:
    v = await resolve_visitor(session, ctx, token)
    conv = await _own_conversation(session, v, conversation_id, lock=True)
    conv.custom_attributes = await merge_attributes(
        session, ctx, AttributeScope.CONVERSATION, conv.custom_attributes, changes
    )
    _outbox(
        session,
        ctx,
        "conversation.updated",
        "conversation",
        conv.id,
        {"changes": ["custom_attributes"]},
    )
    await session.commit()
    return await _conversation_read(session, conv)


async def _settable_label(
    session: AsyncSession, ctx: WidgetContext, title: str
) -> Label:
    label = await session.scalar(
        select(Label).where(
            Label.workspace_id == ctx.workspace_id,
            Label.title == title.strip().lower(),
            Label.widget_settable.is_(True),
        )
    )
    if label is None:
        raise ValidationFailedError(
            "This label can't be set from the widget", code="label_not_allowed"
        )
    return label


async def set_label(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
    title: str,
    *,
    add: bool,
) -> WidgetConversation:
    v = await resolve_visitor(session, ctx, token)
    conv = await _own_conversation(session, v, conversation_id, lock=True)
    label = await _settable_label(session, ctx, title)
    link = await session.get(ConversationLabel, (conv.id, label.id))
    if add and link is None:
        session.add(
            ConversationLabel(
                organization_id=conv.organization_id,
                workspace_id=conv.workspace_id,
                conversation_id=conv.id,
                label_id=label.id,
            )
        )
    elif not add and link is not None:
        await session.delete(link)
    else:
        return await _conversation_read(session, conv)
    _outbox(
        session,
        ctx,
        "conversation.updated",
        "conversation",
        conv.id,
        {"changes": ["labels"]},
    )
    await session.commit()
    return await _conversation_read(session, conv)


async def typing_conversation(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    conversation_id: uuid.UUID,
) -> tuple[Visitor, Conversation]:
    v = await resolve_visitor(session, ctx, token)
    return v, await _own_conversation(session, v, conversation_id)


async def record_event(
    session: AsyncSession,
    ctx: WidgetContext,
    token: str | None,
    name: str,
    url: str | None,
    properties: dict[str, Any],
) -> None:
    """Page views / custom events for automation triggers (Phase 7)."""
    v = await resolve_visitor(session, ctx, token)
    _outbox(
        session,
        ctx,
        "widget.event",
        "contact",
        v.contact.id,
        {
            "name": name,
            "url": url,
            "properties": properties,
            "inbox_id": str(ctx.inbox.id),
        },
    )
    v.contact.last_activity_at = utcnow()
    await session.commit()
