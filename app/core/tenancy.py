"""Tenant context (docs/09_MULTI_TENANCY.md §3)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.enums import MembershipRole


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: uuid.UUID
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None  # None for org-level endpoints
    principal: Principal
    membership_id: uuid.UUID
    membership_role: MembershipRole
    custom_role_id: uuid.UUID | None
    permissions: frozenset[str]

    def require_workspace(self) -> uuid.UUID:
        if self.workspace_id is None:
            raise NotFoundError("Workspace required")
        return self.workspace_id

    def has(self, permission: str) -> bool:
        return permission in self.permissions


async def bind_tenant(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """SET LOCAL app.current_org_id for the current transaction.

    Read by the current_org_id() SQL function used by row-level security
    policies. Transaction-scoped, so it can't leak across pooled connections.
    """
    await session.execute(
        select(func.set_config("app.current_org_id", str(organization_id), True))
    )
