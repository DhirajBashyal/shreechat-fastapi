"""Website widget inboxes: the admin side (docs/integrations/WEBSITE_CHAT.md §5, §7).

Create a widget inbox, change its settings, rotate the identity secret
(shown once, stored encrypted) and disconnect it. Needs ``inbox.manage``.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.web_widget import WebWidgetAdapter, new_identity_secret
from app.core.config import settings
from app.core.crypto import KEY_VERSION, encrypt_secret
from app.core.exceptions import NotFoundError, PlanLimitReachedError
from app.core.tenancy import TenantContext
from app.models import ChannelConnection, Inbox, Organization, WebWidget
from app.models.enums import ChannelProvider, ConnectionStatus
from app.schemas.widget import (
    WebWidgetCreate,
    WebWidgetCreated,
    WebWidgetRead,
    WebWidgetSettings,
)
from app.services.audit import audit
from app.services.limits import plan_info

INBOX_FIELDS = (
    "name",
    "greeting_enabled",
    "greeting_message",
    "working_hours_enabled",
    "working_hours",
    "timezone",
    "out_of_office_message",
    "allow_messages_after_resolved",
    "lock_to_single_conversation",
)
WIDGET_FIELDS = (
    "website_url",
    "allowed_origins",
    "widget_color",
    "welcome_title",
    "welcome_tagline",
    "reply_time",
    "pre_chat_form_enabled",
    "pre_chat_form_options",
    "identity_verification_required",
    "feature_flags",
)


def sdk_url() -> str:
    base = (settings.WIDGET_APP_URL or settings.FRONTEND_URL).rstrip("/")
    return f"{base}/widget/sdk.js"


def embed_script(website_token: str) -> str:
    return (
        "<script>\n"
        "  (function (d, s) {\n"
        "    var js = d.createElement(s); js.async = true;\n"
        f'    js.src = "{sdk_url()}";\n'
        "    js.onload = function () {\n"
        f'      window.converse.init({{ websiteToken: "{website_token}" }});\n'
        "    };\n"
        "    d.head.appendChild(js);\n"
        '  })(document, "script");\n'
        "</script>"
    )


def _read(
    inbox: Inbox, connection: ChannelConnection, widget: WebWidget
) -> WebWidgetRead:
    return WebWidgetRead(
        inbox_id=inbox.id,
        name=inbox.name,
        status=connection.status.value,
        website_token=widget.website_token,
        embed_script=embed_script(widget.website_token),
        website_url=widget.website_url,
        allowed_origins=list(widget.allowed_origins),
        widget_color=widget.widget_color,
        welcome_title=widget.welcome_title,
        welcome_tagline=widget.welcome_tagline,
        reply_time=widget.reply_time,
        pre_chat_form_enabled=widget.pre_chat_form_enabled,
        pre_chat_form_options=widget.pre_chat_form_options,
        identity_verification_required=widget.identity_verification_required,
        identity_secret_set=widget.identity_hmac_secret_ciphertext is not None,
        feature_flags=list(widget.feature_flags),
        greeting_enabled=inbox.greeting_enabled,
        greeting_message=inbox.greeting_message,
        working_hours_enabled=inbox.working_hours_enabled,
        working_hours=inbox.working_hours,
        timezone=inbox.timezone,
        out_of_office_message=inbox.out_of_office_message,
        allow_messages_after_resolved=inbox.allow_messages_after_resolved,
        lock_to_single_conversation=inbox.lock_to_single_conversation,
        created_at=inbox.created_at,
    )


# Columns that may be cleared with null; for the others null means "unchanged".
NULLABLE = {
    "greeting_message",
    "out_of_office_message",
    "website_url",
    "welcome_title",
    "welcome_tagline",
}


def _split(body: WebWidgetSettings) -> tuple[dict[str, Any], dict[str, Any]]:
    data = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None or k in NULLABLE
    }
    inbox = {k: v for k, v in data.items() if k in INBOX_FIELDS}
    widget = {k: v for k, v in data.items() if k in WIDGET_FIELDS}
    return inbox, widget


async def _check_inbox_limit(session: AsyncSession, organization_id: uuid.UUID) -> None:
    org = await session.get(Organization, organization_id)
    if org is None:
        return
    maximum = (await plan_info(session, org)).limits.get("inboxes")
    if maximum is None:
        return
    used = await session.scalar(
        select(func.count())
        .select_from(Inbox)
        .where(Inbox.organization_id == organization_id, Inbox.deleted_at.is_(None))
    )
    if (used or 0) >= int(maximum):
        raise PlanLimitReachedError("inboxes", int(maximum))


async def create(
    session: AsyncSession, ctx: TenantContext, body: WebWidgetCreate
) -> WebWidgetCreated:
    ws = ctx.require_workspace()
    await _check_inbox_limit(session, ctx.organization_id)
    inbox_fields, widget_fields = _split(body)
    inbox = Inbox(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ws,
        provider=ChannelProvider.WEB_WIDGET,
        **inbox_fields,
    )
    session.add(inbox)
    await session.flush()
    widget, connection, secret = await WebWidgetAdapter.create_connection(
        session, inbox, connected_by=ctx.principal.user_id, widget_fields=widget_fields
    )
    audit(
        session,
        action="inbox.created",
        resource_type="inbox",
        resource_id=inbox.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=ws,
        changes={"provider": "web_widget", "name": inbox.name},
    )
    await session.commit()
    for row in (inbox, connection, widget):
        await session.refresh(row)
    return WebWidgetCreated(
        **_read(inbox, connection, widget).model_dump(), identity_secret=secret
    )


async def _load(
    session: AsyncSession,
    ctx: TenantContext,
    inbox_id: uuid.UUID,
    *,
    lock: bool = False,
) -> tuple[Inbox, ChannelConnection, WebWidget]:
    query = select(Inbox).where(
        Inbox.id == inbox_id,
        Inbox.workspace_id == ctx.require_workspace(),
        Inbox.provider == ChannelProvider.WEB_WIDGET,
        Inbox.deleted_at.is_(None),
    )
    inbox = await session.scalar(query.with_for_update() if lock else query)
    connection = (
        await session.scalar(
            select(ChannelConnection).where(ChannelConnection.inbox_id == inbox_id)
        )
        if inbox
        else None
    )
    widget = (
        await session.scalar(
            select(WebWidget).where(WebWidget.channel_connection_id == connection.id)
        )
        if connection
        else None
    )
    if inbox is None or connection is None or widget is None:
        raise NotFoundError("Website widget not found")
    return inbox, connection, widget


async def get(
    session: AsyncSession, ctx: TenantContext, inbox_id: uuid.UUID
) -> WebWidgetRead:
    return _read(*await _load(session, ctx, inbox_id))


async def update(
    session: AsyncSession,
    ctx: TenantContext,
    inbox_id: uuid.UUID,
    body: WebWidgetSettings,
) -> WebWidgetRead:
    inbox, connection, widget = await _load(session, ctx, inbox_id, lock=True)
    inbox_fields, widget_fields = _split(body)
    for key, value in inbox_fields.items():
        setattr(inbox, key, value)
    for key, value in widget_fields.items():
        setattr(widget, key, value)
    if inbox_fields or widget_fields:
        audit(
            session,
            action="inbox.updated",
            resource_type="inbox",
            resource_id=inbox.id,
            actor_id=ctx.principal.user_id,
            organization_id=ctx.organization_id,
            workspace_id=inbox.workspace_id,
            changes={"fields": sorted({*inbox_fields, *widget_fields})},
        )
    await session.commit()
    for row in (inbox, widget):
        await session.refresh(row)
    return _read(inbox, connection, widget)


async def rotate_identity_secret(
    session: AsyncSession, ctx: TenantContext, inbox_id: uuid.UUID
) -> str:
    """New HMAC secret; identifier hashes made with the old one stop working."""
    inbox, _, widget = await _load(session, ctx, inbox_id, lock=True)
    secret = new_identity_secret()
    widget.identity_hmac_secret_ciphertext = encrypt_secret(secret)
    widget.identity_hmac_key_version = KEY_VERSION
    audit(
        session,
        action="inbox.identity_secret_rotated",
        resource_type="inbox",
        resource_id=inbox.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=inbox.workspace_id,
    )
    await session.commit()
    return secret


async def disconnect(
    session: AsyncSession, ctx: TenantContext, inbox_id: uuid.UUID
) -> None:
    inbox, connection, widget = await _load(session, ctx, inbox_id, lock=True)
    if connection.status == ConnectionStatus.DISCONNECTED:
        return
    await WebWidgetAdapter.remove_connection(session, widget, connection)
    audit(
        session,
        action="inbox.disconnected",
        resource_type="inbox",
        resource_id=inbox.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=inbox.workspace_id,
    )
    await session.commit()
