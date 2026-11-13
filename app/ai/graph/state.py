"""Graph state and views (docs/ai/LANGGRAPH_WORKFLOW.md §2).

No ORM objects in state: small frozen views, so state serialises cleanly and
nodes can't lazy-load from the database. Dependencies are not in state either;
they come from ``config["configurable"]["deps"]``.
"""

import operator
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


class ToolResult(TypedDict):
    tool: str
    ok: bool
    data: Any
    text: str
    sources: list[dict[str, Any]]
    error: str | None
    sufficient: bool


class Intent(TypedDict):
    name: str
    confidence: float
    language: str
    sentiment: Literal["positive", "neutral", "negative"]
    needs: list[str]
    search_query: str | None


@dataclass(frozen=True)
class AgentConfigView:
    id: uuid.UUID
    name: str
    is_active: bool
    provider: str
    chat_model: str
    classifier_model: str
    temperature: float
    system_prompt: str
    persona: dict[str, Any]
    guardrails: dict[str, Any]
    handoff_policy: dict[str, Any]
    enabled_tools: frozenset[str]
    allowed_actions: frozenset[str]
    tool_settings: dict[str, Any]
    knowledge_base_ids: tuple[uuid.UUID, ...]
    max_turns_before_handoff: int
    organization_name: str


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ConversationView:
    id: uuid.UUID | None
    channel: str
    labels: tuple[str, ...] = ()
    ai_turn_count: int = 0
    contact_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CustomerView:
    first_name: str | None
    verification_level: Literal["none", "channel", "verified"]
    verification_method: str | None
    has_email: bool = False
    has_phone: bool = False
    contact_type: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    # Own values, used only by the PII guardrail allow-list; never in prompts.
    own_values: tuple[str, ...] = ()


class AgentState(TypedDict, total=False):
    # identity of the run (set by the runner)
    run_id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    conversation_id: uuid.UUID | None
    trigger_message_id: uuid.UUID | None
    ai_agent_id: uuid.UUID
    deadline_ts: float
    started_at: datetime

    # loaded context
    agent: AgentConfigView
    conversation: ConversationView
    customer: CustomerView
    history: list[ChatTurn]
    summary: str
    facts: dict[str, Any]
    input_text: str
    latest_seq: int

    # decisions
    intent: Intent
    route: list[str]
    tool_results: Annotated[list[ToolResult], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    context_block: str
    citations: list[dict[str, Any]]
    context_sufficient: bool

    # output
    draft: str
    guardrail_flags: list[str]
    guardrail_attempts: int
    guardrail_feedback: str
    wants_handoff: bool
    outcome: Literal["reply", "handoff", "discard", "failed"]
    handoff_reason: str | None
    handoff_note: str
    reply_message_id: uuid.UUID | None

    # bookkeeping
    errors: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]


# --- Structured LLM outputs (docs/ai/LANGGRAPH_NODES.md §5, §12, §13) --------------

INTENTS = (
    "greeting",
    "smalltalk",
    "thanks",
    "product_question",
    "order_status",
    "refund_policy",
    "shipping_policy",
    "account_help",
    "complaint",
    "human_request",
    "other",
)


class IntentOutput(BaseModel):
    name: Literal[
        "greeting",
        "smalltalk",
        "thanks",
        "product_question",
        "order_status",
        "refund_policy",
        "shipping_policy",
        "account_help",
        "complaint",
        "human_request",
        "other",
    ]
    confidence: float = Field(ge=0, le=1)
    language: str = Field(
        description="BCP-47 code of the customer's language, e.g. en, ne, es"
    )
    sentiment: Literal["positive", "neutral", "negative"]
    needs: list[Literal["knowledge", "policy", "product", "order", "customer"]] = Field(
        description="Information needed to answer"
    )
    search_query: str | None = Field(
        default=None, description="Standalone rewrite of the question for search"
    )


class GenerationOutput(BaseModel):
    reply: str
    used_sources: list[int] = Field(
        default_factory=list, description="Numbers of the [n] sources used"
    )
    wants_handoff: bool = Field(
        default=False, description="True if a human should take over"
    )


class GroundingOutput(BaseModel):
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Claims in the reply not supported by the context",
    )


class SummaryOutput(BaseModel):
    summary: str = Field(
        description="Factual summary, at most 250 words, no PII beyond first names"
    )
