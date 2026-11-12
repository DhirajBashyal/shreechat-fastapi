"""Labels, custom attribute definitions, canned responses, macros, notifications."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
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
    deleted_at_col,
    jsonb_array,
    jsonb_object,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)
from app.models.enums import (
    ATTRIBUTE_SCOPE,
    ATTRIBUTE_VALUE_TYPE,
    AttributeScope,
    AttributeValueType,
)


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="labels_ws_id_uniq"),
        CheckConstraint("title ~ '^[a-z0-9_-]+$'", name="labels_title_format_chk"),
        workspace_fk(),
        Index("labels_title_per_ws_uniq", "workspace_id", "title", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str] = mapped_column(
        String(9), nullable=False, server_default=text("'#1f93ff'")
    )
    show_on_sidebar: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CustomAttributeDefinition(Base):
    __tablename__ = "custom_attribute_definitions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "scope", "attribute_key", name="custom_attr_key_uniq"
        ),
        CheckConstraint(
            "attribute_key ~ '^[a-z][a-z0-9_]*$'", name="custom_attr_key_format_chk"
        ),
        CheckConstraint(
            "value_type <> 'list' OR cardinality(list_values) > 0",
            name="custom_attr_list_chk",
        ),
        workspace_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope: Mapped[AttributeScope] = mapped_column(ATTRIBUTE_SCOPE, nullable=False)
    attribute_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[AttributeValueType] = mapped_column(
        ATTRIBUTE_VALUE_TYPE, nullable=False
    )
    list_values: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    regex_pattern: Mapped[str | None] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ai_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CannedResponse(Base):
    __tablename__ = "canned_responses"
    __table_args__ = (
        CheckConstraint(
            "char_length(content) <= 10000", name="canned_responses_content_len_chk"
        ),
        workspace_fk(),
        Index(
            "canned_responses_code_uniq",
            "workspace_id",
            text("lower(short_code)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "canned_responses_search_idx",
            text("(short_code || ' ' || content) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    short_code: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class Macro(Base):
    __tablename__ = "macros"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('personal', 'global')", name="macros_visibility_chk"
        ),
        CheckConstraint(
            "jsonb_typeof(actions) = 'array'", name="macros_actions_is_array"
        ),
        workspace_fk(),
        Index("macros_ws_idx", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'personal'")
    )
    actions: Mapped[list[Any]] = jsonb_array()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        workspace_fk(),
        ws_fk("conversation_id", "conversations"),
        ws_fk("message_id", "messages"),
        Index(
            "notifications_user_feed_idx",
            "user_id",
            "workspace_id",
            text("created_at DESC"),
        ),
        Index(
            "notifications_user_unread_idx",
            "user_id",
            "workspace_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = jsonb_object()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
