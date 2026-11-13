import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import AvailabilityStatus, MembershipRole, OrgStatus


class PlanRead(BaseModel):
    code: str | None
    name: str | None


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrgStatus
    locale: str
    timezone: str
    created_at: datetime
    # Set while status = pending_deletion: when the organization is purged.
    deletion_scheduled_at: datetime | None = None
    # Only on GET /organizations/{org}. Every member sees the plan name;
    # limits are null for agents (docs/12 §3). A missing limit key = unlimited.
    plan: PlanRead | None = None
    limits: dict[str, Any] | None = None


class PasswordConfirmation(BaseModel):
    """Re-entered password for destructive owner actions."""

    password: str = Field(min_length=1, max_length=128)


class TransferOwnershipRequest(PasswordConfirmation):
    membership_id: uuid.UUID = Field(description="An administrator's membership")


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    timezone: str
    created_at: datetime


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$"
    )
    timezone: str = Field(default="UTC", max_length=64)


class MemberRead(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    # null unless the caller has member.manage (agents see names only)
    email: str | None
    role: MembershipRole
    custom_role_id: uuid.UUID | None
    availability: AvailabilityStatus
    workspace_ids: list[uuid.UUID]


class MemberUpdate(BaseModel):
    role: MembershipRole | None = None
    # Replace the set of workspaces this member works in.
    workspace_ids: list[uuid.UUID] | None = None


class InvitationCreate(BaseModel):
    # The invitations table has no workspace list: assign workspaces after the
    # invite is accepted with PATCH /organizations/{org}/members/{id}.
    email: EmailStr
    role: MembershipRole = MembershipRole.AGENT


class InvitationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: MembershipRole
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime


class PermissionRead(BaseModel):
    code: str
    description: str


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    actor_kind: str
    actor_id: uuid.UUID | None
    resource_type: str
    resource_id: uuid.UUID | None
    changes: dict[str, Any]
    created_at: datetime
