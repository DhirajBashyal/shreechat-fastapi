"""AI agent API (docs/ai/AI_ROLES_AND_PERMISSIONS.md §6).

/organizations/{org}/ai-credentials           ai.credentials.manage
/workspaces/{ws}/ai-agents                    list/read: member · write: ai.agent.manage
/workspaces/{ws}/ai-agents/{id}/playground    ai.playground.use
/workspaces/{ws}/conversations/{id}/ai-runs   ai.activity.view + conversation visible
/workspaces/{ws}/conversations/{id}/ai/pause|resume   conversation.ai_control + visible
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.ai.graph.state import ChatTurn, CustomerView
from app.ai.providers import ProviderNotConfiguredError, model_allow_list, model_allowed
from app.ai.runner import run_playground
from app.api.deps import CurrentUserDep, DbSession, WorkspaceContext, require_permission
from app.core.config import settings
from app.core.exceptions import APIError, NotFoundError
from app.core.tenancy import TenantContext
from app.models import AiAgent, AiRun, AiToolCall, KbChunk, KbDocument
from app.schemas.ai import (
    AgentCreate,
    AgentInboxesUpdate,
    AgentKnowledgeBasesUpdate,
    AgentRead,
    AgentUpdate,
    AiControlResponse,
    AiModelRead,
    AiModelsResponse,
    AiRunRead,
    CredentialCreate,
    CredentialRead,
    PlaygroundRequest,
    PlaygroundResponse,
    ToolCallRead,
)
from app.services import ai_agents as service
from app.services import conversation_ai
from app.services.visibility import get_visible_conversation

org_router = APIRouter()
ws_router = APIRouter()

ManageCredentials = Annotated[
    TenantContext, Depends(require_permission("ai.credentials.manage"))
]
ManageAgents = Annotated[
    TenantContext, Depends(require_permission("ai.agent.manage", scope="workspace"))
]
UsePlayground = Annotated[
    TenantContext, Depends(require_permission("ai.playground.use", scope="workspace"))
]
ViewActivity = Annotated[
    TenantContext, Depends(require_permission("ai.activity.view", scope="workspace"))
]
ControlAI = Annotated[
    TenantContext,
    Depends(require_permission("conversation.ai_control", scope="workspace")),
]


class AIProviderNotConfiguredError(APIError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ai_provider_not_configured"
    message = (
        "No AI provider key is configured (set AI_API_KEY or add an AI credential)"
    )


# --- Models ---------------------------------------------------------------------------


@org_router.get(
    "/ai-models", tags=["AI"], summary="Allowed models and platform AI status"
)
async def list_models(current: CurrentUserDep) -> AiModelsResponse:
    today = date.today()
    return AiModelsResponse(
        platform_provider=settings.AI_PROVIDER,
        platform_configured=bool(settings.AI_API_KEY),
        default_chat_model=settings.AI_CHAT_MODEL,
        models=[
            AiModelRead(
                provider=m.provider,
                model=m.model,
                context_window=m.context_window,
                supports_tools=m.supports_tools,
                deprecated_at=m.deprecated_at,
                selectable=model_allowed(m.provider, m.model, today=today),
            )
            for m in sorted(
                model_allow_list().values(), key=lambda m: (m.provider, m.model)
            )
        ],
    )


# --- Credentials ----------------------------------------------------------------------

CRED = "/organizations/{organization_id}/ai-credentials"


@org_router.get(CRED, tags=["AI"])
async def list_credentials(
    ctx: ManageCredentials, session: DbSession
) -> list[CredentialRead]:
    return await service.list_credentials(session, ctx)


@org_router.post(
    CRED,
    tags=["AI"],
    status_code=status.HTTP_201_CREATED,
    summary="Add a provider key (shown once, then last 4 only)",
)
async def create_credential(
    request: Request, body: CredentialCreate, ctx: ManageCredentials, session: DbSession
) -> CredentialRead:
    return await service.create_credential(session, ctx, body, request)


@org_router.delete(
    CRED + "/{credential_id}", tags=["AI"], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(
    request: Request,
    credential_id: uuid.UUID,
    ctx: ManageCredentials,
    session: DbSession,
) -> Response:
    await service.delete_credential(session, ctx, credential_id, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Agents ---------------------------------------------------------------------------

AG = "/{workspace_id}/ai-agents"


@ws_router.get(AG, summary="AI agents (prompts only with ai.agent.manage)")
async def list_agents(ctx: WorkspaceContext, session: DbSession) -> list[AgentRead]:
    return await service.list_agents(session, ctx)


@ws_router.post(AG, status_code=status.HTTP_201_CREATED)
async def create_agent(
    request: Request, body: AgentCreate, ctx: ManageAgents, session: DbSession
) -> AgentRead:
    return await service.create_agent(session, ctx, body, request)


@ws_router.get(AG + "/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID, ctx: WorkspaceContext, session: DbSession
) -> AgentRead:
    return await service.get_agent(session, ctx, agent_id)


@ws_router.patch(AG + "/{agent_id}")
async def update_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: AgentUpdate,
    ctx: ManageAgents,
    session: DbSession,
) -> AgentRead:
    return await service.update_agent(session, ctx, agent_id, body, request)


@ws_router.delete(AG + "/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    request: Request, agent_id: uuid.UUID, ctx: ManageAgents, session: DbSession
) -> Response:
    await service.delete_agent(session, ctx, agent_id, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@ws_router.put(
    AG + "/{agent_id}/inboxes", summary="Inboxes this agent answers (replaces the set)"
)
async def set_inboxes(
    request: Request,
    agent_id: uuid.UUID,
    body: AgentInboxesUpdate,
    ctx: ManageAgents,
    session: DbSession,
) -> AgentRead:
    return await service.set_inboxes(session, ctx, agent_id, body, request)


@ws_router.put(
    AG + "/{agent_id}/knowledge-bases",
    summary="Knowledge bases this agent searches (replaces the set)",
)
async def set_knowledge_bases(
    request: Request,
    agent_id: uuid.UUID,
    body: AgentKnowledgeBasesUpdate,
    ctx: ManageAgents,
    session: DbSession,
) -> AgentRead:
    return await service.set_knowledge_bases(session, ctx, agent_id, body, request)


@ws_router.post(
    AG + "/{agent_id}/playground",
    summary="Ask the agent a test question; nothing is sent or saved",
)
async def playground(
    agent_id: uuid.UUID, body: PlaygroundRequest, ctx: UsePlayground, session: DbSession
) -> PlaygroundResponse:
    agent = await session.scalar(
        select(AiAgent).where(
            AiAgent.id == agent_id,
            AiAgent.workspace_id == ctx.require_workspace(),
            AiAgent.deleted_at.is_(None),
        )
    )
    if agent is None:
        raise NotFoundError()
    customer = CustomerView(
        first_name=body.customer_name,
        verification_level=body.customer_verification,
        verification_method="playground",
    )
    try:
        final = await run_playground(
            session,
            organization_id=ctx.organization_id,
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            message=body.message,
            history=[ChatTurn(t.role, t.content) for t in body.history],
            customer=customer,
        )
    except ProviderNotConfiguredError as exc:
        raise AIProviderNotConfiguredError(str(exc)) from exc
    return playground_response(dict(final))


def playground_response(final: dict[str, Any]) -> PlaygroundResponse:
    outcome = final.get("outcome") or "failed"
    tools = [c["tool_name"] for c in final.get("tool_calls", [])]
    route = (
        ["human_handoff"]
        if outcome == "handoff"
        else (tools or ["response_generation"])
    )
    return PlaygroundResponse(
        outcome=outcome,
        reply=final.get("draft") or None,
        handoff_reason=final.get("handoff_reason") if outcome == "handoff" else None,
        intent=final.get("intent"),
        route=route,
        citations=final.get("citations", []),
        context_sufficient=final.get("context_sufficient"),
        guardrail_flags=final.get("guardrail_flags", []),
        tool_calls=final.get("tool_calls", []),
        usage=final.get("usage", []),
        errors=final.get("errors", []),
    )


# --- AI on a conversation -------------------------------------------------------------

CONV = "/{workspace_id}/conversations/{conversation_id}"


@ws_router.get(
    CONV + "/ai-runs",
    summary="Why the AI answered: runs, sources, tool calls, handoff reasons",
)
async def list_ai_runs(
    conversation_id: uuid.UUID, ctx: ViewActivity, session: DbSession
) -> list[AiRunRead]:
    conv = await get_visible_conversation(session, ctx, conversation_id)
    runs = list(
        await session.scalars(
            select(AiRun)
            .where(AiRun.conversation_id == conv.id)
            .order_by(AiRun.started_at.desc())
            .limit(50)
        )
    )
    # Chunk text only for people who manage knowledge; others see titles (§2.3.3).
    show_text = ctx.has("ai.knowledge.manage")
    chunk_ids = {cid for r in runs for cid in r.retrieved_chunk_ids}
    chunks: dict[uuid.UUID, tuple[str, str]] = {}
    if chunk_ids:
        for cid, content, title in await session.execute(
            select(KbChunk.id, KbChunk.content, KbDocument.title)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(KbChunk.id.in_(chunk_ids))
        ):
            chunks[cid] = (title, content)
    calls: dict[uuid.UUID, list[ToolCallRead]] = {}
    if runs:
        for call in await session.scalars(
            select(AiToolCall)
            .where(AiToolCall.run_id.in_([r.id for r in runs]))
            .order_by(AiToolCall.created_at)
        ):
            calls.setdefault(call.run_id, []).append(ToolCallRead.model_validate(call))
    out = []
    for r in runs:
        sources = []
        for cid in r.retrieved_chunk_ids:
            title, content = chunks.get(cid, ("(deleted)", ""))
            sources.append(
                {
                    "chunk_id": str(cid),
                    "title": title,
                    **({"content": content} if show_text else {}),
                }
            )
        out.append(
            AiRunRead(
                id=r.id,
                status=str(r.status),
                intent=r.intent,
                route=r.route,
                handoff_reason=r.handoff_reason,
                model=r.model,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                latency_ms=r.latency_ms,
                guardrail_flags=list(r.guardrail_flags),
                error=r.error,
                sources=sources,
                tool_calls=calls.get(r.id, []),
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
        )
    return out


@ws_router.post(CONV + "/ai/pause", summary="Pause the AI on this conversation")
async def pause_ai(
    conversation_id: uuid.UUID, ctx: ControlAI, session: DbSession
) -> AiControlResponse:
    conv = await get_visible_conversation(session, ctx, conversation_id, lock=True)
    await conversation_ai.switch_off_ai(
        session, conv, reason="agent_paused", by_user_id=ctx.principal.user_id
    )
    await session.commit()
    return AiControlResponse(
        conversation_id=conv.id, ai_active=conv.ai_active, status=str(conv.status)
    )


@ws_router.post(
    CONV + "/ai/resume",
    summary="Return the conversation to the AI (answers the next message)",
)
async def resume_ai(
    conversation_id: uuid.UUID, ctx: ControlAI, session: DbSession
) -> AiControlResponse:
    conv = await get_visible_conversation(session, ctx, conversation_id, lock=True)
    if not conv.ai_active:
        await conversation_ai.return_to_ai(
            session, conv, by_user_id=ctx.principal.user_id
        )
        await session.commit()
    return AiControlResponse(
        conversation_id=conv.id, ai_active=conv.ai_active, status=str(conv.status)
    )
