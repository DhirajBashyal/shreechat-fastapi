"""AI provider credentials and AI agent configuration.

docs/ai/AI_ROLES_AND_PERMISSIONS.md §2 (who may configure), §2.3 (escalation
rules, audit with old/new values), §3.3/§3.4 (actions and tool policies),
§6 (API). Secrets are encrypted at rest and never returned.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import policy
from app.ai.providers import SUPPORTED_PROVIDERS, model_allowed
from app.core.config import settings
from app.core.crypto import KEY_VERSION, encrypt_secret
from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.core.security import utcnow
from app.core.tenancy import TenantContext
from app.models import (
    AiAgent,
    AiAgentInbox,
    AiAgentKnowledgeBase,
    AiProviderCredential,
    Inbox,
    KnowledgeBase,
    Label,
)
from app.schemas.ai import (
    AgentCreate,
    AgentInboxesUpdate,
    AgentInboxLink,
    AgentKnowledgeBasesUpdate,
    AgentRead,
    AgentUpdate,
    CredentialCreate,
    CredentialRead,
)
from app.services.audit import audit
from app.services.limits import require_feature

# --- Credentials --------------------------------------------------------------------


async def list_credentials(
    session: AsyncSession, ctx: TenantContext
) -> list[CredentialRead]:
    rows = await session.scalars(
        select(AiProviderCredential)
        .where(AiProviderCredential.organization_id == ctx.organization_id)
        .order_by(AiProviderCredential.created_at)
    )
    return [CredentialRead.model_validate(r) for r in rows]


async def create_credential(
    session: AsyncSession, ctx: TenantContext, data: CredentialCreate, request: Request
) -> CredentialRead:
    await require_feature(session, ctx.organization_id, "byo_llm_keys")
    cred = AiProviderCredential(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        provider=data.provider,
        label=data.label,
        api_key_ciphertext=encrypt_secret(data.api_key),
        api_key_key_version=KEY_VERSION,
        api_key_last4=data.api_key[-4:].rjust(4, "*"),
        created_by=ctx.principal.user_id,
    )
    session.add(cred)
    audit(
        session,
        action="ai_credential.created",
        resource_type="ai_provider_credential",
        resource_id=cred.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={
            "provider": data.provider,
            "label": data.label,
            "last4": cred.api_key_last4,
        },
        request=request,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("A credential with this label already exists") from None
    await session.refresh(cred)
    return CredentialRead.model_validate(cred)


async def delete_credential(
    session: AsyncSession,
    ctx: TenantContext,
    credential_id: uuid.UUID,
    request: Request,
) -> None:
    cred = await session.scalar(
        select(AiProviderCredential).where(
            AiProviderCredential.id == credential_id,
            AiProviderCredential.organization_id == ctx.organization_id,
        )
    )
    if cred is None:
        raise NotFoundError()
    await session.delete(
        cred
    )  # agents using it fall back to the platform key (FK SET NULL)
    audit(
        session,
        action="ai_credential.deleted",
        resource_type="ai_provider_credential",
        resource_id=cred.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={"label": cred.label, "last4": cred.api_key_last4},
        request=request,
    )
    await session.commit()


# --- Agents -------------------------------------------------------------------------


async def _get_agent(
    session: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID
) -> AiAgent:
    agent = await session.scalar(
        select(AiAgent).where(
            AiAgent.id == agent_id,
            AiAgent.workspace_id == ctx.require_workspace(),
            AiAgent.deleted_at.is_(None),
        )
    )
    if agent is None:
        raise NotFoundError()
    return agent


async def _credential(
    session: AsyncSession, ctx: TenantContext, credential_id: uuid.UUID | None
) -> AiProviderCredential | None:
    if credential_id is None:
        return None
    cred = await session.scalar(
        select(AiProviderCredential).where(
            AiProviderCredential.id == credential_id,
            AiProviderCredential.organization_id == ctx.organization_id,
        )
    )
    if cred is None:
        raise NotFoundError("AI credential not found")
    return cred


async def _read(session: AsyncSession, ctx: TenantContext, agent: AiAgent) -> AgentRead:
    inboxes = (
        await session.execute(
            select(AiAgentInbox.inbox_id, AiAgentInbox.mode).where(
                AiAgentInbox.ai_agent_id == agent.id
            )
        )
    ).all()
    kb_ids = list(
        await session.scalars(
            select(AiAgentKnowledgeBase.knowledge_base_id).where(
                AiAgentKnowledgeBase.ai_agent_id == agent.id
            )
        )
    )
    cred = await _credential(session, ctx, agent.provider_credential_id)
    full = ctx.has("ai.agent.manage")
    return AgentRead(
        id=agent.id,
        workspace_id=agent.workspace_id,
        name=agent.name,
        description=agent.description,
        is_active=agent.is_active,
        provider=cred.provider if cred else settings.AI_PROVIDER,
        provider_credential_id=agent.provider_credential_id,
        chat_model=agent.chat_model,
        classifier_model=agent.classifier_model,
        temperature=float(agent.temperature),
        enabled_tools=list(agent.enabled_tools),
        allowed_actions=list(agent.allowed_actions),
        max_turns_before_handoff=agent.max_turns_before_handoff,
        inboxes=[AgentInboxLink(inbox_id=i, mode=m) for i, m in inboxes],  # type: ignore[arg-type]
        knowledge_base_ids=kb_ids,
        system_prompt=agent.system_prompt if full else None,
        persona=agent.persona if full else None,
        guardrails=agent.guardrails if full else None,
        handoff_policy=agent.handoff_policy if full else None,
        tool_settings=agent.tool_settings if full else None,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


async def list_agents(session: AsyncSession, ctx: TenantContext) -> list[AgentRead]:
    agents = await session.scalars(
        select(AiAgent)
        .where(
            AiAgent.workspace_id == ctx.require_workspace(),
            AiAgent.deleted_at.is_(None),
        )
        .order_by(AiAgent.created_at)
    )
    return [await _read(session, ctx, a) for a in agents]


async def get_agent(
    session: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID
) -> AgentRead:
    return await _read(session, ctx, await _get_agent(session, ctx, agent_id))


async def _apply(
    session: AsyncSession,
    ctx: TenantContext,
    agent: AiAgent,
    data: AgentCreate | AgentUpdate,
) -> dict[str, Any]:
    """Validate and apply the given fields; returns {field: [old, new]}."""
    fields = data.model_dump(exclude_unset=True)
    if "provider_credential_id" in fields:
        await _credential(session, ctx, fields["provider_credential_id"])
    cred = await _credential(
        session, ctx, fields.get("provider_credential_id", agent.provider_credential_id)
    )
    provider = cred.provider if cred else settings.AI_PROVIDER
    if provider not in SUPPORTED_PROVIDERS:
        raise ValidationFailedError(f"AI provider '{provider}' is not supported")
    for key in ("chat_model", "classifier_model"):
        model = fields.get(key)
        if model and not model_allowed(provider, model):
            raise policy.AgentConfigError(
                f"Model '{model}' is not allowed for {provider}",
                code="model_not_allowed",
            )

    if "allowed_actions" in fields:
        fields["allowed_actions"] = policy.validate_actions(fields["allowed_actions"])
    if "enabled_tools" in fields:
        fields["enabled_tools"] = policy.validate_tools(fields["enabled_tools"])
    if "tool_settings" in fields:
        labels = set(
            await session.scalars(
                select(Label.title).where(Label.workspace_id == agent.workspace_id)
            )
        )
        fields["tool_settings"] = policy.validate_tool_settings(
            fields["tool_settings"] or {}, labels
        )
    for key, model_cls in (
        ("handoff_policy", policy.HandoffPolicy),
        ("guardrails", policy.Guardrails),
        ("persona", policy.Persona),
    ):
        if key in fields:
            fields[key] = policy.parse_model(model_cls, fields[key] or {}, key)

    # Escalation rules look at the final config, not only the changed fields.
    policy.check_escalation(
        ctx.permissions,
        enabled_tools=fields.get("enabled_tools", list(agent.enabled_tools or [])),
        allowed_actions=fields.get(
            "allowed_actions", list(agent.allowed_actions or [])
        ),
    )

    changes: dict[str, Any] = {}
    for key, value in fields.items():
        old = getattr(agent, key, None)
        if key == "temperature" and value is not None:
            value = round(float(value), 2)
            old = float(old) if old is not None else None
        if old != value:
            changes[key] = [_jsonable(old), _jsonable(value)]
            setattr(agent, key, value)
    return changes


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _audit_changes(
    session: AsyncSession,
    ctx: TenantContext,
    agent: AiAgent,
    changes: dict[str, Any],
    request: Request,
) -> None:
    """One audit row per category (docs §2.3.2): the full prompt is stored, secrets never."""
    groups = {
        "ai_agent.tools_changed": {"enabled_tools", "tool_settings"},
        "ai_agent.actions_changed": {"allowed_actions"},
    }
    remaining = dict(changes)
    for action, keys in groups.items():
        part = {k: remaining.pop(k) for k in list(remaining) if k in keys}
        if part:
            audit(
                session,
                action=action,
                resource_type="ai_agent",
                resource_id=agent.id,
                actor_id=ctx.principal.user_id,
                organization_id=ctx.organization_id,
                workspace_id=agent.workspace_id,
                changes=part,
                request=request,
            )
    if remaining:
        audit(
            session,
            action="ai_agent.updated",
            resource_type="ai_agent",
            resource_id=agent.id,
            actor_id=ctx.principal.user_id,
            organization_id=ctx.organization_id,
            workspace_id=agent.workspace_id,
            changes=remaining,
            request=request,
        )


async def create_agent(
    session: AsyncSession, ctx: TenantContext, data: AgentCreate, request: Request
) -> AgentRead:
    agent = AiAgent(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ctx.require_workspace(),
        name=data.name,
        chat_model=data.chat_model or settings.AI_CHAT_MODEL,
        enabled_tools=["knowledge_search"],
        allowed_actions=list(policy.DEFAULT_ACTIONS),
        handoff_policy=policy.HandoffPolicy().model_dump(),
        guardrails=policy.Guardrails().model_dump(),
        persona=policy.Persona().model_dump(),
        tool_settings={},
    )
    if not data.chat_model and not model_allowed(
        settings.AI_PROVIDER, agent.chat_model
    ):
        raise policy.AgentConfigError(
            "Set chat_model: the platform default isn't allowed",
            code="model_not_allowed",
        )
    changes = await _apply(session, ctx, agent, data)
    session.add(agent)
    audit(
        session,
        action="ai_agent.created",
        resource_type="ai_agent",
        resource_id=agent.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=agent.workspace_id,
        changes={k: v[1] for k, v in changes.items()},
        request=request,
    )
    await session.commit()
    await session.refresh(agent)
    return await _read(session, ctx, agent)


async def update_agent(
    session: AsyncSession,
    ctx: TenantContext,
    agent_id: uuid.UUID,
    data: AgentUpdate,
    request: Request,
) -> AgentRead:
    agent = await _get_agent(session, ctx, agent_id)
    changes = await _apply(session, ctx, agent, data)
    if changes:
        _audit_changes(session, ctx, agent, changes, request)
        await session.commit()
        await session.refresh(agent)
    return await _read(session, ctx, agent)


async def delete_agent(
    session: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, request: Request
) -> None:
    agent = await _get_agent(session, ctx, agent_id)
    await session.execute(
        delete(AiAgentInbox).where(AiAgentInbox.ai_agent_id == agent.id)
    )
    agent.deleted_at = utcnow()
    agent.is_active = False
    audit(
        session,
        action="ai_agent.deleted",
        resource_type="ai_agent",
        resource_id=agent.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=agent.workspace_id,
        request=request,
    )
    await session.commit()


async def set_inboxes(
    session: AsyncSession,
    ctx: TenantContext,
    agent_id: uuid.UUID,
    data: AgentInboxesUpdate,
    request: Request,
) -> AgentRead:
    agent = await _get_agent(session, ctx, agent_id)
    wanted = {link.inbox_id: link.mode for link in data.inboxes}
    found = set(
        await session.scalars(
            select(Inbox.id).where(
                Inbox.id.in_(wanted), Inbox.workspace_id == agent.workspace_id
            )
        )
    )
    if set(wanted) - found:
        raise NotFoundError("Inbox not found")
    taken = set(
        await session.scalars(
            select(AiAgentInbox.inbox_id).where(
                AiAgentInbox.inbox_id.in_(wanted), AiAgentInbox.ai_agent_id != agent.id
            )
        )
    )
    if taken:
        raise ConflictError("An inbox already has another AI agent")
    await session.execute(
        delete(AiAgentInbox).where(AiAgentInbox.ai_agent_id == agent.id)
    )
    session.add_all(
        AiAgentInbox(
            organization_id=agent.organization_id,
            workspace_id=agent.workspace_id,
            ai_agent_id=agent.id,
            inbox_id=inbox_id,
            mode=mode,
        )
        for inbox_id, mode in wanted.items()
    )
    audit(
        session,
        action="ai_agent.updated",
        resource_type="ai_agent",
        resource_id=agent.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=agent.workspace_id,
        changes={"inboxes": {str(k): v for k, v in wanted.items()}},
        request=request,
    )
    await session.commit()
    return await _read(session, ctx, agent)


async def set_knowledge_bases(
    session: AsyncSession,
    ctx: TenantContext,
    agent_id: uuid.UUID,
    data: AgentKnowledgeBasesUpdate,
    request: Request,
) -> AgentRead:
    agent = await _get_agent(session, ctx, agent_id)
    wanted = set(data.knowledge_base_ids)
    found = set(
        await session.scalars(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id.in_(wanted),
                KnowledgeBase.workspace_id
                == agent.workspace_id,  # same workspace only (§2.3.1)
                KnowledgeBase.deleted_at.is_(None),
            )
        )
    )
    if wanted - found:
        raise NotFoundError("Knowledge base not found")
    await session.execute(
        delete(AiAgentKnowledgeBase).where(AiAgentKnowledgeBase.ai_agent_id == agent.id)
    )
    session.add_all(
        AiAgentKnowledgeBase(
            organization_id=agent.organization_id,
            workspace_id=agent.workspace_id,
            ai_agent_id=agent.id,
            knowledge_base_id=kb_id,
        )
        for kb_id in wanted
    )
    audit(
        session,
        action="ai_agent.updated",
        resource_type="ai_agent",
        resource_id=agent.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=agent.workspace_id,
        changes={"knowledge_base_ids": sorted(str(k) for k in wanted)},
        request=request,
    )
    await session.commit()
    return await _read(session, ctx, agent)
