"""Tenancy: organizations, roles, memberships, invitations, API tokens, workspaces."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CIText,
    created_at_col,
    deleted_at_col,
    jsonb_object,
    org_fk,
    updated_at_col,
    uuid_pk,
    workspace_fk,
)
from app.models.enums import (
    AVAILABILITY_STATUS,
    MEMBERSHIP_ROLE,
    ORG_STATUS,
    AvailabilityStatus,
    MembershipRole,
    OrgStatus,
)


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(settings) = 'object'", name="organizations_settings_is_object"
        ),
        Index(
            "organizations_slug_uniq",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("organizations_status_idx", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OrgStatus] = mapped_column(
        ORG_STATUS, nullable=False, server_default=text("'active'")
    )
    locale: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'en'")
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )
    feature_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    settings: Mapped[dict[str, Any]] = jsonb_object()
    limits_override: Mapped[dict[str, Any]] = jsonb_object()
    external_customer_id: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="roles_name_per_org_uniq"),
        UniqueConstraint("organization_id", "id", name="roles_org_id_uniq"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (org_fk("role_id", "roles"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    permission_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("permissions.code", ondelete="CASCADE"),
        primary_key=True,
    )


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="org_memberships_user_uniq"
        ),
        UniqueConstraint("organization_id", "id", name="org_memberships_org_id_uniq"),
        org_fk("custom_role_id", "roles", ondelete="SET NULL"),
        Index("org_memberships_user_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MembershipRole] = mapped_column(
        MEMBERSHIP_ROLE, nullable=False, server_default=text("'agent'")
    )
    custom_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    availability: Mapped[AvailabilityStatus] = mapped_column(
        AVAILABILITY_STATUS, nullable=False, server_default=text("'offline'")
    )
    auto_offline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    accepts_conversations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        org_fk("custom_role_id", "roles", ondelete="SET NULL"),
        Index("invitations_token_uniq", "token_hash", unique=True),
        Index(
            "invitations_open_email_uniq",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CIText(), nullable=False)
    role: Mapped[MembershipRole] = mapped_column(
        MEMBERSHIP_ROLE, nullable=False, server_default=text("'agent'")
    )
    custom_role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class ApiAccessToken(Base):
    __tablename__ = "api_access_tokens"
    __table_args__ = (
        Index("api_access_tokens_hash_uniq", "token_hash", unique=True),
        Index("api_access_tokens_org_idx", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="workspaces_org_id_uniq"),
        CheckConstraint(
            "auto_resolve_after_minutes IS NULL OR auto_resolve_after_minutes BETWEEN 10 AND 1439856",
            name="workspaces_auto_resolve_chk",
        ),
        Index(
            "workspaces_slug_per_org_uniq",
            "organization_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )
    auto_resolve_after_minutes: Mapped[int | None] = mapped_column(Integer)
    settings: Mapped[dict[str, Any]] = jsonb_object()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class WorkspaceCounter(Base):
    """Monotonic per-workspace counters (e.g. conversation display numbers).

    Only accessed through the next_workspace_counter() SQL function.
    """

    __tablename__ = "workspace_counters"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    counter_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    current_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        workspace_fk(),
        ForeignKeyConstraint(
            ["organization_id", "membership_id"],
            ["organization_memberships.organization_id", "organization_memberships.id"],
            ondelete="CASCADE",
        ),
        Index("workspace_memberships_member_idx", "membership_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = created_at_col()
