"""Organizations, workspaces, members and invitations.

Role rules (docs/10_AUTHENTICATION_AND_RBAC.md §4.1, docs/backend/API_DESIGN.md §3):
- An organization has exactly one owner. Ownership isn't changed through the
  member endpoints (that's a separate transfer-ownership flow).
- Only the owner can grant, change or revoke the administrator role.
- Administrators manage agents (permission ``member.manage``).
"""

import uuid
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.security import generate_token, hash_token, utcnow
from app.core.tenancy import TenantContext
from app.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.models.enums import MembershipRole
from app.schemas.tenancy import (
    InvitationCreate,
    MemberRead,
    MemberUpdate,
    OrganizationUpdate,
    WorkspaceCreate,
)
from app.services.audit import audit
from app.services.auth import slugify
from app.services.mailer import send_email

# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


async def get_organization(session: AsyncSession, ctx: TenantContext) -> Organization:
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise NotFoundError()
    return org


async def update_organization(
    session: AsyncSession,
    ctx: TenantContext,
    data: OrganizationUpdate,
    request: Request,
) -> Organization:
    org = await get_organization(session, ctx)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(org, field, value)
    audit(
        session,
        action="organization.updated",
        resource_type="organization",
        resource_id=org.id,
        actor_id=ctx.principal.user_id,
        organization_id=org.id,
        changes=changes,
        request=request,
    )
    await session.commit()
    await session.refresh(org)
    return org


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


async def create_workspace(
    session: AsyncSession, ctx: TenantContext, data: WorkspaceCreate, request: Request
) -> Workspace:
    slug = data.slug or slugify(data.name)
    taken = await session.scalar(
        select(Workspace.id).where(
            Workspace.organization_id == ctx.organization_id,
            Workspace.slug == slug,
            Workspace.deleted_at.is_(None),
        )
    )
    if taken:
        raise ConflictError("A workspace with this slug already exists")
    workspace = Workspace(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        name=data.name,
        slug=slug,
        timezone=data.timezone,
    )
    session.add(workspace)
    await session.flush()
    # The creator works in the new workspace too.
    session.add(
        WorkspaceMembership(
            organization_id=ctx.organization_id,
            workspace_id=workspace.id,
            membership_id=ctx.membership_id,
        )
    )
    audit(
        session,
        action="workspace.created",
        resource_type="workspace",
        resource_id=workspace.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        workspace_id=workspace.id,
        request=request,
    )
    await session.commit()
    await session.refresh(workspace)
    return workspace


async def get_workspace(session: AsyncSession, ctx: TenantContext) -> Workspace:
    ws = await session.get(Workspace, ctx.require_workspace())
    if ws is None or ws.deleted_at is not None:
        raise NotFoundError()
    return ws


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def list_members(
    session: AsyncSession, ctx: TenantContext, workspace_id: uuid.UUID | None = None
) -> list[MemberRead]:
    stmt = (
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == ctx.organization_id)
        .order_by(OrganizationMembership.created_at)
    )
    rows = (await session.execute(stmt)).all()
    ws_rows = (
        await session.execute(
            select(WorkspaceMembership.membership_id, WorkspaceMembership.workspace_id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.organization_id == ctx.organization_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).all()
    by_member: dict[uuid.UUID, list[uuid.UUID]] = {}
    for membership_id, ws_id in ws_rows:
        by_member.setdefault(membership_id, []).append(ws_id)

    members = []
    for m, user in rows:
        ws_ids = by_member.get(m.id, [])
        if workspace_id is not None:
            in_ws = workspace_id in ws_ids or m.role in (
                MembershipRole.OWNER,
                MembershipRole.ADMINISTRATOR,
            )
            if not in_ws:
                continue
        members.append(
            MemberRead(
                membership_id=m.id,
                user_id=user.id,
                name=user.name,
                email=user.email,
                role=m.role,
                custom_role_id=m.custom_role_id,
                availability=m.availability,
                workspace_ids=ws_ids,
            )
        )
    return members


async def _get_member(
    session: AsyncSession, ctx: TenantContext, membership_id: uuid.UUID
) -> OrganizationMembership:
    m = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == ctx.organization_id,
        )
    )
    if m is None:
        raise NotFoundError()
    return m


def _check_can_manage(ctx: TenantContext, target_role: MembershipRole) -> None:
    if target_role == MembershipRole.OWNER:
        raise PermissionDeniedError("owner")
    if (
        target_role == MembershipRole.ADMINISTRATOR
        and ctx.membership_role != MembershipRole.OWNER
    ):
        raise PermissionDeniedError("owner")


async def update_member(
    session: AsyncSession,
    ctx: TenantContext,
    membership_id: uuid.UUID,
    data: MemberUpdate,
    request: Request,
) -> MemberRead:
    m = await _get_member(session, ctx, membership_id)
    _check_can_manage(ctx, m.role)
    changes: dict[str, object] = {}

    if data.role is not None and data.role != m.role:
        if data.role == MembershipRole.OWNER:
            raise ValidationFailedError("Use transfer-ownership to change the owner")
        _check_can_manage(ctx, data.role)
        changes["role"] = [m.role.value, data.role.value]
        m.role = data.role
        if data.role != MembershipRole.AGENT:
            m.custom_role_id = None  # custom roles only apply to agents

    if data.workspace_ids is not None:
        wanted = set(data.workspace_ids)
        valid = set(
            await session.scalars(
                select(Workspace.id).where(
                    Workspace.organization_id == ctx.organization_id,
                    Workspace.id.in_(wanted),
                    Workspace.deleted_at.is_(None),
                )
            )
        )
        if wanted - valid:
            raise NotFoundError("Workspace not found")
        await session.execute(
            delete(WorkspaceMembership).where(WorkspaceMembership.membership_id == m.id)
        )
        session.add_all(
            WorkspaceMembership(
                organization_id=ctx.organization_id,
                workspace_id=ws_id,
                membership_id=m.id,
            )
            for ws_id in wanted
        )
        changes["workspace_ids"] = sorted(str(w) for w in wanted)

    if changes:
        audit(
            session,
            action="member.updated",
            resource_type="organization_membership",
            resource_id=m.id,
            actor_id=ctx.principal.user_id,
            organization_id=ctx.organization_id,
            changes=changes,
            request=request,
        )
    await session.commit()
    return next(
        member
        for member in await list_members(session, ctx)
        if member.membership_id == m.id
    )


async def remove_member(
    session: AsyncSession,
    ctx: TenantContext,
    membership_id: uuid.UUID,
    request: Request,
) -> None:
    m = await _get_member(session, ctx, membership_id)
    _check_can_manage(ctx, m.role)
    await session.delete(m)  # workspace memberships cascade
    audit(
        session,
        action="member.removed",
        resource_type="organization_membership",
        resource_id=m.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={"user_id": str(m.user_id), "role": m.role.value},
        request=request,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def create_invitation(
    session: AsyncSession, ctx: TenantContext, data: InvitationCreate, request: Request
) -> Invitation:
    _check_can_manage(ctx, data.role)
    already_member = await session.scalar(
        select(OrganizationMembership.id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == ctx.organization_id,
            User.email == data.email,
        )
    )
    if already_member:
        raise ConflictError("This person is already a member")

    token = generate_token("inv_")
    invitation = Invitation(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        email=data.email,
        role=data.role,
        token_hash=hash_token(token),
        invited_by=ctx.principal.user_id,
        expires_at=utcnow() + timedelta(days=settings.INVITATION_TTL_DAYS),
        created_at=utcnow(),
    )
    session.add(invitation)
    try:
        await session.flush()
    except IntegrityError:
        raise ConflictError("An invitation for this email is already pending") from None

    org = await session.get(Organization, ctx.organization_id)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/accept-invitation?token={token}"
    await send_email(
        data.email,
        f"You're invited to join {org.name if org else 'an organization'}",
        f"You've been invited as {data.role.value}.\n\n"
        f"Accept within {settings.INVITATION_TTL_DAYS} days:\n{link}\n\n"
        "New to the platform? Sign up with this email and the invitation token "
        "to join directly.",
    )
    audit(
        session,
        action="invitation.created",
        resource_type="invitation",
        resource_id=invitation.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={"email": data.email, "role": data.role.value},
        request=request,
    )
    await session.commit()
    await session.refresh(invitation)
    return invitation


async def list_invitations(
    session: AsyncSession, ctx: TenantContext
) -> list[Invitation]:
    return list(
        await session.scalars(
            select(Invitation)
            .where(
                Invitation.organization_id == ctx.organization_id,
                Invitation.accepted_at.is_(None),
                Invitation.expires_at > utcnow(),
            )
            .order_by(Invitation.created_at.desc())
        )
    )


async def revoke_invitation(
    session: AsyncSession,
    ctx: TenantContext,
    invitation_id: uuid.UUID,
    request: Request,
) -> None:
    invitation = await session.scalar(
        select(Invitation).where(
            Invitation.id == invitation_id,
            Invitation.organization_id == ctx.organization_id,
            Invitation.accepted_at.is_(None),
        )
    )
    if invitation is None:
        raise NotFoundError()
    await session.delete(invitation)
    audit(
        session,
        action="invitation.revoked",
        resource_type="invitation",
        resource_id=invitation_id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        request=request,
    )
    await session.commit()
