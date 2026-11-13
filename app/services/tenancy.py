"""Organizations, workspaces, members, invitations and the organization
lifecycle.

Role rules (docs/12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md §4):
- An organization has exactly one owner. Nobody can remove or demote the
  owner, and nobody becomes owner except through transfer-ownership.
- Owners and administrators (permission ``member.manage``) invite, change and
  remove administrators and agents. Nobody grants a role above their own.
- Members don't edit or remove themselves through the member endpoints; they
  leave with POST /organizations/{org}/leave (the owner must transfer first).
- Owner-only (role check, not a permission code): transfer ownership, delete
  and restore the organization. Both destructive ones need the password.
"""

import uuid
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PasswordConfirmationError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.security import generate_token, hash_token, utcnow, verify_password
from app.core.tenancy import TenantContext
from app.models import (
    ApiAccessToken,
    Conversation,
    InboxMember,
    Invitation,
    Organization,
    OrganizationMembership,
    TeamMember,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.models.enums import ConversationStatus, MembershipRole, OrgStatus
from app.schemas.tenancy import (
    InvitationCreate,
    MemberRead,
    MemberUpdate,
    OrganizationRead,
    OrganizationUpdate,
    PlanRead,
    WorkspaceCreate,
)
from app.services.audit import audit
from app.services.auth import slugify
from app.services.limits import check_agent_seats, plan_info
from app.services.mailer import send_email

ROLE_RANK = {
    MembershipRole.AGENT: 0,
    MembershipRole.ADMINISTRATOR: 1,
    MembershipRole.OWNER: 2,
}

# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


async def get_organization(session: AsyncSession, ctx: TenantContext) -> Organization:
    org = await session.get(Organization, ctx.organization_id)
    if org is None:
        raise NotFoundError()
    return org


async def organization_read(
    session: AsyncSession, ctx: TenantContext, org: Organization
) -> OrganizationRead:
    """Owners and administrators see plan and limits; agents only the plan
    name (docs/12 §3)."""
    info = await plan_info(session, org)
    sees_limits = ctx.membership_role in (
        MembershipRole.OWNER,
        MembershipRole.ADMINISTRATOR,
    )
    read = OrganizationRead.model_validate(org)
    read.plan = PlanRead(code=info.code, name=info.name) if info.name else None
    read.limits = info.limits if sees_limits else None
    return read


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
                # Agents see names and availability, not contact details.
                email=user.email if ctx.has("member.manage") else None,
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
    """May the caller invite, change or remove someone with ``target_role``
    (or give someone that role)? The caller already holds member.manage."""
    if target_role == MembershipRole.OWNER:
        raise PermissionDeniedError("owner")
    if ROLE_RANK[target_role] > ROLE_RANK[ctx.membership_role]:
        raise PermissionDeniedError("member.manage")


def _check_not_self(ctx: TenantContext, membership: OrganizationMembership) -> None:
    if membership.id == ctx.membership_id:
        raise ValidationFailedError(
            "You can't change or remove yourself here; use leave organization"
        )


async def update_member(
    session: AsyncSession,
    ctx: TenantContext,
    membership_id: uuid.UUID,
    data: MemberUpdate,
    request: Request,
) -> MemberRead:
    m = await _get_member(session, ctx, membership_id)
    _check_can_manage(ctx, m.role)
    _check_not_self(ctx, m)
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


async def _detach_member(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """What happens when someone stops being a member (docs/12 §4):
    their open conversations go back to the unassigned queue, they leave every
    inbox and team, and their personal API tokens for this organization are
    revoked. Access itself ends at once: memberships are read on every
    request. Returns the number of conversations unassigned."""
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.organization_id == organization_id,
            Conversation.assignee_id == user_id,
            Conversation.status != ConversationStatus.RESOLVED,
        )
        .values(assignee_id=None)
    )
    for table in (InboxMember, TeamMember):
        await session.execute(
            delete(table).where(
                table.organization_id == organization_id, table.user_id == user_id
            )
        )
    await session.execute(
        update(ApiAccessToken)
        .where(
            ApiAccessToken.organization_id == organization_id,
            ApiAccessToken.user_id == user_id,
            ApiAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def remove_member(
    session: AsyncSession,
    ctx: TenantContext,
    membership_id: uuid.UUID,
    request: Request,
) -> None:
    m = await _get_member(session, ctx, membership_id)
    _check_can_manage(ctx, m.role)
    _check_not_self(ctx, m)
    unassigned = await _detach_member(session, ctx.organization_id, m.user_id)
    await session.delete(m)  # workspace memberships cascade
    audit(
        session,
        action="member.removed",
        resource_type="organization_membership",
        resource_id=m.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={
            "user_id": str(m.user_id),
            "role": m.role.value,
            "conversations_unassigned": unassigned,
        },
        request=request,
    )
    await session.commit()


async def leave_organization(
    session: AsyncSession, ctx: TenantContext, request: Request
) -> None:
    """Any member except the owner can leave (the owner transfers first)."""
    if ctx.membership_role == MembershipRole.OWNER:
        raise ValidationFailedError(
            "The owner can't leave; transfer ownership first", code="owner_cannot_leave"
        )
    m = await _get_member(session, ctx, ctx.membership_id)
    unassigned = await _detach_member(session, ctx.organization_id, m.user_id)
    await session.delete(m)
    audit(
        session,
        action="member.left",
        resource_type="organization_membership",
        resource_id=m.id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={"role": m.role.value, "conversations_unassigned": unassigned},
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
    if data.role == MembershipRole.OWNER:
        raise ValidationFailedError("Use transfer-ownership to change the owner")
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
    await check_agent_seats(session, ctx.organization_id, include_pending=True)

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


# ---------------------------------------------------------------------------
# Organization lifecycle (owner only; docs/12 §4 and §5.1)
# ---------------------------------------------------------------------------


async def _confirm_password(
    session: AsyncSession, ctx: TenantContext, password: str
) -> None:
    user = await session.get(User, ctx.principal.user_id)
    if user is None or not verify_password(password, user.password_hash):
        raise PasswordConfirmationError()


async def transfer_ownership(
    session: AsyncSession,
    ctx: TenantContext,
    new_owner_membership_id: uuid.UUID,
    password: str,
    request: Request,
) -> None:
    """The owner hands the organization to an existing administrator and
    becomes an administrator."""
    await _confirm_password(session, ctx, password)
    target = await _get_member(session, ctx, new_owner_membership_id)
    if target.id == ctx.membership_id:
        raise ValidationFailedError("You are already the owner")
    if target.role != MembershipRole.ADMINISTRATOR:
        raise ValidationFailedError(
            "The new owner must be an administrator", code="new_owner_not_admin"
        )
    current = await _get_member(session, ctx, ctx.membership_id)
    # Demote first: the database allows only one owner at a time.
    current.role = MembershipRole.ADMINISTRATOR
    await session.flush()
    target.role = MembershipRole.OWNER
    target.custom_role_id = None
    audit(
        session,
        action="organization.ownership_transferred",
        resource_type="organization",
        resource_id=ctx.organization_id,
        actor_id=ctx.principal.user_id,
        organization_id=ctx.organization_id,
        changes={
            "from_membership_id": str(current.id),
            "to_membership_id": str(target.id),
        },
        request=request,
    )
    await session.commit()


async def schedule_deletion(
    session: AsyncSession, ctx: TenantContext, password: str, request: Request
) -> Organization:
    """Start the grace period. Until it ends only the owner sees the
    organization, and only to restore it."""
    await _confirm_password(session, ctx, password)
    org = await get_organization(session, ctx)
    org.status = OrgStatus.PENDING_DELETION
    org.deletion_scheduled_at = utcnow() + timedelta(
        days=settings.ORGANIZATION_DELETION_GRACE_DAYS
    )
    audit(
        session,
        action="organization.deletion_scheduled",
        resource_type="organization",
        resource_id=org.id,
        actor_id=ctx.principal.user_id,
        organization_id=org.id,
        changes={"purge_at": org.deletion_scheduled_at.isoformat()},
        request=request,
    )
    await session.commit()
    await session.refresh(org)
    return org


async def restore_organization(
    session: AsyncSession, ctx: TenantContext, request: Request
) -> Organization:
    org = await get_organization(session, ctx)
    if org.status != OrgStatus.PENDING_DELETION:
        raise ConflictError("Organization isn't scheduled for deletion")
    if org.deletion_scheduled_at is not None and org.deletion_scheduled_at <= utcnow():
        raise NotFoundError()  # grace period over; waiting for the purge job
    org.status = OrgStatus.ACTIVE
    org.deletion_scheduled_at = None
    audit(
        session,
        action="organization.restored",
        resource_type="organization",
        resource_id=org.id,
        actor_id=ctx.principal.user_id,
        organization_id=org.id,
        request=request,
    )
    await session.commit()
    await session.refresh(org)
    return org


async def purge_expired_organizations(session: AsyncSession) -> list[uuid.UUID]:
    """Close organizations whose grace period has ended.

    Sets ``deleted_at`` (the organization disappears for everyone, including
    the owner, and its slug is freed). Erasing the tenant's rows from every
    table is done by the data-retention job (docs/backend/BACKGROUND_JOBS.md),
    which picks up organizations with ``deleted_at`` set.
    """
    now = utcnow()
    orgs = list(
        await session.scalars(
            select(Organization).where(
                Organization.status == OrgStatus.PENDING_DELETION,
                Organization.deletion_scheduled_at <= now,
                Organization.deleted_at.is_(None),
            )
        )
    )
    for org in orgs:
        org.deleted_at = now
        audit(
            session,
            action="organization.purged",
            resource_type="organization",
            resource_id=org.id,
            actor_kind="system",
            organization_id=org.id,
        )
    await session.commit()
    return [o.id for o in orgs]
