"""Platform tables: global, NOT tenant-scoped (no organization_id, no RLS)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CIText,
    created_at_col,
    deleted_at_col,
    jsonb_object,
    random_token_default,
    updated_at_col,
    uuid_pk,
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(ui_settings) = 'object'", name="users_ui_settings_is_object"
        ),
        # users → stored_files → organizations → users is a cycle, so this FK
        # is added after the tables are created (ALTER TABLE).
        ForeignKeyConstraint(
            ["avatar_file_id"],
            ["stored_files.id"],
            ondelete="SET NULL",
            name="users_avatar_fk",
            use_alter=True,
        ),
        Index(
            "users_email_uniq",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("users_realtime_token_uniq", "realtime_token", unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CIText(), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mfa_backup_code_hashes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    ui_settings: Mapped[dict[str, Any]] = jsonb_object()
    message_signature: Mapped[str | None] = mapped_column(Text)
    realtime_token: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=random_token_default()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class PlatformAdmin(Base):
    __tablename__ = "platform_admins"
    __table_args__ = (UniqueConstraint("user_id", name="platform_admins_user_uniq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("user_sessions_token_uniq", "refresh_token_hash", unique=True),
        Index(
            "user_sessions_user_active_idx",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("user_sessions_family_idx", "family_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Permission(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    __table_args__ = (
        UniqueConstraint("code", name="subscription_plans_code_uniq"),
        CheckConstraint(
            "billing_interval IN ('month', 'year')",
            name="subscription_plans_interval_chk",
        ),
        CheckConstraint("price_cents >= 0", name="subscription_plans_price_chk"),
        CheckConstraint(
            "jsonb_typeof(limits) = 'object'",
            name="subscription_plans_limits_is_object",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(16), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default=text("'USD'")
    )
    limits: Mapped[dict[str, Any]] = jsonb_object()
    features: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    external_price_id: Mapped[str | None] = mapped_column(String(255))
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
