"""Running the agent graph (docs/ai/LANGGRAPH_WORKFLOW.md §5, §6).

Live runs: one ``ai_runs`` row per (session, trigger message), so a trigger
delivered twice is a no-op; a 45 s deadline; tool calls stored in
``ai_tool_calls``; on failure, handoff with reason ai_unavailable / ai_timeout
when the agent's policy says so. A whole run is never retried (a retry could
double-reply); response_delivery's checks make re-execution safe anyway.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph.build import graph
from app.ai.graph.nodes import REASON_TEXT, AgentDeps, DeadlineExceededError
from app.ai.graph.repo import DbRepo, PlaygroundRepo
from app.ai.graph.state import AgentState, ChatTurn, CustomerView
from app.ai.providers import LLM, T, Usage, build_llm
from app.core import metrics
from app.core.config import settings
from app.core.logger import get_logger
from app.core.security import utcnow
from app.core.tenancy import bind_tenant
from app.models import (
    AiAgent,
    AiAgentInbox,
    AiConversationSession,
    AiRun,
    AiToolCall,
    Conversation,
)
from app.models.enums import AiRunStatus, SenderKind
from app.services import conversation_ai

logger = get_logger()

STATUS_FOR_OUTCOME = {
    "reply": AiRunStatus.REPLIED,
    "handoff": AiRunStatus.HANDED_OFF,
    "discard": AiRunStatus.DISCARDED,
    "failed": AiRunStatus.FAILED,
}


class LazyLLM:
    """Built on first use, after tenant_context has loaded the agent's credential."""

    def __init__(self, repo: DbRepo) -> None:
        self._repo = repo
        self._llm: LLM | None = None
        self.provider = settings.AI_PROVIDER

    async def structured(
        self, schema: type[T], system: str, messages: list[Any], **kw: Any
    ) -> tuple[T, Usage]:
        if self._llm is None:
            self._llm = build_llm(self._repo.credential)
            self.provider = self._llm.provider
        return await self._llm.structured(schema, system, messages, **kw)

    async def moderate(self, text: str) -> list[str] | None:
        if self._llm is None:
            self._llm = build_llm(self._repo.credential)
            self.provider = self._llm.provider
        return await self._llm.moderate(text)


@dataclass
class RunResult:
    outcome: str
    run_id: uuid.UUID | None = None
    handoff_reason: str | None = None
    duplicate: bool = False


def _route(state: AgentState) -> str:
    if state.get("outcome") == "handoff":
        return "handoff"
    tools = sorted({c["tool_name"] for c in state.get("tool_calls", [])})
    return "+".join(tools)[:64] if tools else "direct"


async def _invoke(state: AgentState, deps: AgentDeps) -> AgentState:
    timeout = max(1.0, state["deadline_ts"] - time.time() + 5)
    result = await asyncio.wait_for(
        graph().ainvoke(
            state, config={"configurable": {"deps": deps}, "recursion_limit": 25}
        ),
        timeout=timeout,
    )
    return result  # type: ignore[no-any-return]


# --- Live conversations -------------------------------------------------------------


async def _agent_for(session: AsyncSession, conv: Conversation) -> uuid.UUID | None:
    if conv.ai_agent_id:
        return conv.ai_agent_id
    return await session.scalar(
        select(AiAgentInbox.ai_agent_id).where(
            AiAgentInbox.inbox_id == conv.inbox_id, AiAgentInbox.mode == "autonomous"
        )
    )


async def _session_row(
    session: AsyncSession, conv: Conversation, agent_id: uuid.UUID
) -> uuid.UUID:
    await session.execute(
        pg_insert(AiConversationSession)
        .values(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            conversation_id=conv.id,
            ai_agent_id=agent_id,
            thread_id=f"org:{conv.organization_id}:conv:{conv.id}:agent:{agent_id}",
        )
        .on_conflict_do_nothing(constraint="ai_sessions_conversation_uniq")
    )
    session_id = await session.scalar(
        select(AiConversationSession.id).where(
            AiConversationSession.conversation_id == conv.id,
            AiConversationSession.ai_agent_id == agent_id,
        )
    )
    assert session_id is not None
    return session_id


async def run_for_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    trigger_message_id: uuid.UUID,
    *,
    llm: LLM | None = None,
) -> RunResult:
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return RunResult("discard")
    await bind_tenant(session, conv.organization_id)
    agent_id = await _agent_for(session, conv)
    if agent_id is None:
        return RunResult("discard")
    agent = await session.get(AiAgent, agent_id)
    session_id = await _session_row(session, conv, agent_id)
    run_id = await session.scalar(
        pg_insert(AiRun)
        .values(
            organization_id=conv.organization_id,
            workspace_id=conv.workspace_id,
            session_id=session_id,
            conversation_id=conv.id,
            trigger_message_id=trigger_message_id,
            model=agent.chat_model if agent else "unknown",
        )
        .on_conflict_do_nothing(constraint="ai_runs_trigger_uniq")
        .returning(AiRun.id)
    )
    await session.commit()
    if run_id is None:
        return RunResult("discard", duplicate=True)  # this trigger was already handled

    repo = DbRepo(
        session, conv.organization_id, conv.workspace_id, session_id=session_id
    )
    deps = AgentDeps(repo=repo, llm=llm or LazyLLM(repo))
    started = time.time()
    state: AgentState = {
        "run_id": run_id,
        "organization_id": conv.organization_id,
        "workspace_id": conv.workspace_id,
        "conversation_id": conv.id,
        "trigger_message_id": trigger_message_id,
        "ai_agent_id": agent_id,
        "deadline_ts": started + settings.AI_RUN_DEADLINE_SECONDS,
        "started_at": utcnow(),
    }
    try:
        final = await _invoke(state, deps)
    except Exception as exc:
        await session.rollback()
        reason = (
            "ai_timeout"
            if isinstance(exc, (asyncio.TimeoutError, DeadlineExceededError))
            else "ai_unavailable"
        )
        logger.warning(
            "AI run failed", extra={"run_id": str(run_id), "error": repr(exc)[:300]}
        )
        await _finish(
            session,
            run_id,
            {},
            started,
            error=repr(exc)[:1000],
            status=AiRunStatus.FAILED,
        )
        handed = await _fallback_handoff(
            session, conversation_id, agent_id, run_id, reason
        )
        return RunResult(
            "handoff" if handed else "failed", run_id, reason if handed else None
        )
    await _finish(session, run_id, final, started)
    return RunResult(
        final.get("outcome", "failed"), run_id, final.get("handoff_reason")
    )


async def _finish(
    session: AsyncSession,
    run_id: uuid.UUID,
    final: AgentState | dict[str, Any],
    started: float,
    *,
    error: str | None = None,
    status: AiRunStatus | None = None,
) -> None:
    run = await session.get(AiRun, run_id)
    if run is None:
        return
    usage = final.get("usage", [])
    run.status = status or STATUS_FOR_OUTCOME.get(
        final.get("outcome", "failed"), AiRunStatus.FAILED
    )
    intent = final.get("intent") or {}
    run.intent = (intent.get("name") or None) if intent else None
    run.route = _route(final) if final else None  # type: ignore[arg-type]
    run.handoff_reason = (
        (final.get("handoff_reason") or None)
        if final.get("outcome") == "handoff"
        else None
    )
    models = [u["model"] for u in usage if u.get("node") == "response_generation"]
    if models:
        run.model = models[-1]
    run.input_tokens = sum(int(u.get("input_tokens", 0)) for u in usage)
    run.output_tokens = sum(int(u.get("output_tokens", 0)) for u in usage)
    run.latency_ms = int((time.time() - started) * 1000)
    run.retrieved_chunk_ids = [
        uuid.UUID(c["chunk_id"]) for c in final.get("citations", [])
    ]
    run.guardrail_flags = list(final.get("guardrail_flags", []))
    run.reply_message_id = final.get("reply_message_id")
    errors = [e["error"] for e in final.get("errors", [])]
    run.error = error or ("; ".join(errors)[:1000] if errors else None)
    run.finished_at = utcnow()
    metrics.AI_RUNS.inc(status=str(run.status))
    if run.handoff_reason:
        metrics.AI_HANDOFFS.inc(reason=run.handoff_reason)
    metrics.AI_RUN_LATENCY.observe(time.time() - started)
    for u in usage:
        metrics.AI_TOKENS.inc(
            int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0)),
            model=u.get("model", "unknown"),
        )
    session.add_all(
        AiToolCall(organization_id=run.organization_id, run_id=run.id, **call)
        for call in final.get("tool_calls", [])
    )
    await session.commit()


async def _fallback_handoff(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: str,
) -> bool:
    agent = await session.get(AiAgent, agent_id)
    if (
        agent is None
        or (agent.handoff_policy or {}).get("on_ai_error", "handoff") != "handoff"
    ):
        return False
    conv = await conversation_ai.lock_conversation(session, conversation_id)
    if conv is None or not conv.ai_active or conv.assignee_id is not None:
        await session.rollback()
        return False
    policy = agent.handoff_policy or {}
    message = (policy.get("handoff_message") or {}).get("en")
    if message:
        conversation_ai.add_message(
            session,
            conv,
            content=message,
            sender_kind=SenderKind.AI_AGENT,
            sender_ai_agent_id=agent.id,
            client_message_id=f"ai:{run_id}",
        )
    if policy.get("post_internal_note", True):
        conversation_ai.add_message(
            session,
            conv,
            content=f"🤖 AI handed off: {REASON_TEXT.get(reason, reason)}.",
            sender_kind=SenderKind.AI_AGENT,
            sender_ai_agent_id=agent.id,
            is_private=True,
            client_message_id=f"ai-note:{run_id}",
        )
    await conversation_ai.handoff_to_human(
        session, conv, reason=reason, ai_agent_id=agent.id
    )
    run = await session.get(AiRun, run_id)
    if run is not None:
        run.handoff_reason = reason
    metrics.AI_HANDOFFS.inc(reason=reason)
    await session.commit()
    return True


# --- Playground ---------------------------------------------------------------------


async def run_playground(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    message: str,
    history: list[ChatTurn],
    customer: CustomerView,
    llm: LLM | None = None,
) -> AgentState:
    """Same graph, simulated conversation, nothing written (no messages, memory,
    usage or ai_runs rows). Raises ProviderNotConfiguredError if no LLM key."""
    repo = PlaygroundRepo(
        session,
        organization_id,
        workspace_id,
        playground=True,
        message=message,
        turns=history,
        customer=customer,
    )
    deps = AgentDeps(repo=repo, llm=llm or LazyLLM(repo))
    state: AgentState = {
        "run_id": uuid.uuid4(),
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "conversation_id": None,
        "trigger_message_id": None,
        "ai_agent_id": agent_id,
        "deadline_ts": time.time() + settings.AI_RUN_DEADLINE_SECONDS,
        "started_at": utcnow(),
    }
    return await _invoke(state, deps)
