"""/api/v1/widget — the website live chat visitor API (docs/integrations/WEBSITE_CHAT.md §3).

Public: no user login. Every call names the widget with ``website_token``
(query string, or the JSON body), and all but ``config`` and ``sessions``
need the visitor's ``X-Visitor-Token``. Requests from browsers must come from
one of the widget's ``allowed_origins``; requests from our own widget app (the
iframe) are checked by the embedding page's origin in ``X-Parent-Origin``.
"""

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from slowapi.util import get_remote_address

from app.ai.queue import enqueue_incoming
from app.api.deps import DbSession
from app.core.config import settings
from app.core.exceptions import APIError
from app.core.logger import get_logger
from app.core.rate_limit import limiter
from app.core.realtime import org_channel, publish
from app.models import Conversation
from app.schemas.widget import (
    ContactUpdate,
    ConversationAttributesUpdate,
    ConversationCreate,
    ConversationCreated,
    EventCreate,
    IdentifyRequest,
    LabelRequest,
    MessageCreate,
    SessionCreate,
    SessionResponse,
    TypingRequest,
    UploadRequest,
    WidgetConfig,
    WidgetContact,
    WidgetConversation,
    WidgetMessage,
)
from app.services import widget as service
from app.services.widget import UploadsUnavailableError, WidgetContext

logger = get_logger()
router = APIRouter()


class WebsiteTokenRequiredError(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "website_token_required"
    message = "website_token is required"


async def _website_token(request: Request) -> str:
    token = request.query_params.get("website_token")
    if not token and request.method in ("POST", "PATCH", "PUT", "DELETE"):
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict) and isinstance(body.get("website_token"), str):
            token = body["website_token"]
    if not token:
        raise WebsiteTokenRequiredError()
    return token


async def widget_context(request: Request, session: DbSession) -> WidgetContext:
    ctx = await service.resolve_widget(session, await _website_token(request))
    service.check_origin(
        ctx.widget,
        request.headers.get("origin"),
        request.headers.get("x-parent-origin"),
    )
    return ctx


Widget = Annotated[WidgetContext, Depends(widget_context)]
VisitorToken = Annotated[str | None, Header(alias="X-Visitor-Token")]


def visitor_key(request: Request) -> str:
    """Rate-limit key: the visitor (hashed token), else the client IP."""
    token = request.headers.get("x-visitor-token")
    if token:
        return "visitor:" + hashlib.sha256(token.encode()).hexdigest()[:24]
    return get_remote_address(request)


async def _wake_ai(request: Request, conv: Conversation, message_id: uuid.UUID) -> None:
    """After commit: queue the AI run (debounced) when an AI agent owns it."""
    if not conv.ai_active:
        return
    try:
        await enqueue_incoming(request.app.state.redis, conv.id, message_id)
    except Exception as exc:  # the message is saved; the AI just won't answer it
        logger.error("AI enqueue failed", extra={"error": repr(exc)[:200]})


# --- config + sessions --------------------------------------------------------


@router.get("/config", summary="Public widget settings")
async def get_config(request: Request, ctx: Widget, session: DbSession) -> WidgetConfig:
    return await service.get_config(session, ctx, request.app.state.redis)


@router.post("/sessions", summary="Create (or resume) an anonymous visitor")
@limiter.limit(lambda: settings.WIDGET_SESSION_RATE_LIMIT)
async def create_session(
    request: Request,
    response: Response,
    body: SessionCreate,
    ctx: Widget,
    session: DbSession,
) -> SessionResponse:
    result = await service.create_session(session, ctx, body.visitor_token)
    response.status_code = (
        status.HTTP_200_OK if result.resumed else status.HTTP_201_CREATED
    )
    return result


@router.delete(
    "/sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="reset(): revoke the visitor token",
)
async def reset_session(
    ctx: Widget, session: DbSession, visitor_token: VisitorToken = None
) -> Response:
    await service.reset(session, ctx, visitor_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/identify", summary="Identify / verify the visitor (rotates the token)")
@limiter.limit("10/minute", key_func=visitor_key)
async def identify(
    request: Request,
    response: Response,
    body: IdentifyRequest,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> SessionResponse:
    return await service.identify(session, ctx, visitor_token, body)


@router.patch("/contact", summary="Update name / email / phone / custom attributes")
async def update_contact(
    body: ContactUpdate,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetContact:
    return await service.update_contact(session, ctx, visitor_token, body)


@router.delete("/contact/custom-attributes/{key}")
async def delete_contact_attribute(
    key: str, ctx: Widget, session: DbSession, visitor_token: VisitorToken = None
) -> WidgetContact:
    return await service.delete_contact_attribute(session, ctx, visitor_token, key)


# --- conversations ------------------------------------------------------------


@router.get("/conversations", summary="The visitor's conversations (latest first)")
async def list_conversations(
    ctx: Widget, session: DbSession, visitor_token: VisitorToken = None
) -> list[WidgetConversation]:
    return await service.list_conversations(session, ctx, visitor_token)


@router.post("/conversations", summary="Start a conversation with the first message")
@limiter.limit(lambda: settings.WIDGET_MESSAGE_RATE_LIMIT, key_func=visitor_key)
async def create_conversation(
    request: Request,
    response: Response,
    body: ConversationCreate,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> ConversationCreated:
    result, created, conv, incoming_id = await service.create_conversation(
        session,
        ctx,
        visitor_token,
        body,
        user_agent=request.headers.get("user-agent", ""),
        accept_language=request.headers.get("accept-language"),
    )
    if incoming_id is not None:
        await _wake_ai(request, conv, incoming_id)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.patch(
    "/conversations/{conversation_id}", summary="Conversation custom attributes"
)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationAttributesUpdate,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetConversation:
    return await service.update_conversation_attributes(
        session, ctx, visitor_token, conversation_id, body.custom_attributes
    )


@router.get("/conversations/{conversation_id}/messages", summary="Message history")
async def list_messages(
    conversation_id: uuid.UUID,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
    before: Annotated[
        int | None, Query(description="sequence_number: older page")
    ] = None,
    after: Annotated[
        int | None, Query(description="sequence_number: missed messages")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[WidgetMessage]:
    return await service.list_messages(
        session,
        ctx,
        visitor_token,
        conversation_id,
        before=before,
        after=after,
        limit=limit,
    )


@router.post("/conversations/{conversation_id}/messages", summary="Send a message")
@limiter.limit(lambda: settings.WIDGET_MESSAGE_RATE_LIMIT, key_func=visitor_key)
async def send_message(
    request: Request,
    response: Response,
    conversation_id: uuid.UUID,
    body: MessageCreate,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetMessage:
    message, created, conv = await service.send_message(
        session, ctx, visitor_token, conversation_id, body
    )
    if created:
        await _wake_ai(request, conv, message.id)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return message


@router.post(
    "/conversations/{conversation_id}/typing", status_code=status.HTTP_204_NO_CONTENT
)
async def typing(
    request: Request,
    conversation_id: uuid.UUID,
    body: TypingRequest,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> Response:
    v, conv = await service.typing_conversation(
        session, ctx, visitor_token, conversation_id
    )
    await publish_typing(request.app.state.redis, ctx, v, conv, body.state)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def publish_typing(
    redis: object,
    ctx: WidgetContext,
    v: service.Visitor,
    conv: Conversation,
    state: str,
) -> None:
    await publish(
        redis,  # type: ignore[arg-type]
        org_channel(ctx.organization_id),
        "conversation.typing",
        {
            "conversation_id": str(conv.id),
            "actor": {
                "kind": "contact",
                "id": str(v.contact.id),
                "name": v.contact.name,
            },
            "state": state,
        },
        workspace_id=ctx.workspace_id,
    )


@router.post(
    "/conversations/{conversation_id}/seen", summary="Visitor read the conversation"
)
async def seen(
    conversation_id: uuid.UUID,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetConversation:
    return await service.mark_seen(session, ctx, visitor_token, conversation_id)


@router.post(
    "/conversations/{conversation_id}/resolve", summary="Visitor ends the chat"
)
async def resolve(
    conversation_id: uuid.UUID,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetConversation:
    return await service.resolve_conversation(
        session, ctx, visitor_token, conversation_id
    )


@router.post(
    "/conversations/{conversation_id}/labels",
    summary="setLabel (widget-settable labels)",
)
async def add_label(
    conversation_id: uuid.UUID,
    body: LabelRequest,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetConversation:
    return await service.set_label(
        session, ctx, visitor_token, conversation_id, body.label, add=True
    )


@router.delete("/conversations/{conversation_id}/labels/{label}", summary="removeLabel")
async def remove_label(
    conversation_id: uuid.UUID,
    label: str,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> WidgetConversation:
    return await service.set_label(
        session, ctx, visitor_token, conversation_id, label, add=False
    )


# --- uploads + events -----------------------------------------------------------


@router.post("/uploads", summary="Presigned upload (not available yet: 503)")
async def create_upload(
    body: UploadRequest,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> None:
    await service.resolve_visitor(session, ctx, visitor_token)
    raise UploadsUnavailableError()


@router.post(
    "/events", status_code=status.HTTP_202_ACCEPTED, summary="Page view / custom event"
)
@limiter.limit("60/minute", key_func=visitor_key)
async def create_event(
    request: Request,
    response: Response,
    body: EventCreate,
    ctx: Widget,
    session: DbSession,
    visitor_token: VisitorToken = None,
) -> dict[str, bool]:
    await service.record_event(
        session, ctx, visitor_token, body.name, body.url, body.properties
    )
    return {"accepted": True}
