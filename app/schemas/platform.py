"""Platform admin API schemas (/api/v1/platform)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import MembershipRole, OrgStatus
from app.schemas.tenancy import PlanRead


class OrganizationCounts(BaseModel):
    """How many organizations exist (not counting purged ones)."""

    total: int
    active: int
    suspended: int
    pending_deletion: int


class PlatformOrganizationRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: OrgStatus
    created_at: datetime
    deletion_scheduled_at: datetime | None
    owner_email: str | None
    member_count: int
    workspace_count: int
    plan: PlanRead | None


class PlatformOrganizationList(BaseModel):
    counts: OrganizationCounts
    # Organizations matching q/status (for pagination); counts are global.
    matching: int
    limit: int
    offset: int
    items: list[PlatformOrganizationRead]


class PlatformMemberRead(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    role: MembershipRole
    joined_at: datetime


class PlatformOrganizationDetail(PlatformOrganizationRead):
    limits: dict[str, Any]
    limits_override: dict[str, Any]
    feature_flags: list[str]
    workspaces: list[str]
    members: list[PlatformMemberRead]


class PlatformOrganizationUpdate(BaseModel):
    # active ↔ suspended only; deletion is the owner's decision.
    status: OrgStatus | None = None
    # Replaces organizations.limits_override, e.g. {"agents": 50}. Keys
    # override the plan's limits; {} goes back to the plan.
    limits_override: dict[str, int] | None = None
    feature_flags: list[str] | None = None

    @field_validator("limits_override")
    @classmethod
    def non_negative(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        if v and any(n < 0 for n in v.values()):
            raise ValueError("limits must be 0 or more")
        return v


class PlatformAdminRead(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    granted_by: uuid.UUID | None
    created_at: datetime


class PlatformAdminGrant(BaseModel):
    email: EmailStr = Field(description="An existing user's email")
