"""Conversation operations used by the AI agent and by humans taking over.

docs/ai/HUMAN_HANDOFF.md. The AI never writes conversation/message rows itself:
graph nodes call these functions (the "MessageService / ConversationService"
of the docs). Callers hold a row lock on the conversation (SELECT … FOR UPDATE)
and commit.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.models import (
    AiConversationSession,
    Conversation,
    ConversationAssignment,
    EventOutbox,
    Message,
    ReportingEvent,
)
from app.models.enums import ConversationStatus, MessageDirection, SenderKind


def _outbox(
    session: AsyncSession, conv: Conversation, event: str, payload: dict[str, Any]
) -> None:
    session.add(
        EventOutbox(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            event_name=event,
            aggregate_type="conversation",
            aggregate_id=conv.id,
            payload={"conversation_id": str(conv.id), **payload},
        )
    )


async def lock_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id).with_for_update()
    )


def add_message(
    session: AsyncSession,
    conv: Conversation,
    *,
    content: str,
    sender_kind: SenderKind,
    direction: MessageDirection = MessageDirection.OUTGOING,
    is_private: bool = False,
    sender_ai_agent_id: uuid.UUID | None = None,
    sender_user_id: uuid.UUID | None = None,
    content_attributes: dict[str, Any] | None = None,
    client_message_id: str | None = None,
) -> Message:
    """Outgoing / private / activity message. Delivery to the channel happens in
    the outbound pipeline from the message.created outbox event (Phase 4)."""
    msg = Message(
        id=uuid.uuid4(),
        organization_id=conv.organization_id,
        workspace_id=conv.workspace_id,
        inbox_id=conv.inbox_id,
        conversation_id=conv.id,
        direction=direction,
        content=content,
        is_private=is_private,
        sender_kind=sender_kind,
        sender_ai_agent_id=sender_ai_agent_id,
        sender_user_id=sender_user_id,
        content_attributes=content_attributes or {},
        client_message_id=client_message_id,
    )
    session.add(msg)
    now = utcnow()
    conv.last_activity_at = now
    if direction == MessageDirection.OUTGOING and not is_private:
        conv.waiting_since = None
        if conv.first_reply_at is None:
            conv.first_reply_at = now
    _outbox(
        session,
        conv,
        "message.created",
        {"message_id": str(msg.id), "private": is_private},
    )
    return msg


def add_activity(session: AsyncSession, conv: Conversation, text: str) -> Message:
    return add_message(
        session,
        conv,
        content=text,
        sender_kind=SenderKind.SYSTEM,
        direction=MessageDirection.ACTIVITY,
    )


async def _set_session_status(
    session: AsyncSession, conv: Conversation, status: str
) -> None:
    values: dict[str, Any] = {"status": status}
    values["handed_off_at"] = utcnow() if status == "handed_off" else None
    await session.execute(
        update(AiConversationSession)
        .where(AiConversationSession.conversation_id == conv.id)
        .values(**values)
    )


async def handoff_to_human(
    session: AsyncSession,
    conv: Conversation,
    *,
    reason: str,
    ai_agent_id: uuid.UUID | None,
    team_id: uuid.UUID | None = None,
) -> None:
    """HUMAN_HANDOFF.md §4 steps 3–5 (the customer message and private note are
    created by the caller before this)."""
    now = utcnow()
    conv.ai_active = False
    if conv.status != ConversationStatus.OPEN:
        conv.status = ConversationStatus.OPEN
        conv.status_changed_at = now
    if team_id and conv.team_id is None:
        conv.team_id = team_id
    await _set_session_status(session, conv, "handed_off")
    session.add(
        ConversationAssignment(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            conversation_id=conv.id,
            assignee_id=conv.assignee_id,
            team_id=conv.team_id,
            assigned_by_kind=SenderKind.AI_AGENT,
            reason="ai_handoff",
        )
    )
    add_activity(session, conv, "Conversation handed to a human by AI")
    session.add(
        ReportingEvent(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            name="conversation_bot_handoff",
            conversation_id=conv.id,
            inbox_id=conv.inbox_id,
            team_id=conv.team_id,
            value_seconds=(now - conv.created_at).total_seconds()
            if conv.created_at
            else 0.0,
            event_start_at=conv.created_at or now,
            event_end_at=now,
        )
    )
    _outbox(
        session,
        conv,
        "conversation.handoff",
        {"reason": reason, "ai_agent_id": str(ai_agent_id) if ai_agent_id else None},
    )
    _outbox(session, conv, "conversation.updated", {"ai_active": False})


async def switch_off_ai(
    session: AsyncSession,
    conv: Conversation,
    *,
    reason: str,
    by_user_id: uuid.UUID | None = None,
) -> bool:
    """A human takes over without a handoff (HUMAN_HANDOFF.md §5): reasons
    agent_replied, agent_assigned, automation_assigned, agent_paused.
    Phase 3 reply/assign services call this. Returns True if the AI was on."""
    if not conv.ai_active:
        return False
    conv.ai_active = False
    await _set_session_status(session, conv, "handed_off")
    who = "an agent" if by_user_id else "automation"
    add_activity(session, conv, f"AI paused ({reason.replace('_', ' ')}) by {who}")
    _outbox(
        session, conv, "conversation.updated", {"ai_active": False, "reason": reason}
    )
    return True


async def return_to_ai(
    session: AsyncSession, conv: Conversation, *, by_user_id: uuid.UUID
) -> None:
    """ "Return to AI" (HUMAN_HANDOFF.md §6). The AI answers the customer's next
    message, not immediately."""
    now = utcnow()
    conv.ai_active = True
    conv.assignee_id = None
    if conv.status != ConversationStatus.PENDING:
        conv.status = ConversationStatus.PENDING
        conv.status_changed_at = now
    await _set_session_status(session, conv, "active")
    add_activity(session, conv, "Conversation returned to the AI by an agent")
    _outbox(session, conv, "conversation.updated", {"ai_active": True})
