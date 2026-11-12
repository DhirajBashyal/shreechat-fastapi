"""Conversations, messages, attachments, assignment history, participants, mentions."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

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
    ATTACHMENT_KIND,
    CONVERSATION_PRIORITY,
    CONVERSATION_STATUS,
    DELIVERY_STATUS,
    MESSAGE_CONTENT_TYPE,
    MESSAGE_DIRECTION,
    SENDER_KIND,
    AttachmentKind,
    ConversationPriority,
    ConversationStatus,
    DeliveryStatus,
    MessageContentType,
    MessageDirection,
    SenderKind,
)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="conversations_ws_id_uniq"),
        UniqueConstraint(
            "workspace_id", "display_number", name="conversations_display_uniq"
        ),
        CheckConstraint(
            "status <> 'snoozed' OR snoozed_until IS NOT NULL",
            name="conversations_snooze_chk",
        ),
        CheckConstraint(
            "jsonb_typeof(custom_attributes) = 'object'",
            name="conversations_custom_attrs_is_object",
        ),
        workspace_fk(),
        ws_fk("inbox_id", "inboxes"),
        ws_fk("contact_id", "contacts"),
        ws_fk("contact_identity_id", "contact_identities"),
        ws_fk("team_id", "teams", ondelete="SET NULL"),
        ws_fk("ai_agent_id", "ai_agents", ondelete="SET NULL"),
        Index(
            "conversations_list_idx",
            "workspace_id",
            "status",
            text("last_activity_at DESC"),
        ),
        Index(
            "conversations_inbox_status_idx",
            "workspace_id",
            "inbox_id",
            "status",
            "assignee_id",
        ),
        Index(
            "conversations_assignee_idx",
            "workspace_id",
            "assignee_id",
            "status",
            postgresql_where=text("assignee_id IS NOT NULL"),
        ),
        Index(
            "conversations_team_idx",
            "workspace_id",
            "team_id",
            "status",
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        Index("conversations_contact_idx", "contact_id", text("created_at DESC")),
        Index(
            "conversations_identity_open_idx",
            "contact_identity_id",
            postgresql_where=text("status <> 'resolved'"),
        ),
        Index(
            "conversations_snoozed_idx",
            "snoozed_until",
            postgresql_where=text("status = 'snoozed'"),
        ),
        Index(
            "conversations_waiting_idx",
            "workspace_id",
            "waiting_since",
            postgresql_where=text("waiting_since IS NOT NULL"),
        ),
        Index("conversations_org_idx", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contact_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Allocated with next_workspace_counter(workspace_id, 'conversation').
    display_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        CONVERSATION_STATUS, nullable=False, server_default=text("'open'")
    )
    priority: Mapped[ConversationPriority | None] = mapped_column(CONVERSATION_PRIORITY)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ai_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = created_at_col()
    last_incoming_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    agent_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime] = created_at_col()
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    additional_attributes: Mapped[dict[str, Any]] = jsonb_object()
    custom_attributes: Mapped[dict[str, Any]] = jsonb_object()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="messages_ws_id_uniq"),
        CheckConstraint(
            "content IS NULL OR char_length(content) <= 150000",
            name="messages_content_len_chk",
        ),
        CheckConstraint(
            "NOT is_private OR direction = 'outgoing'",
            name="messages_private_outgoing_chk",
        ),
        CheckConstraint(
            "(sender_kind = 'user' AND sender_user_id IS NOT NULL) OR "
            "(sender_kind = 'contact' AND sender_contact_id IS NOT NULL) OR "
            "(sender_kind = 'ai_agent' AND sender_ai_agent_id IS NOT NULL) OR "
            "(sender_kind IN ('system', 'automation'))",
            name="messages_sender_chk",
        ),
        workspace_fk(),
        ws_fk("conversation_id", "conversations"),
        ws_fk("inbox_id", "inboxes"),
        ws_fk("sender_contact_id", "contacts", ondelete="SET NULL"),
        ws_fk("sender_ai_agent_id", "ai_agents", ondelete="SET NULL"),
        ws_fk("in_reply_to_id", "messages", ondelete="SET NULL"),
        # Provider message ids are unique per inbox: guards against duplicate webhooks.
        Index(
            "messages_external_id_uniq",
            "inbox_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        # Client-generated ids make agent/widget sends idempotent on retry.
        Index(
            "messages_client_id_uniq",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        Index("messages_conversation_seq_idx", "conversation_id", "sequence_number"),
        Index("messages_ws_created_idx", "workspace_id", "created_at"),
        Index(
            "messages_ws_direction_created_idx",
            "workspace_id",
            "direction",
            "created_at",
        ),
        Index(
            "messages_content_trgm_idx",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
            postgresql_where=text("is_private = false"),
        ),
        Index("messages_org_idx", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        MESSAGE_DIRECTION, nullable=False
    )
    content_type: Mapped[MessageContentType] = mapped_column(
        MESSAGE_CONTENT_TYPE, nullable=False, server_default=text("'text'")
    )
    content: Mapped[str | None] = mapped_column(Text)
    content_attributes: Mapped[dict[str, Any]] = jsonb_object()
    is_private: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sender_kind: Mapped[SenderKind] = mapped_column(SENDER_KIND, nullable=False)
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    sender_contact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sender_ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        DELIVERY_STATUS, nullable=False, server_default=text("'sent'")
    )
    delivery_error: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(255))
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    # Strict ordering within a conversation; assigned by the database.
    sequence_number: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "file_id IS NOT NULL OR external_url IS NOT NULL OR kind IN ('location', 'fallback')",
            name="attachments_source_chk",
        ),
        ws_fk("message_id", "messages"),
        org_fk("file_id", "stored_files", ondelete="SET NULL"),
        workspace_fk(),
        Index("attachments_message_idx", "message_id"),
        Index("attachments_ws_idx", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[AttachmentKind] = mapped_column(ATTACHMENT_KIND, nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text)
    fallback_title: Mapped[str | None] = mapped_column(String(512))
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    meta: Mapped[dict[str, Any]] = jsonb_object()
    created_at: Mapped[datetime] = created_at_col()


class ConversationAssignment(Base):
    __tablename__ = "conversation_assignments"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('manual', 'auto_round_robin', 'auto_capacity', 'automation', 'ai_handoff', 'unassigned')",
            name="conversation_assignments_reason_chk",
        ),
        ws_fk("conversation_id", "conversations"),
        ws_fk("team_id", "teams", ondelete="SET NULL"),
        workspace_fk(),
        Index(
            "conversation_assignments_conv_idx",
            "conversation_id",
            text("created_at DESC"),
        ),
        Index(
            "conversation_assignments_assignee_idx",
            "workspace_id",
            "assignee_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    assigned_by_kind: Mapped[SenderKind] = mapped_column(SENDER_KIND, nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"
    __table_args__ = (
        ws_fk("conversation_id", "conversations"),
        workspace_fk(),
        Index("conversation_participants_user_idx", "user_id", "workspace_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_at_col()


class Mention(Base):
    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="mentions_uniq"),
        ws_fk("conversation_id", "conversations"),
        ws_fk("message_id", "messages"),
        workspace_fk(),
        Index("mentions_user_idx", "user_id", "workspace_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()


class ConversationLabel(Base):
    __tablename__ = "conversation_labels"
    __table_args__ = (
        ws_fk("conversation_id", "conversations"),
        ws_fk("label_id", "labels"),
        workspace_fk(),
        Index("conversation_labels_label_idx", "label_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    label_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = created_at_col()
