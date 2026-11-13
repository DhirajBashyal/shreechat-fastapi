"""Platform admin: our staff, above every organization (docs/12 §7).

Platform admins are users with a ``platform_admins`` row. They aren't members
of any organization through this role; they see every organization through
the /api/v1/platform API only. Every change writes an audit row with
``actor_kind = 'platform_admin'``.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    Organization,
    OrganizationMembership,
    PlatformAdmin,
    User,
    Workspace,
)
from app.models.enums import MembershipRole, OrgStatus
from app.schemas.platform import (
    OrganizationCounts,
    PlatformAdminRead,
    PlatformMemberRead,
    PlatformOrganizationDetail,
    PlatformOrganizationList,
    PlatformOrganizationRead,
    PlatformOrganizationUpdate,
)
from app.schemas.tenancy import PlanRead
from app.services.audit import audit
from app.services.limits import plan_info

_member_count = (
    select(func.count())
    .where(OrganizationMembership.organization_id == Organization.id)
    .correlate(Organization)
    .scalar_subquery()
)
_workspace_count = (
    select(func.count())
    .where(Workspace.organization_id == Organization.id, Workspace.deleted_at.is_(None))
    .correlate(Organization)
    .scalar_subquery()
)
_owner_email = (
    select(User.email)
    .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
    .where(
        OrganizationMembership.organization_id == Organization.id,
        OrganizationMembership.role == MembershipRole.OWNER,
    )
    .correlate(Organization)
    .scalar_subquery()
)


async def is_platform_admin(session: AsyncSession, user_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(PlatformAdmin.id).where(PlatformAdmin.user_id == user_id)
    )
    return found is not None


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


async def organization_counts(session: AsyncSession) -> OrganizationCounts:
    rows = (
        await session.execute(
            select(Organization.status, func.count())
            .where(Organization.deleted_at.is_(None))
            .group_by(Organization.status)
        )
    ).all()
    by_status = {status: count for status, count in rows}
    return OrganizationCounts(
        total=sum(by_status.values()),
        active=by_status.get(OrgStatus.ACTIVE, 0),
        suspended=by_status.get(OrgStatus.SUSPENDED, 0),
        pending_deletion=by_status.get(OrgStatus.PENDING_DELETION, 0),
    )


async def list_organizations(
    session: AsyncSession,
    *,
    q: str | None,
    status: OrgStatus | None,
    limit: int,
    offset: int,
) -> PlatformOrganizationList:
    filters: list[Any] = [Organization.deleted_at.is_(None)]
    if status is not None:
        filters.append(Organization.status == status)
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Organization.name.ilike(pattern),
                Organization.slug.ilike(pattern),
                _owner_email.ilike(pattern),
            )
        )
    matching = await session.scalar(
        select(func.count()).select_from(Organization).where(*filters)
    )
    rows = (
        await session.execute(
            select(Organization, _member_count, _workspace_count, _owner_email)
            .where(*filters)
            .order_by(Organization.created_at.desc(), Organization.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for org, members, workspaces, owner_email in rows:
        info = await plan_info(session, org)
        items.append(
            PlatformOrganizationRead(
                id=org.id,
                name=org.name,
                slug=org.slug,
                status=org.status,
                created_at=org.created_at,
                deletion_scheduled_at=org.deletion_scheduled_at,
                owner_email=owner_email,
                member_count=members,
                workspace_count=workspaces,
                plan=PlanRead(code=info.code, name=info.name) if info.name else None,
            )
        )
    return PlatformOrganizationList(
        counts=await organization_counts(session),
        matching=matching or 0,
        limit=limit,
        offset=offset,
        items=items,
    )


async def _get_org(session: AsyncSession, organization_id: uuid.UUID) -> Organization:
    org = await session.scalar(
        select(Organization).where(
            Organization.id == organization_id, Organization.deleted_at.is_(None)
        )
    )
    if org is None:
        raise NotFoundError()
    return org


async def get_organization(
    session: AsyncSession, organization_id: uuid.UUID
) -> PlatformOrganizationDetail:
    org = await _get_org(session, organization_id)
    info = await plan_info(session, org)
    members = (
        await session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == org.id)
            .order_by(OrganizationMembership.created_at)
        )
    ).all()
    workspaces = list(
        await session.scalars(
            select(Workspace.name)
            .where(Workspace.organization_id == org.id, Workspace.deleted_at.is_(None))
            .order_by(Workspace.created_at)
        )
    )
    owner = next((u.email for m, u in members if m.role == MembershipRole.OWNER), None)
    return PlatformOrganizationDetail(
        id=org.id,
        name=org.name,
        slug=org.slug,
        status=org.status,
        created_at=org.created_at,
        deletion_scheduled_at=org.deletion_scheduled_at,
        owner_email=owner,
        member_count=len(members),
        workspace_count=len(workspaces),
        plan=PlanRead(code=info.code, name=info.name) if info.name else None,
        limits=info.limits,
        limits_override=org.limits_override or {},
        feature_flags=list(org.feature_flags or []),
        workspaces=workspaces,
        members=[
            PlatformMemberRead(
                user_id=u.id,
                name=u.name,
                email=u.email,
                role=m.role,
                joined_at=m.created_at,
            )
            for m, u in members
        ],
    )


async def update_organization(
    session: AsyncSession,
    admin_user_id: uuid.UUID,
    organization_id: uuid.UUID,
    data: PlatformOrganizationUpdate,
    request: Request,
) -> PlatformOrganizationDetail:
    org = await _get_org(session, organization_id)
    changes: dict[str, Any] = {}

    if data.status is not None and data.status != org.status:
        # Deletion belongs to the owner (delete / restore); platform admins
        # suspend and reactivate.
        if org.status == OrgStatus.PENDING_DELETION:
            raise ConflictError(
                "Organization is pending deletion; only its owner can restore it"
            )
        if data.status not in (OrgStatus.ACTIVE, OrgStatus.SUSPENDED):
            raise ValidationFailedError("status must be active or suspended")
        changes["status"] = [org.status.value, data.status.value]
        org.status = data.status

    if data.limits_override is not None:
        changes["limits_override"] = data.limits_override
        org.limits_override = data.limits_override

    if data.feature_flags is not None:
        flags = sorted(set(data.feature_flags))
        changes["feature_flags"] = flags
        org.feature_flags = flags

    if changes:
        audit(
            session,
            action="platform.organization_updated",
            resource_type="organization",
            resource_id=org.id,
            actor_id=admin_user_id,
            actor_kind="platform_admin",
            organization_id=org.id,
            changes=changes,
            request=request,
        )
        await session.commit()
    return await get_organization(session, org.id)


# ---------------------------------------------------------------------------
# Platform admins
# ---------------------------------------------------------------------------


async def list_admins(session: AsyncSession) -> list[PlatformAdminRead]:
    rows = (
        await session.execute(
            select(PlatformAdmin, User)
            .join(User, User.id == PlatformAdmin.user_id)
            .order_by(PlatformAdmin.created_at)
        )
    ).all()
    return [
        PlatformAdminRead(
            user_id=u.id,
            name=u.name,
            email=u.email,
            granted_by=a.granted_by,
            created_at=a.created_at,
        )
        for a, u in rows
    ]


async def grant_admin(
    session: AsyncSession,
    email: str,
    *,
    granted_by: uuid.UUID | None,
    request: Request | None = None,
) -> User:
    """Make an existing user a platform admin."""
    user = await session.scalar(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    if user is None:
        raise NotFoundError("No user with this email; they must sign up first")
    if await is_platform_admin(session, user.id):
        raise ConflictError("Already a platform admin")
    session.add(PlatformAdmin(user_id=user.id, granted_by=granted_by))
    audit(
        session,
        action="platform.admin_granted",
        resource_type="user",
        resource_id=user.id,
        actor_id=granted_by,
        actor_kind="platform_admin" if granted_by else "system",
        changes={"email": user.email},
        request=request,
    )
    await session.commit()
    return user


async def revoke_admin(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    revoked_by: uuid.UUID | None,
    request: Request | None = None,
) -> None:
    if revoked_by is not None and user_id == revoked_by:
        # Prevents locking the platform out by removing the last admin.
        raise ValidationFailedError("You can't revoke your own platform admin role")
    admin = await session.scalar(
        select(PlatformAdmin).where(PlatformAdmin.user_id == user_id)
    )
    if admin is None:
        raise NotFoundError()
    await session.delete(admin)
    audit(
        session,
        action="platform.admin_revoked",
        resource_type="user",
        resource_id=user_id,
        actor_id=revoked_by,
        actor_kind="platform_admin" if revoked_by else "system",
        request=request,
    )
    await session.commit()
