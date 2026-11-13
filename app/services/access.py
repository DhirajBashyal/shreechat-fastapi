"""Membership and permission resolution (docs/10_AUTHENTICATION_AND_RBAC.md §4).

Permissions are resolved from the database on every request, never from the
token, so a removed member or a role change takes effect immediately.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import effective_permissions
from app.models import (
    Organization,
    OrganizationMembership,
    RolePermission,
    Workspace,
    WorkspaceMembership,
)
from app.models.enums import MembershipRole, OrgStatus
from app.schemas.auth import MembershipRead, WorkspaceSummary


@dataclass(frozen=True, slots=True)
class ResolvedMembership:
    membership: OrganizationMembership
    organization: Organization
    permissions: frozenset[str]

    @property
    def sees_all_workspaces(self) -> bool:
        """Owners and administrators work in every workspace of the org;
        agents only in the workspaces they were added to."""
        return self.membership.role in (
            MembershipRole.OWNER,
            MembershipRole.ADMINISTRATOR,
        )


async def _custom_role_permissions(
    session: AsyncSession, role_id: uuid.UUID | None
) -> frozenset[str] | None:
    if role_id is None:
        return None
    rows = await session.scalars(
        select(RolePermission.permission_code).where(RolePermission.role_id == role_id)
    )
    return frozenset(rows)


async def resolve_membership(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> ResolvedMembership | None:
    """The user's membership in a (not deleted) organization, or None."""
    row = (
        await session.execute(
            select(OrganizationMembership, Organization)
            .join(
                Organization, Organization.id == OrganizationMembership.organization_id
            )
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
                Organization.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    membership, organization = row
    # During the deletion grace period only the owner (who can restore it)
    # still sees the organization; for everyone else it's gone (docs/12 §5.1).
    if (
        organization.status == OrgStatus.PENDING_DELETION
        and membership.role != MembershipRole.OWNER
    ):
        return None
    custom = await _custom_role_permissions(session, membership.custom_role_id)
    return ResolvedMembership(
        membership=membership,
        organization=organization,
        permissions=effective_permissions(membership.role, custom),
    )


async def can_access_workspace(
    session: AsyncSession, resolved: ResolvedMembership, workspace_id: uuid.UUID
) -> bool:
    if resolved.sees_all_workspaces:
        return True
    found = await session.scalar(
        select(WorkspaceMembership.workspace_id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.membership_id == resolved.membership.id,
        )
    )
    return found is not None


async def visible_workspaces(
    session: AsyncSession, resolved: ResolvedMembership
) -> list[Workspace]:
    stmt = select(Workspace).where(
        Workspace.organization_id == resolved.organization.id,
        Workspace.deleted_at.is_(None),
    )
    if not resolved.sees_all_workspaces:
        stmt = stmt.join(
            WorkspaceMembership,
            (WorkspaceMembership.workspace_id == Workspace.id)
            & (WorkspaceMembership.membership_id == resolved.membership.id),
        )
    return list(await session.scalars(stmt.order_by(Workspace.created_at)))


async def list_memberships(
    session: AsyncSession, user_id: uuid.UUID
) -> list[MembershipRead]:
    """Every organization the user belongs to, with role, permissions and the
    workspaces they can see. Used by login and GET /auth/me."""
    org_ids = await session.scalars(
        select(OrganizationMembership.organization_id)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user_id,
            Organization.deleted_at.is_(None),
        )
        .order_by(OrganizationMembership.created_at)
    )
    result: list[MembershipRead] = []
    for org_id in list(org_ids):
        resolved = await resolve_membership(session, user_id, org_id)
        if resolved is None:
            continue
        workspaces = await visible_workspaces(session, resolved)
        result.append(
            MembershipRead(
                organization_id=resolved.organization.id,
                organization_name=resolved.organization.name,
                organization_slug=resolved.organization.slug,
                organization_status=resolved.organization.status,
                deletion_scheduled_at=resolved.organization.deletion_scheduled_at,
                membership_id=resolved.membership.id,
                role=resolved.membership.role,
                custom_role_id=resolved.membership.custom_role_id,
                permissions=sorted(resolved.permissions),
                workspaces=[
                    WorkspaceSummary(id=w.id, name=w.name, slug=w.slug)
                    for w in workspaces
                ],
            )
        )
    return result
