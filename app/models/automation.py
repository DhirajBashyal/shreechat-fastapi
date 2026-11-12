"""Automation rules and their execution log."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
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
    jsonb_array,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="automation_rules_ws_id_uniq"),
        CheckConstraint(
            "event_name IN ('conversation.created', 'conversation.updated', "
            "'conversation.opened', 'conversation.resolved', 'message.created')",
            name="automation_rules_event_chk",
        ),
        CheckConstraint(
            "jsonb_typeof(conditions) = 'array'",
            name="automation_rules_conditions_is_array",
        ),
        CheckConstraint(
            "jsonb_typeof(actions) = 'array' AND jsonb_array_length(actions) > 0",
            name="automation_rules_actions_is_array",
        ),
        workspace_fk(),
        Index(
            "automation_rules_lookup_idx",
            "workspace_id",
            "event_name",
            "position",
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[list[Any]] = jsonb_array()
    actions: Mapped[list[Any]] = jsonb_array()
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AutomationRuleExecution(Base):
    __tablename__ = "automation_rule_executions"
    __table_args__ = (
        UniqueConstraint(
            "rule_id", "outbox_event_id", name="automation_executions_once_uniq"
        ),
        ws_fk("rule_id", "automation_rules"),
        ws_fk("conversation_id", "conversations"),
        workspace_fk(),
        Index("automation_executions_rule_idx", "rule_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outbox_event_id: Mapped[int | None] = mapped_column(BigInteger)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actions_result: Mapped[list[Any]] = jsonb_array()
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
