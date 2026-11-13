"""AI credentials, agents, playground and AI activity (docs/ai/AI_ROLES_AND_PERMISSIONS.md §6)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Credentials ------------------------------------------------------------------


class CredentialCreate(BaseModel):
    provider: Literal["openai", "gemini", "anthropic"]
    label: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=500)


class CredentialRead(BaseModel):
    """The key itself is never returned, only its last 4 characters."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    label: str
    api_key_last4: str
    created_at: datetime


class AiModelRead(BaseModel):
    provider: str
    model: str
    context_window: int
    supports_tools: bool
    deprecated_at: date | None
    # False once deprecated_at has passed: can't be newly selected.
    selectable: bool


class AiModelsResponse(BaseModel):
    """What the agent settings form offers (config/llm_models.yaml)."""

    platform_provider: str
    # False when the server has no AI_API_KEY: agents without their own key
    # can't answer and the playground returns 503.
    platform_configured: bool
    default_chat_model: str
    models: list[AiModelRead]


# --- Agents -----------------------------------------------------------------------


class AgentBase(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    provider_credential_id: uuid.UUID | None = None
    chat_model: str | None = Field(default=None, max_length=128)
    classifier_model: str | None = Field(default=None, max_length=128)
    temperature: Decimal | None = Field(default=None, ge=0, le=2)
    system_prompt: str | None = Field(default=None, max_length=20_000)
    persona: dict[str, Any] | None = None
    guardrails: dict[str, Any] | None = None
    handoff_policy: dict[str, Any] | None = None
    enabled_tools: list[str] | None = None
    allowed_actions: list[str] | None = None
    tool_settings: dict[str, Any] | None = None
    max_turns_before_handoff: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None


class AgentCreate(AgentBase):
    name: str = Field(min_length=1, max_length=255)


class AgentUpdate(AgentBase):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class AgentInboxLink(BaseModel):
    inbox_id: uuid.UUID
    mode: Literal["autonomous", "assist_only"] = "autonomous"


class AgentInboxesUpdate(BaseModel):
    inboxes: list[AgentInboxLink] = Field(max_length=100)


class AgentKnowledgeBasesUpdate(BaseModel):
    knowledge_base_ids: list[uuid.UUID] = Field(max_length=100)


class AgentRead(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    provider: str
    provider_credential_id: uuid.UUID | None
    chat_model: str
    classifier_model: str | None
    temperature: float
    enabled_tools: list[str]
    allowed_actions: list[str]
    max_turns_before_handoff: int
    inboxes: list[AgentInboxLink]
    knowledge_base_ids: list[uuid.UUID]
    # Only for users with ai.agent.manage (null otherwise).
    system_prompt: str | None = None
    persona: dict[str, Any] | None = None
    guardrails: dict[str, Any] | None = None
    handoff_policy: dict[str, Any] | None = None
    tool_settings: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


# --- Playground -------------------------------------------------------------------


class PlaygroundTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class PlaygroundRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[PlaygroundTurn] = Field(default_factory=list, max_length=20)
    # Simulated customer verification, to test tools that need it.
    customer_verification: Literal["none", "channel", "verified"] = "none"
    customer_name: str | None = Field(default=None, max_length=100)


class PlaygroundResponse(BaseModel):
    outcome: Literal["reply", "handoff", "discard", "failed"]
    reply: str | None
    handoff_reason: str | None
    intent: dict[str, Any] | None
    route: list[str]
    citations: list[dict[str, Any]]
    context_sufficient: bool | None
    guardrail_flags: list[str]
    tool_calls: list[dict[str, Any]]
    usage: list[dict[str, Any]]
    errors: list[dict[str, Any]]


# --- AI activity ------------------------------------------------------------------


class ToolCallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool_name: str
    arguments: dict[str, Any]
    result_excerpt: str | None
    policy_decision: str
    denial_reason: str | None
    succeeded: bool
    latency_ms: int | None


class AiRunRead(BaseModel):
    id: uuid.UUID
    status: str
    intent: str | None
    route: str | None
    handoff_reason: str | None
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int | None
    guardrail_flags: list[str]
    error: str | None
    sources: list[dict[str, Any]]
    tool_calls: list[ToolCallRead]
    started_at: datetime
    finished_at: datetime | None


class AiControlResponse(BaseModel):
    conversation_id: uuid.UUID
    ai_active: bool
    status: str
