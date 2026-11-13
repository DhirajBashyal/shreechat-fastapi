"""/ws/widget — realtime for website visitors (docs/integrations/WEBSITE_CHAT.md §4,
docs/backend/REALTIME_COMMUNICATION.md §4–5).

    wss://…/ws/widget?website_token=…
    → {"type": "auth", "token": "<visitor_token>", "parent_origin": "https://shop…"}
                                                         first frame, within 5 s
    ← {"type": "ready", "connection_id": "…", "heartbeat_seconds": 25}
    ← {"type": "event", "event": "message.created", "data": {"message": {…}}, …}
    → {"type": "typing", "conversation_id": "…", "state": "on"}
    → {"type": "ping"}   ← {"type": "pong"}

The token never goes in the URL (access logs). The Origin header is checked
against the widget's allowed_origins; from our own widget app (the iframe)
the auth frame's ``parent_origin`` is checked instead (§8). The socket closes with 4401 when the
token is missing, invalid or expires, 4403 for a disallowed origin and 4404
for an unknown website token. Messages are sent over REST, not the socket.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.core.db import sessionmanager
from app.core.exceptions import APIError
from app.core.logger import get_logger
from app.core.realtime import contact_channel, org_channel, publish
from app.services import widget as service

logger = get_logger()
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 5
HEARTBEAT_SECONDS = 25
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404


@router.websocket("/ws/widget")
async def widget_socket(ws: WebSocket) -> None:
    website_token = ws.query_params.get("website_token", "")
    redis: Redis = ws.app.state.redis
    assert sessionmanager.session_factory is not None

    # Resolve the widget and check the origin before accepting.
    async with sessionmanager.session_factory() as session:
        try:
            ctx = await service.resolve_widget(session, website_token)
        except APIError:
            await ws.close(code=CLOSE_NOT_FOUND)
            return
        if not service.origin_allowed(ctx.widget, ws.headers.get("origin")):
            await ws.close(code=CLOSE_FORBIDDEN)
            return
    await ws.accept()

    try:
        frame = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        await _close(ws, CLOSE_UNAUTHORIZED)
        return
    token = frame.get("token") if isinstance(frame, dict) else None
    if not isinstance(frame, dict) or frame.get("type") != "auth" or not token:
        await _close(ws, CLOSE_UNAUTHORIZED)
        return
    parent = frame.get("parent_origin")
    if not service.origin_allowed(
        ctx.widget,
        ws.headers.get("origin"),
        parent if isinstance(parent, str) else None,
    ):
        await _close(ws, CLOSE_FORBIDDEN)
        return
    async with sessionmanager.session_factory() as session:
        try:
            ctx = await service.resolve_widget(session, website_token)
            visitor = await service.resolve_visitor(session, ctx, token)
        except APIError:
            await _close(ws, CLOSE_UNAUTHORIZED)
            return
    realtime_token = visitor.identity.realtime_token
    contact_id, contact_name = visitor.contact.id, visitor.contact.name

    pubsub = redis.pubsub()
    await pubsub.subscribe(contact_channel(realtime_token))
    await ws.send_json(
        {
            "type": "ready",
            "connection_id": f"c_{uuid.uuid4().hex[:16]}",
            "heartbeat_seconds": HEARTBEAT_SECONDS,
        }
    )
    own_conversations: set[uuid.UUID] = set()

    async def forward() -> None:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is None:
                continue
            raw = message["data"]
            envelope = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            await ws.send_json({"type": "event", **envelope})

    async def owns(conversation_id: uuid.UUID) -> bool:
        if conversation_id in own_conversations:
            return True
        assert sessionmanager.session_factory is not None
        async with sessionmanager.session_factory() as session:
            try:
                fresh = await service.resolve_widget(session, website_token)
                await service.typing_conversation(
                    session, fresh, token, conversation_id
                )
            except APIError:
                return False
        own_conversations.add(conversation_id)
        return True

    async def receive() -> None:
        while True:
            data = await ws.receive_json()
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "typing":
                try:
                    conversation_id = uuid.UUID(str(data.get("conversation_id")))
                except ValueError:
                    await ws.send_json({"type": "error", "code": "invalid_frame"})
                    continue
                if not await owns(conversation_id):
                    await ws.send_json({"type": "error", "code": "not_found"})
                    continue
                await publish(
                    redis,
                    org_channel(ctx.organization_id),
                    "conversation.typing",
                    {
                        "conversation_id": str(conversation_id),
                        "actor": {
                            "kind": "contact",
                            "id": str(contact_id),
                            "name": contact_name,
                        },
                        "state": "on" if data.get("state") == "on" else "off",
                    },
                    workspace_id=ctx.workspace_id,
                )
            else:
                await ws.send_json({"type": "error", "code": "unknown_frame"})

    expired = False

    async def expire() -> None:
        nonlocal expired
        await anyio.sleep(max(0.0, visitor.expires_at - time.time()))
        expired = True

    try:
        async with anyio.create_task_group() as tg:

            async def run(part: Callable[[], Awaitable[None]]) -> None:
                # The first part to finish (socket closed, token expired)
                # stops the others.
                try:
                    await part()
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning(
                        "Widget socket error", extra={"error": repr(exc)[:200]}
                    )
                finally:
                    tg.cancel_scope.cancel()

            for part in (forward, receive, expire):
                tg.start_soon(run, part)
        if expired:
            await _close(ws, CLOSE_UNAUTHORIZED)
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except RuntimeError:
        pass  # already closed
