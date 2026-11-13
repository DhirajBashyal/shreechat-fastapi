"""When the AI answers (docs/ai/AI_ARCHITECTURE.md §3, ``should_run``).

The inbound pipeline (Phase 4) calls ``on_incoming_message`` for every new
incoming message (today: inline; later from the message.created outbox event
in the ``ai`` queue, debounced 2.5 s per conversation).
"""

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.runner import RunResult, run_for_message
from app.models import (
    AiAgent,
    AiAgentInbox,
    AiConversationSession,
    Contact,
    Conversation,
    Message,
    Organization,
    UsageRecord,
)
from app.models.enums import MessageDirection
from app.services.limits import plan_info


async def should_run(session: AsyncSession, message: Message) -> str | None:
    """None if the AI should answer, else the reason it doesn't."""
    if message.direction != MessageDirection.INCOMING or message.deleted_at is not None:
        return "not_incoming"
    conv = await session.get(Conversation, message.conversation_id)
    if conv is None:
        return "no_conversation"
    contact = await session.get(Contact, conv.contact_id)
    if contact is not None and contact.blocked:
        return "contact_blocked"
    link = (
        await session.execute(
            select(AiAgentInbox.ai_agent_id, AiAgentInbox.mode).where(
                AiAgentInbox.inbox_id == conv.inbox_id
            )
        )
    ).one_or_none()
    if link is None or link[1] != "autonomous":
        return "no_autonomous_agent"
    agent = await session.get(AiAgent, link[0])
    if agent is None or not agent.is_active or agent.deleted_at is not None:
        return "agent_inactive"
    if not conv.ai_active or conv.assignee_id is not None:
        return "human_owns_conversation"
    ai_session_status = await session.scalar(
        select(AiConversationSession.status).where(
            AiConversationSession.conversation_id == conv.id,
            AiConversationSession.ai_agent_id == agent.id,
        )
    )
    if ai_session_status not in (None, "active"):
        return "handed_off"
    org = await session.get(Organization, conv.organization_id)
    assert org is not None
    info = await plan_info(session, org)
    if "ai_agent" not in info.features:
        return "feature_not_in_plan"
    quota = info.limits.get("ai_responses_per_month")
    if quota is not None and agent.provider_credential_id is None:
        used = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                UsageRecord.organization_id == org.id,
                UsageRecord.metric == "ai_responses",
                UsageRecord.period_start == date.today().replace(day=1),
            )
        )
        if (used or 0) >= int(quota):
            return "quota_exhausted"
    return None


async def on_incoming_message(
    session: AsyncSession, message_id: uuid.UUID
) -> RunResult | str:
    """Run the AI for this message if it should answer; returns the run result
    or the reason it didn't run."""
    message = await session.get(Message, message_id)
    if message is None:
        return "message_not_found"
    reason = await should_run(session, message)
    if reason:
        return reason
    return await run_for_message(session, message.conversation_id, message.id)
