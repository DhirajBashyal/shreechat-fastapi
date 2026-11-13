"""Connecting Meta channels: WhatsApp (manual, Embedded Signup), Messenger and
Instagram (OAuth), disconnect, health, templates, business profile, Meta
user callbacks. docs/backend/API_DESIGN.md §7–11, docs/integrations/*.

Provider calls go through the adapters' static helpers; this module owns the
database rows: inbox, channel_connections (encrypted secrets),
channel_routes, and the provider detail tables.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelError, ReauthorizationRequiredError
from app.channels.meta.common import oauth
from app.channels.meta.common.graph import GraphClient
from app.channels.meta.instagram.adapter import InstagramAdapter
from app.channels.meta.messenger.adapter import MessengerAdapter
from app.channels.meta.whatsapp.adapter import WhatsAppCloudAdapter
from app.channels.meta.whatsapp.templates import placeholders
from app.channels.registry import (
    META_PROVIDERS,
    decrypt_json,
    encrypt_json,
    load_adapter,
)
from app.channels.types import ConnectResult
from app.core.config import settings
from app.core.exceptions import (
    APIError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.security import utcnow
from app.core.tenancy import TenantContext
from app.models import (
    ChannelConnection,
    ChannelRoute,
    Contact,
    ContactIdentity,
    EventOutbox,
    FacebookPage,
    Inbox,
    InstagramAccount,
    Message,
    WhatsappBusinessAccount,
    WhatsappMessageTemplate,
    WhatsappPhoneNumber,
)
from app.models.enums import ChannelProvider, ConnectionStatus, MessageDirection
from app.schemas.channels import (
    BusinessProfile,
    BusinessProfileUpdate,
    ConnectionRead,
    HealthResponse,
    InboxSummary,
    InstagramConnected,
    InstagramSelection,
    MessengerConnected,
    PageOption,
    ProviderInfo,
    TemplateRead,
    WhatsAppConnected,
)
from app.services.access import can_access_workspace, resolve_membership
from app.services.audit import audit
from app.services.web_widgets import _check_inbox_limit


class ProviderError(APIError):
    status_code = 502
    code = "provider_error"
    message = "The provider returned an error"


class InvalidCredentialsError(APIError):
    status_code = 422
    code = "invalid_credentials"
    message = "The provider rejected these credentials"


class ChannelNotConfiguredError(APIError):
    status_code = 503
    code = "channel_not_configured"
    message = "This channel isn't configured on the server"


def provider_failure(exc: ChannelError) -> APIError:
    """Map an adapter error during setup to an API error."""
    if exc.code in ("number_not_in_waba",):
        return ValidationFailedError(exc.message, code=exc.code)
    if (
        isinstance(exc, ReauthorizationRequiredError)
        or exc.code == "invalid_credentials"
    ):
        return InvalidCredentialsError(exc.message)
    return ProviderError(exc.message)


# =============================================================================
# Shared rows
# =============================================================================


def summary(inbox: Inbox, connection: ChannelConnection) -> InboxSummary:
    return InboxSummary(
        id=inbox.id,
        name=inbox.name,
        provider=inbox.provider.value,
        connection=ConnectionRead(
            status=connection.status.value,
            last_error=connection.last_error,
            token_expires_at=connection.token_expires_at,
        ),
    )


async def _route_free(
    session: AsyncSession, provider: str, external_id: str, inbox_id: uuid.UUID | None
) -> None:
    """A Page / number / account is active in one inbox at a time (§7)."""
    route = await session.get(ChannelRoute, (provider, external_id))
    if route is not None and route.inbox_id != inbox_id:
        raise ConflictError(
            "This account is already connected to another inbox",
            code="channel_already_connected",
        )


async def _inbox_for(
    session: AsyncSession,
    ctx: TenantContext,
    provider: str,
    name: str,
    inbox_id: uuid.UUID | None,
) -> tuple[Inbox, ChannelConnection]:
    """New inbox + pending connection, or the existing one when reauthorizing."""
    ws = ctx.require_workspace()
    if inbox_id is not None:
        inbox = await session.scalar(
            select(Inbox).where(
                Inbox.id == inbox_id,
                Inbox.workspace_id == ws,
                Inbox.provider == provider,
                Inbox.deleted_at.is_(None),
            )
        )
        connection = (
            await session.scalar(
                select(ChannelConnection).where(ChannelConnection.inbox_id == inbox_id)
            )
            if inbox
            else None
        )
        if inbox is None or connection is None:
            raise NotFoundError("Inbox not found")
        return inbox, connection
    await _check_inbox_limit(session, ctx.organization_id)
    inbox = Inbox(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ws,
        name=name,
        provider=ChannelProvider(provider),
    )
    session.add(inbox)
    await session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ws,
        inbox_id=inbox.id,
        provider=ChannelProvider(provider),
        status=ConnectionStatus.PENDING,
        connected_by=ctx.principal.user_id,
    )
    session.add(connection)
    await session.flush()
    return inbox, connection


async def _activate(
    session: AsyncSession,
    ctx: TenantContext,
    inbox: Inbox,
    connection: ChannelConnection,
    *,
    external_id: str,
    secrets: dict[str, str] | None,
    config: dict[str, Any] | None = None,
    token_expires_at: datetime | None = None,
    reauthorized: bool,
) -> None:
    provider = connection.provider.value
    # Replace any old route of this connection (another Page on reconnect).
    await session.execute(
        delete(ChannelRoute).where(ChannelRoute.channel_connection_id == connection.id)
    )
    await session.flush()
    session.add(
        ChannelRoute(
            provider=ChannelProvider(provider),
            external_id=external_id,
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
            inbox_id=inbox.id,
            channel_connection_id=connection.id,
        )
    )
    if secrets is not None:
        connection.credentials_ciphertext, connection.credentials_key_version = (
            encrypt_json(secrets)
        )
    connection.config = {
        **(connection.config or {}),
        **(config or {}),
        "external_id": external_id,
    }
    connection.status = ConnectionStatus.ACTIVE
    connection.token_expires_at = token_expires_at
    connection.last_error = None
    connection.connected_at = utcnow()
    connection.connected_by = ctx.principal.user_id
    connection.disconnected_at = None
    session.add(
        EventOutbox(
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
            event_name="inbox.connection_changed",
            aggregate_type="inbox",
            aggregate_id=inbox.id,
            payload={"inbox_id": str(inbox.id), "status": "active", "last_error": None},
        )
    )
    audit(
        session,
        action="inbox.reauthorized" if reauthorized else "inbox.created",
        resource_type="inbox",
        resource_id=inbox.id,
        actor_id=ctx.principal.user_id,
        organization_id=inbox.organization_id,
        workspace_id=inbox.workspace_id,
        changes={"provider": provider, "external_id": external_id},
    )


def providers() -> list[ProviderInfo]:
    meta = bool(settings.META_APP_ID and settings.META_APP_SECRET)
    return [
        ProviderInfo(
            provider="web_widget", setup="builtin", configured=True, release="mvp"
        ),
        ProviderInfo(
            provider="whatsapp_cloud",
            setup="manual",
            configured=bool(settings.META_APP_SECRET),
            release="mvp",
        ),
        ProviderInfo(
            provider="whatsapp_cloud",
            setup="embedded_signup",
            configured=meta and bool(settings.META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID),
            release="r1",
        ),
        ProviderInfo(
            provider="facebook_messenger", setup="oauth", configured=meta, release="mvp"
        ),
        ProviderInfo(
            provider="instagram",
            setup="oauth",
            configured=bool(
                settings.META_INSTAGRAM_APP_ID
                and (settings.META_INSTAGRAM_APP_SECRET or settings.META_APP_SECRET)
            ),
            release="r1",
        ),
    ]


# =============================================================================
# WhatsApp
# =============================================================================


async def upsert_templates(
    session: AsyncSession,
    waba: WhatsappBusinessAccount,
    templates: list[dict[str, Any]],
) -> int:
    now = utcnow()
    count = 0
    for t in templates:
        if not t.get("id") or not t.get("name") or not t.get("language"):
            continue
        components = t.get("components") or []
        fmt = str(t.get("parameter_format") or "").lower()
        if fmt not in ("named", "positional"):
            names = [p for c in components for p in placeholders(c.get("text"))]
            fmt = "named" if any(not n.isdigit() for n in names) else "positional"
        row = await session.scalar(
            select(WhatsappMessageTemplate).where(
                WhatsappMessageTemplate.waba_id == waba.id,
                WhatsappMessageTemplate.name == t["name"],
                WhatsappMessageTemplate.language == t["language"],
            )
        )
        if row is None:
            row = WhatsappMessageTemplate(
                id=uuid.uuid4(),
                organization_id=waba.organization_id,
                waba_id=waba.id,
                name=str(t["name"])[:512],
                language=str(t["language"])[:16],
            )
            session.add(row)
        row.external_template_id = str(t["id"])
        row.category = str(t.get("category") or "UTILITY")[:32]
        row.status = str(t.get("status") or "PENDING")[:32]
        row.components = components
        row.parameter_format = fmt
        row.synced_at = now
        count += 1
    waba.templates_synced_at = now
    return count


async def _whatsapp_finish(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    inbox_name: str,
    waba_id: str,
    result: ConnectResult,
    inbox_id: uuid.UUID | None,
) -> WhatsAppConnected:
    phone_number_id = result.external_id
    await _route_free(session, "whatsapp_cloud", phone_number_id, inbox_id)
    existing_number = await session.scalar(
        select(WhatsappPhoneNumber).where(
            WhatsappPhoneNumber.phone_number_id == phone_number_id
        )
    )
    if existing_number is not None and (
        inbox_id is None or existing_number.organization_id != ctx.organization_id
    ):
        conn = await session.get(
            ChannelConnection, existing_number.channel_connection_id
        )
        if conn is None or conn.inbox_id != inbox_id:
            raise ConflictError(
                "This number is already connected", code="channel_already_connected"
            )
    inbox, connection = await _inbox_for(
        session, ctx, "whatsapp_cloud", inbox_name, inbox_id
    )
    waba = await session.scalar(
        select(WhatsappBusinessAccount).where(
            WhatsappBusinessAccount.organization_id == ctx.organization_id,
            WhatsappBusinessAccount.waba_id == waba_id,
        )
    )
    if waba is None:
        waba = WhatsappBusinessAccount(
            id=uuid.uuid4(), organization_id=ctx.organization_id, waba_id=waba_id
        )
        session.add(waba)
    waba.credentials_ciphertext, waba.credentials_key_version = encrypt_json(
        result.secrets
    )
    await session.flush()
    number = result.details.get("number") or {}
    phone = existing_number or WhatsappPhoneNumber(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=inbox.workspace_id,
        channel_connection_id=connection.id,
        phone_number_id=phone_number_id,
    )
    phone.waba_id = waba.id
    phone.display_phone_number = str(
        number.get("display_phone_number") or phone_number_id
    )[:32]
    phone.verified_name = number.get("verified_name")
    phone.quality_rating = number.get("quality_rating")
    phone.messaging_limit_tier = number.get("messaging_limit_tier")
    phone.health = number
    phone.health_checked_at = utcnow()
    if existing_number is None:
        session.add(phone)
    synced = await upsert_templates(
        session, waba, result.details.get("templates") or []
    )
    await _activate(
        session,
        ctx,
        inbox,
        connection,
        external_id=phone_number_id,
        secrets=None,  # the token lives on the WABA row, shared by its numbers
        config={"waba_id": waba_id},
        reauthorized=inbox_id is not None,
    )
    connection.credentials_ciphertext = None
    connection.credentials_key_version = None
    await session.commit()
    return WhatsAppConnected(
        inbox=summary(inbox, connection),
        phone_number={
            "display_phone_number": phone.display_phone_number,
            "verified_name": phone.verified_name,
            "quality_rating": phone.quality_rating,
        },
        templates_synced=synced,
    )


async def whatsapp_manual(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    *,
    inbox_name: str,
    waba_id: str,
    phone_number_id: str,
    access_token: str,
    inbox_id: uuid.UUID | None,
) -> WhatsAppConnected:
    await _route_free(session, "whatsapp_cloud", phone_number_id, inbox_id)
    try:
        result = await WhatsAppCloudAdapter.validate_manual(
            http,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    return await _whatsapp_finish(
        session,
        ctx,
        inbox_name=inbox_name,
        waba_id=waba_id,
        result=result,
        inbox_id=inbox_id,
    )


async def whatsapp_embedded_signup(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    *,
    inbox_name: str,
    code: str,
    waba_id: str,
    phone_number_id: str,
    inbox_id: uuid.UUID | None,
) -> WhatsAppConnected:
    if not (settings.META_APP_ID and settings.META_APP_SECRET):
        raise ChannelNotConfiguredError()
    await _route_free(session, "whatsapp_cloud", phone_number_id, inbox_id)
    try:
        token = await WhatsAppCloudAdapter.exchange_embedded_signup_code(
            http,
            code=code,
            app_id=settings.META_APP_ID,
            app_secret=settings.META_APP_SECRET,
            waba_id=waba_id,
        )
        result = await WhatsAppCloudAdapter.validate_manual(
            http, waba_id=waba_id, phone_number_id=phone_number_id, access_token=token
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    return await _whatsapp_finish(
        session,
        ctx,
        inbox_name=inbox_name,
        waba_id=waba_id,
        result=result,
        inbox_id=inbox_id,
    )


async def _whatsapp_rows(
    session: AsyncSession, ctx: TenantContext, inbox_id: uuid.UUID
) -> tuple[Inbox, ChannelConnection, WhatsappPhoneNumber, WhatsappBusinessAccount]:
    inbox, connection = await _meta_inbox(session, ctx, inbox_id)
    phone = await session.scalar(
        select(WhatsappPhoneNumber).where(
            WhatsappPhoneNumber.channel_connection_id == connection.id
        )
    )
    waba = await session.get(WhatsappBusinessAccount, phone.waba_id) if phone else None
    if phone is None or waba is None:
        raise NotFoundError("WhatsApp inbox not found")
    return inbox, connection, phone, waba


async def list_templates(
    session: AsyncSession,
    ctx: TenantContext,
    inbox_id: uuid.UUID,
    *,
    status: str | None,
    q: str | None,
) -> list[TemplateRead]:
    _, _, _, waba = await _whatsapp_rows(session, ctx, inbox_id)
    query = select(WhatsappMessageTemplate).where(
        WhatsappMessageTemplate.waba_id == waba.id
    )
    if status:
        query = query.where(WhatsappMessageTemplate.status == status.upper())
    if q:
        query = query.where(
            WhatsappMessageTemplate.name.ilike(f"%{q.replace('%', '')}%")
        )
    rows = await session.scalars(
        query.order_by(WhatsappMessageTemplate.name, WhatsappMessageTemplate.language)
    )
    return [
        TemplateRead(
            id=r.id,
            name=r.name,
            language=r.language,
            category=r.category,
            status=r.status,
            parameter_format=r.parameter_format,
            components=r.components,
            synced_at=r.synced_at,
        )
        for r in rows
    ]


async def sync_templates(
    session: AsyncSession, waba: WhatsappBusinessAccount, http: httpx.AsyncClient
) -> int:
    token = decrypt_json(waba.credentials_ciphertext).get("access_token")
    if not token:
        return 0
    templates = await WhatsAppCloudAdapter.fetch_templates(
        http, waba_id=waba.waba_id, access_token=str(token)
    )
    count = await upsert_templates(session, waba, templates)
    await session.commit()
    return count


async def sync_templates_for_inbox(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    inbox_id: uuid.UUID,
) -> int:
    _, _, _, waba = await _whatsapp_rows(session, ctx, inbox_id)
    try:
        return await sync_templates(session, waba, http)
    except ChannelError as exc:
        raise provider_failure(exc) from exc


PROFILE_FIELDS = "about,address,description,email,websites,vertical"


async def get_business_profile(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    inbox_id: uuid.UUID,
) -> BusinessProfile:
    _, connection, phone, _ = await _whatsapp_rows(session, ctx, inbox_id)
    adapter = await load_adapter(session, connection, http)
    try:
        data = await adapter.graph.get(  # type: ignore[attr-defined]
            f"{phone.phone_number_id}/whatsapp_business_profile",
            params={"fields": PROFILE_FIELDS},
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    row = (data.get("data") or [{}])[0]
    return BusinessProfile(**{k: row.get(k) for k in BusinessProfile.model_fields})


async def update_business_profile(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    inbox_id: uuid.UUID,
    body: BusinessProfileUpdate,
) -> BusinessProfile:
    _, connection, phone, _ = await _whatsapp_rows(session, ctx, inbox_id)
    adapter = await load_adapter(session, connection, http)
    changes = body.model_dump(exclude_unset=True)
    try:
        await adapter.graph.post(  # type: ignore[attr-defined]
            f"{phone.phone_number_id}/whatsapp_business_profile",
            json={"messaging_product": "whatsapp", **changes},
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    return await get_business_profile(session, ctx, http, inbox_id)


# =============================================================================
# OAuth (Messenger + Instagram)
# =============================================================================


async def oauth_start(
    session: AsyncSession,
    redis: Redis,
    ctx: TenantContext,
    provider: str,
    inbox_id: uuid.UUID | None,
) -> str:
    if provider == "facebook_messenger" and not (
        settings.META_APP_ID and settings.META_APP_SECRET
    ):
        raise ChannelNotConfiguredError()
    if provider == "instagram" and not settings.META_INSTAGRAM_APP_ID:
        raise ChannelNotConfiguredError()
    if inbox_id is not None:
        await _meta_inbox(session, ctx, inbox_id, provider=provider)
    state = await oauth.create_state(
        redis,
        {
            "provider": provider,
            "organization_id": str(ctx.organization_id),
            "workspace_id": str(ctx.require_workspace()),
            "user_id": str(ctx.principal.user_id),
            "inbox_id": str(inbox_id) if inbox_id else None,
        },
    )
    return oauth.authorize_url(provider, state)


def _frontend(data: dict[str, Any], query: dict[str, str]) -> str:
    provider = {"facebook_messenger": "messenger"}.get(
        str(data.get("provider")), str(data.get("provider"))
    )
    base = settings.FRONTEND_URL.rstrip("/")
    path = f"/orgs/{data.get('organization_id')}/ws/{data.get('workspace_id')}/settings/inboxes"
    if data.get("inbox_id"):
        path += f"/{data['inbox_id']}/reauthorize/{provider}"
    else:
        path += f"/new/{provider}"
    return f"{base}{path}?{urlencode(query)}"


async def _may_manage(session: AsyncSession, data: dict[str, Any]) -> bool:
    """The callback runs without a login: re-check that the user who started
    still has inbox.manage in that workspace (FACEBOOK_MESSENGER.md §7)."""
    try:
        user_id = uuid.UUID(str(data["user_id"]))
        org_id = uuid.UUID(str(data["organization_id"]))
        ws_id = uuid.UUID(str(data["workspace_id"]))
    except (KeyError, ValueError):
        return False
    resolved = await resolve_membership(session, user_id, org_id)
    if resolved is None or resolved.organization.status.value != "active":
        return False
    return "inbox.manage" in resolved.permissions and await can_access_workspace(
        session, resolved, ws_id
    )


async def oauth_callback(
    session: AsyncSession,
    redis: Redis,
    http: httpx.AsyncClient,
    *,
    expected_provider: str,
    code: str | None,
    state: str | None,
    error: str | None,
) -> str:
    """Returns the dashboard URL to redirect to (?selection_token= or ?error=)."""
    data = await oauth.consume_state(redis, state or "")
    if data is None or data.get("provider") not in expected_provider.split("|"):
        return f"{settings.FRONTEND_URL.rstrip('/')}/oauth/error?{urlencode({'error': 'invalid_state'})}"
    if error or not code:
        return _frontend(data, {"error": error or "access_denied"})
    if not await _may_manage(session, data):
        return _frontend(data, {"error": "forbidden"})
    provider = str(data["provider"])
    try:
        if provider == "facebook_messenger":
            user_token = await MessengerAdapter.exchange_code(
                http,
                code=code,
                redirect_uri=oauth.redirect_uri(provider),
                app_id=settings.META_APP_ID,
                app_secret=settings.META_APP_SECRET,
            )
            pages = await MessengerAdapter.list_pages(http, user_token)
            # App-scoped user id, for the deauthorize callback.
            me = await GraphClient(http, user_token, product=provider).get(
                "me", params={"fields": "id"}
            )
            selection = {
                **data,
                "user_token": user_token,
                "facebook_user_id": me.get("id"),
                "pages": pages,
            }
        else:
            result = await InstagramAdapter.exchange_code(
                http,
                code=code,
                redirect_uri=oauth.redirect_uri(provider),
                app_id=settings.META_INSTAGRAM_APP_ID,
                app_secret=settings.META_INSTAGRAM_APP_SECRET
                or settings.META_APP_SECRET,
            )
            selection = {
                **data,
                "account": {
                    "instagram_user_id": result.external_id,
                    "username": result.details.get("username"),
                    "name": result.details.get("name"),
                },
                "token": result.secrets["access_token"],
                "token_expires_at": result.token_expires_at.isoformat()
                if result.token_expires_at
                else None,
            }
    except ChannelError as exc:
        return _frontend(data, {"error": exc.code})
    token = await oauth.create_selection(redis, selection)
    return _frontend(data, {"selection_token": token})


async def _selection(
    redis: Redis, ctx: TenantContext, token: str, provider: str
) -> dict[str, Any]:
    data = await oauth.read_selection(redis, token)
    if (
        data is None
        or data.get("provider") != provider
        or data.get("user_id") != str(ctx.principal.user_id)
        or data.get("workspace_id") != str(ctx.require_workspace())
    ):
        raise ValidationFailedError(
            "The selection expired; connect again", code="selection_expired"
        )
    return data


async def messenger_pages(
    session: AsyncSession, redis: Redis, ctx: TenantContext, token: str
) -> list[PageOption]:
    data = await _selection(redis, ctx, token, "facebook_messenger")
    out = []
    for p in data.get("pages") or []:
        route = await session.get(ChannelRoute, ("facebook_messenger", p["page_id"]))
        out.append(
            PageOption(
                page_id=p["page_id"],
                page_name=p.get("page_name"),
                instagram_id=p.get("instagram_id"),
                can_message=not p.get("tasks")
                or "MESSAGING" in p.get("tasks", [])
                or "MANAGE" in p.get("tasks", []),
                connected_inbox_id=route.inbox_id
                if route and route.organization_id == ctx.organization_id
                else None,
            )
        )
    return out


async def messenger_connect(
    session: AsyncSession,
    redis: Redis,
    http: httpx.AsyncClient,
    ctx: TenantContext,
    *,
    selection_token: str,
    page_id: str | None,
    inbox_name: str,
    inbox_id: uuid.UUID | None,
) -> MessengerConnected:
    data = await _selection(redis, ctx, selection_token, "facebook_messenger")
    pages = {p["page_id"]: p for p in data.get("pages") or []}
    if inbox_id is not None and page_id is None:
        connection = await session.scalar(
            select(ChannelConnection).where(ChannelConnection.inbox_id == inbox_id)
        )
        page_id = (
            str((connection.config or {}).get("external_id")) if connection else None
        )
    page = pages.get(page_id or "")
    if page is None:
        raise ValidationFailedError(
            "That Page isn't in your Facebook account", code="page_not_found"
        )
    await _route_free(session, "facebook_messenger", page["page_id"], inbox_id)
    try:
        fields = await MessengerAdapter.subscribe_page(
            http, page_id=page["page_id"], page_token=page["access_token"]
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    inbox, connection = await _inbox_for(
        session, ctx, "facebook_messenger", inbox_name, inbox_id
    )
    row = await session.scalar(
        select(FacebookPage).where(FacebookPage.channel_connection_id == connection.id)
    )
    if row is None:
        row = FacebookPage(
            id=uuid.uuid4(),
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
            channel_connection_id=connection.id,
            page_id=page["page_id"],
        )
        session.add(row)
    row.page_id = page["page_id"]
    row.page_name = page.get("page_name")
    row.linked_instagram_id = page.get("instagram_id")
    row.subscribed_fields = fields
    await _activate(
        session,
        ctx,
        inbox,
        connection,
        external_id=page["page_id"],
        secrets={
            "page_access_token": page["access_token"],
            "user_access_token": data["user_token"],
        },
        config={
            "facebook_user_id": data.get("facebook_user_id"),
            "subscribed_fields": fields,
        },
        reauthorized=inbox_id is not None,
    )
    await session.commit()
    await oauth.drop_selection(redis, selection_token)
    return MessengerConnected(
        inbox=summary(inbox, connection),
        page={"page_id": row.page_id, "page_name": row.page_name},
    )


async def instagram_selection(
    redis: Redis, ctx: TenantContext, token: str
) -> InstagramSelection:
    data = await _selection(redis, ctx, token, "instagram")
    return InstagramSelection(**data["account"])


async def instagram_connect(
    session: AsyncSession,
    redis: Redis,
    http: httpx.AsyncClient,
    ctx: TenantContext,
    *,
    selection_token: str,
    inbox_name: str,
    inbox_id: uuid.UUID | None,
) -> InstagramConnected:
    data = await _selection(redis, ctx, selection_token, "instagram")
    account = data["account"]
    ig_user_id = str(account["instagram_user_id"])
    await _route_free(session, "instagram", ig_user_id, inbox_id)
    existing = await session.scalar(
        select(InstagramAccount).where(InstagramAccount.instagram_user_id == ig_user_id)
    )
    if existing is not None:
        conn = await session.get(ChannelConnection, existing.channel_connection_id)
        if conn is None or conn.inbox_id != inbox_id:
            raise ConflictError(
                "This account is already connected", code="channel_already_connected"
            )
    try:
        await InstagramAdapter.subscribe(
            http, ig_user_id=ig_user_id, token=data["token"]
        )
    except ChannelError as exc:
        raise provider_failure(exc) from exc
    inbox, connection = await _inbox_for(
        session, ctx, "instagram", inbox_name, inbox_id
    )
    row = existing or InstagramAccount(
        id=uuid.uuid4(),
        organization_id=inbox.organization_id,
        workspace_id=inbox.workspace_id,
        channel_connection_id=connection.id,
        instagram_user_id=ig_user_id,
        connection_method="instagram_login",
    )
    row.username = account.get("username")
    if existing is None:
        session.add(row)
    expires = data.get("token_expires_at")
    await _activate(
        session,
        ctx,
        inbox,
        connection,
        external_id=ig_user_id,
        secrets={"access_token": data["token"]},
        token_expires_at=datetime.fromisoformat(expires) if expires else None,
        reauthorized=inbox_id is not None,
    )
    await session.commit()
    await oauth.drop_selection(redis, selection_token)
    return InstagramConnected(
        inbox=summary(inbox, connection),
        account={"instagram_user_id": ig_user_id, "username": row.username},
    )


# =============================================================================
# Disconnect, health
# =============================================================================


async def _meta_inbox(
    session: AsyncSession,
    ctx: TenantContext,
    inbox_id: uuid.UUID,
    *,
    provider: str | None = None,
) -> tuple[Inbox, ChannelConnection]:
    inbox = await session.scalar(
        select(Inbox).where(
            Inbox.id == inbox_id,
            Inbox.workspace_id == ctx.require_workspace(),
            Inbox.deleted_at.is_(None),
        )
    )
    if (
        inbox is None
        or inbox.provider.value not in META_PROVIDERS
        or (provider and inbox.provider.value != provider)
    ):
        raise NotFoundError("Inbox not found")
    connection = await session.scalar(
        select(ChannelConnection).where(ChannelConnection.inbox_id == inbox_id)
    )
    if connection is None:
        raise NotFoundError("Inbox not found")
    return inbox, connection


async def disconnect(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    inbox_id: uuid.UUID,
) -> InboxSummary:
    inbox, connection = await _meta_inbox(session, ctx, inbox_id)
    if connection.status != ConnectionStatus.DISCONNECTED:
        try:
            adapter = await load_adapter(session, connection, http)
            await adapter.disconnect()
        except Exception:
            pass  # best effort at the provider; we disconnect locally regardless
        await session.execute(
            delete(ChannelRoute).where(
                ChannelRoute.channel_connection_id == connection.id
            )
        )
        connection.credentials_ciphertext = None
        connection.credentials_key_version = None
        connection.status = ConnectionStatus.DISCONNECTED
        connection.disconnected_at = utcnow()
        session.add(
            EventOutbox(
                organization_id=inbox.organization_id,
                workspace_id=inbox.workspace_id,
                event_name="inbox.connection_changed",
                aggregate_type="inbox",
                aggregate_id=inbox.id,
                payload={
                    "inbox_id": str(inbox.id),
                    "status": "disconnected",
                    "last_error": None,
                },
            )
        )
        audit(
            session,
            action="inbox.disconnected",
            resource_type="inbox",
            resource_id=inbox.id,
            actor_id=ctx.principal.user_id,
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
        )
        await session.commit()
    return summary(inbox, connection)


async def health(
    session: AsyncSession,
    ctx: TenantContext,
    http: httpx.AsyncClient,
    inbox_id: uuid.UUID,
) -> HealthResponse:
    inbox, connection = await _meta_inbox(session, ctx, inbox_id)
    if connection.status == ConnectionStatus.DISCONNECTED:
        return HealthResponse(
            ok=False, status="disconnected", details={}, error="disconnected"
        )
    adapter = await load_adapter(session, connection, http)
    try:
        result = await adapter.health_check()
    except ChannelError as exc:
        return HealthResponse(
            ok=False, status=connection.status.value, details={}, error=exc.code
        )
    if (
        result.error == "reauthorization_required"
        and connection.status == ConnectionStatus.ACTIVE
    ):
        connection.status = ConnectionStatus.REAUTHORIZATION_REQUIRED
        connection.last_error = "reauthorization_required"
        connection.last_error_at = utcnow()
        await session.commit()
    elif result.ok and connection.status == ConnectionStatus.ERROR:
        connection.status = (
            ConnectionStatus.ACTIVE
        )  # §7: error → active when health passes
        connection.last_error = None
        await session.commit()
    return HealthResponse(
        ok=result.ok,
        status=connection.status.value,
        details=result.details,
        error=result.error,
    )


# =============================================================================
# Meta user callbacks (signed_request)
# =============================================================================


async def deauthorize(session: AsyncSession, facebook_user_id: str) -> int:
    """The user removed our app: their Messenger connections need reauthorizing."""
    connections = list(
        await session.scalars(
            select(ChannelConnection).where(
                ChannelConnection.provider.in_(["facebook_messenger", "instagram"]),
                ChannelConnection.status == ConnectionStatus.ACTIVE,
                ChannelConnection.config["facebook_user_id"].astext == facebook_user_id,
            )
        )
    )
    for c in connections:
        c.status = ConnectionStatus.REAUTHORIZATION_REQUIRED
        c.last_error = "app_deauthorized"
        c.last_error_at = utcnow()
        session.add(
            EventOutbox(
                organization_id=c.organization_id,
                workspace_id=c.workspace_id,
                event_name="inbox.connection_changed",
                aggregate_type="inbox",
                aggregate_id=c.inbox_id,
                payload={
                    "inbox_id": str(c.inbox_id),
                    "status": c.status.value,
                    "last_error": "app_deauthorized",
                },
            )
        )
    await session.commit()
    return len(connections)


async def erase_identities(session: AsyncSession, source_id: str) -> int:
    """Data deletion request: wipe the person's Messenger/Instagram data
    (identity profile, contact fields, the content of their messages)."""
    identities = list(
        await session.scalars(
            select(ContactIdentity)
            .join(Inbox, Inbox.id == ContactIdentity.inbox_id)
            .where(
                ContactIdentity.source_id == source_id,
                Inbox.provider.in_(["facebook_messenger", "instagram"]),
            )
        )
    )
    for identity in identities:
        identity.profile = {"erased_at": datetime.now(UTC).isoformat()}
        identity.source_id = f"erased:{identity.id}"
        await session.execute(
            update(Message)
            .where(
                Message.sender_contact_id == identity.contact_id,
                Message.inbox_id == identity.inbox_id,
                Message.direction == MessageDirection.INCOMING,
            )
            .values(content=None, content_attributes={"erased": True})
        )
        contact = await session.get(Contact, identity.contact_id)
        if contact is not None:
            others = await session.scalar(
                select(ContactIdentity.id)
                .where(
                    ContactIdentity.contact_id == contact.id,
                    ContactIdentity.id != identity.id,
                )
                .limit(1)
            )
            if others is None:
                contact.name = "Deleted user"
                contact.email = None
                contact.phone_number = None
                contact.additional_attributes = {}
                contact.custom_attributes = {}
    await session.commit()
    return len(identities)
