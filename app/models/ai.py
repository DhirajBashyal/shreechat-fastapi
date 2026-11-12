"""AI: provider credentials, agents, knowledge bases (RAG), memory, runs, tool calls.

Embeddings come from the local embedding service (embedding_service/), so the
vector size and default model follow app.core.constants instead of the
1536-dim OpenAI defaults in the original schema docs.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL_NAME
from app.models.base import (
    Base,
    created_at_col,
    deleted_at_col,
    jsonb_object,
    org_fk,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)
from app.models.enums import (
    AI_RUN_STATUS,
    KB_DOCUMENT_STATUS,
    AiRunStatus,
    KbDocumentStatus,
)

_DEFAULT_EMBEDDING_MODEL = text(f"'{EMBEDDING_MODEL_NAME}'")


class AiProviderCredential(Base):
    __tablename__ = "ai_provider_credentials"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "label", name="ai_provider_credentials_label_uniq"
        ),
        UniqueConstraint(
            "organization_id", "id", name="ai_provider_credentials_org_id_uniq"
        ),
        CheckConstraint(
            "provider IN ('openai', 'anthropic', 'azure_openai', 'gemini', 'openai_compatible')",
            name="ai_provider_credentials_provider_chk",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AiAgent(Base):
    __tablename__ = "ai_agents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="ai_agents_ws_id_uniq"),
        CheckConstraint(
            "temperature BETWEEN 0 AND 2", name="ai_agents_temperature_chk"
        ),
        CheckConstraint(
            "allowed_actions <@ ARRAY['reply', 'handoff', 'add_label', 'set_priority', "
            "'resolve', 'snooze', 'update_contact', 'add_private_note']::text[] "
            "AND 'handoff' = ANY(allowed_actions)",
            name="ai_agents_actions_chk",
        ),
        CheckConstraint(
            "jsonb_typeof(tool_settings) = 'object'",
            name="ai_agents_tool_settings_is_object",
        ),
        workspace_fk(),
        org_fk(
            "provider_credential_id",
            "ai_provider_credentials",
            ondelete="SET NULL",
            name="ai_agents_credential_fk",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    provider_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    chat_model: Mapped[str] = mapped_column(String(128), nullable=False)
    classifier_model: Mapped[str | None] = mapped_column(String(128))
    embedding_model: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=_DEFAULT_EMBEDDING_MODEL
    )
    temperature: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("0.2")
    )
    system_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    persona: Mapped[dict[str, Any]] = jsonb_object()
    guardrails: Mapped[dict[str, Any]] = jsonb_object()
    handoff_policy: Mapped[dict[str, Any]] = jsonb_object()
    enabled_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY['knowledge_search']")
    )
    max_turns_before_handoff: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY['reply', 'handoff']")
    )
    tool_settings: Mapped[dict[str, Any]] = jsonb_object()
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class AiAgentInbox(Base):
    """Which AI agent answers an inbox (at most one agent per inbox)."""

    __tablename__ = "ai_agent_inboxes"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('autonomous', 'assist_only')", name="ai_agent_inboxes_mode_chk"
        ),
        ws_fk("ai_agent_id", "ai_agents"),
        ws_fk("inbox_id", "inboxes"),
        workspace_fk(),
        Index("ai_agent_inboxes_agent_idx", "ai_agent_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'autonomous'")
    )
    created_at: Mapped[datetime] = created_at_col()


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="knowledge_bases_ws_id_uniq"),
        # kb_chunks.embedding is a fixed-size column, so every KB must match it.
        CheckConstraint(
            f"embedding_dimensions = {EMBEDDING_DIMENSIONS}",
            name="knowledge_bases_dims_chk",
        ),
        workspace_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=_DEFAULT_EMBEDDING_MODEL
    )
    embedding_dimensions: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(EMBEDDING_DIMENSIONS))
    )
    chunk_size_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("500")
    )
    chunk_overlap_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class AiAgentKnowledgeBase(Base):
    __tablename__ = "ai_agent_knowledge_bases"
    __table_args__ = (
        ws_fk("ai_agent_id", "ai_agents"),
        ws_fk("knowledge_base_id", "knowledge_bases"),
        workspace_fk(),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)


class KbDocument(Base):
    """A source document (URL, file, text, FAQ) that is chunked and embedded."""

    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="kb_documents_ws_id_uniq"),
        CheckConstraint(
            "(source_type = 'url' AND source_url IS NOT NULL) OR "
            "(source_type = 'file' AND file_id IS NOT NULL) OR "
            "(source_type IN ('text', 'faq'))",
            name="kb_documents_source_chk",
        ),
        ws_fk("knowledge_base_id", "knowledge_bases"),
        org_fk("file_id", "stored_files", ondelete="SET NULL"),
        workspace_fk(),
        Index(
            "kb_documents_kb_idx",
            "knowledge_base_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "kb_documents_url_uniq",
            "knowledge_base_id",
            "source_url",
            unique=True,
            postgresql_where=text("source_url IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[KbDocumentStatus] = mapped_column(
        KB_DOCUMENT_STATUS, nullable=False, server_default=text("'pending'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Column "metadata"; the attribute is renamed because declarative reserves it.
    document_metadata: Mapped[dict[str, Any]] = jsonb_object("metadata")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class KbChunk(Base):
    """An embedded chunk of a document: the vector store for RAG.

    Hybrid search uses ``embedding`` (HNSW, cosine) plus ``search_vector`` (FTS).
    """

    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="kb_chunks_position_uniq"),
        ws_fk("document_id", "kb_documents"),
        ws_fk("knowledge_base_id", "knowledge_bases"),
        workspace_fk(),
        Index(
            "kb_chunks_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("kb_chunks_kb_idx", "knowledge_base_id"),
        Index("kb_chunks_fts_idx", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=False
    )
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Column "metadata"; the attribute is renamed because declarative reserves it.
    chunk_metadata: Mapped[dict[str, Any]] = jsonb_object("metadata")
    # Maintained by PostgreSQL (generated column); never written by the app.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    created_at: Mapped[datetime] = created_at_col()


class AiConversationSession(Base):
    """Long-lived AI memory for one conversation (summary, facts, LangGraph thread)."""

    __tablename__ = "ai_conversation_sessions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "ai_agent_id", name="ai_sessions_conversation_uniq"
        ),
        UniqueConstraint("thread_id", name="ai_sessions_thread_uniq"),
        UniqueConstraint("workspace_id", "id", name="ai_sessions_ws_id_uniq"),
        CheckConstraint(
            "status IN ('active', 'handed_off', 'closed')",
            name="ai_sessions_status_chk",
        ),
        CheckConstraint(
            "customer_verification_level IN ('none', 'channel', 'verified')",
            name="ai_sessions_verification_chk",
        ),
        ws_fk("conversation_id", "conversations"),
        ws_fk("ai_agent_id", "ai_agents"),
        workspace_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    summarized_through_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    facts: Mapped[dict[str, Any]] = jsonb_object()
    turn_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_verification_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'none'")
    )
    customer_verified_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AiRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "trigger_message_id", name="ai_runs_trigger_uniq"
        ),
        ws_fk("session_id", "ai_conversation_sessions"),
        ws_fk("conversation_id", "conversations"),
        ws_fk("trigger_message_id", "messages", ondelete="SET NULL"),
        ws_fk("reply_message_id", "messages", ondelete="SET NULL"),
        workspace_fk(),
        Index("ai_runs_conversation_idx", "conversation_id", text("started_at DESC")),
        Index("ai_runs_ws_started_idx", "workspace_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[AiRunStatus] = mapped_column(
        AI_RUN_STATUS, nullable=False, server_default=text("'running'")
    )
    intent: Mapped[str | None] = mapped_column(String(64))
    route: Mapped[str | None] = mapped_column(String(64))
    handoff_reason: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retrieved_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'")
    )
    guardrail_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiToolCall(Base):
    __tablename__ = "ai_tool_calls"
    __table_args__ = (
        CheckConstraint(
            "policy_decision IN ('allowed', 'denied', 'needs_verification', 'needs_approval')",
            name="ai_tool_calls_decision_chk",
        ),
        Index("ai_tool_calls_run_idx", "run_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = jsonb_object()
    result_excerpt: Mapped[str | None] = mapped_column(Text)
    policy_decision: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'allowed'")
    )
    denial_reason: Mapped[str | None] = mapped_column(String(64))
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()
