"""/api/v1/organizations — organizations, members, invitations, workspaces.

Every /organizations/{organization_id}/... route resolves the caller's
membership first (404 for non-members), then checks the permission.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.api.deps import (
    CurrentUserDep,
    DbSession,
    OrgContext,
    require_permission,
)
from app.core.permissions import PERMISSIONS
from app.core.tenancy import TenantContext
from app.models import AuditLog, Organization, OrganizationMembership
from app.schemas.auth import AcceptInvitationRequest, MeResponse, UserRead
from app.schemas.tenancy import (
    AuditLogRead,
    InvitationCreate,
    InvitationRead,
    MemberRead,
    MemberUpdate,
    OrganizationRead,
    OrganizationUpdate,
    PermissionRead,
    WorkspaceCreate,
    WorkspaceRead,
)
from app.services import auth as auth_service
from app.services import tenancy as tenancy_service
from app.services.access import (
    list_memberships,
    resolve_membership,
    visible_workspaces,
)

router = APIRouter()


# Org context that must hold the given permission (403 otherwise).
ManageOrg = Annotated[TenantContext, Depends(require_permission("organization.manage"))]
ManageMembers = Annotated[TenantContext, Depends(require_permission("member.manage"))]
ViewAuditLog = Annotated[TenantContext, Depends(require_permission("audit_log.view"))]


@router.get("/permissions", tags=["Organizations"], summary="Permission catalog")
async def list_permissions(current: CurrentUserDep) -> list[PermissionRead]:
    return [PermissionRead(code=c, description=d) for c, d in PERMISSIONS.items()]


@router.get(
    "/organizations",
    tags=["Organizations"],
    summary="Organizations the current user belongs to",
)
async def list_organizations(
    current: CurrentUserDep, session: DbSession
) -> list[OrganizationRead]:
    orgs = await session.scalars(
        select(Organization)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(
            OrganizationMembership.user_id == current.user.id,
            Organization.deleted_at.is_(None),
        )
        .order_by(Organization.created_at)
    )
    return [OrganizationRead.model_validate(o) for o in orgs]


@router.get("/organizations/{organization_id}", tags=["Organizations"])
async def get_organization(ctx: OrgContext, session: DbSession) -> OrganizationRead:
    return OrganizationRead.model_validate(
        await tenancy_service.get_organization(session, ctx)
    )


@router.patch("/organizations/{organization_id}", tags=["Organizations"])
async def update_organization(
    request: Request, body: OrganizationUpdate, ctx: ManageOrg, session: DbSession
) -> OrganizationRead:
    org = await tenancy_service.update_organization(session, ctx, body, request)
    return OrganizationRead.model_validate(org)


# --- Workspaces ---------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/workspaces",
    tags=["Workspaces"],
    summary="Workspaces visible to the current user",
)
async def list_workspaces(
    ctx: OrgContext, current: CurrentUserDep, session: DbSession
) -> list[WorkspaceRead]:
    resolved = await resolve_membership(session, current.user.id, ctx.organization_id)
    assert resolved is not None  # guaranteed by OrgContext
    return [
        WorkspaceRead.model_validate(w)
        for w in await visible_workspaces(session, resolved)
    ]


@router.post(
    "/organizations/{organization_id}/workspaces",
    tags=["Workspaces"],
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    request: Request, body: WorkspaceCreate, ctx: ManageOrg, session: DbSession
) -> WorkspaceRead:
    ws = await tenancy_service.create_workspace(session, ctx, body, request)
    return WorkspaceRead.model_validate(ws)


# --- Members ----------------------------------------------------------------


@router.get("/organizations/{organization_id}/members", tags=["Members"])
async def list_members(ctx: OrgContext, session: DbSession) -> list[MemberRead]:
    return await tenancy_service.list_members(session, ctx)


@router.patch(
    "/organizations/{organization_id}/members/{membership_id}",
    tags=["Members"],
    summary="Change a member's role and/or workspaces",
)
async def update_member(
    request: Request,
    membership_id: uuid.UUID,
    body: MemberUpdate,
    ctx: ManageMembers,
    session: DbSession,
) -> MemberRead:
    return await tenancy_service.update_member(
        session, ctx, membership_id, body, request
    )


@router.delete(
    "/organizations/{organization_id}/members/{membership_id}",
    tags=["Members"],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    request: Request, membership_id: uuid.UUID, ctx: ManageMembers, session: DbSession
) -> Response:
    await tenancy_service.remove_member(session, ctx, membership_id, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Invitations --------------------------------------------------------------


@router.post(
    "/organizations/{organization_id}/invitations",
    tags=["Invitations"],
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    request: Request, body: InvitationCreate, ctx: ManageMembers, session: DbSession
) -> InvitationRead:
    inv = await tenancy_service.create_invitation(session, ctx, body, request)
    return InvitationRead.model_validate(inv)


@router.get(
    "/organizations/{organization_id}/invitations",
    tags=["Invitations"],
    summary="Pending invitations",
)
async def list_invitations(
    ctx: ManageMembers, session: DbSession
) -> list[InvitationRead]:
    return [
        InvitationRead.model_validate(i)
        for i in await tenancy_service.list_invitations(session, ctx)
    ]


@router.delete(
    "/organizations/{organization_id}/invitations/{invitation_id}",
    tags=["Invitations"],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    request: Request, invitation_id: uuid.UUID, ctx: ManageMembers, session: DbSession
) -> Response:
    await tenancy_service.revoke_invitation(session, ctx, invitation_id, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/invitations/accept",
    tags=["Invitations"],
    summary="Accept an invitation (logged in, invitation sent to your email)",
)
async def accept_invitation(
    request: Request,
    body: AcceptInvitationRequest,
    current: CurrentUserDep,
    session: DbSession,
) -> MeResponse:
    await auth_service.accept_invitation(session, current.user, body.token, request)
    return MeResponse(
        user=UserRead.model_validate(current.user),
        memberships=await list_memberships(session, current.user.id),
    )


# --- Audit log -----------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/audit-logs",
    tags=["Organizations"],
    summary="Latest 100 audit log entries",
)
async def list_audit_logs(ctx: ViewAuditLog, session: DbSession) -> list[AuditLogRead]:
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.organization_id == ctx.organization_id)
        .order_by(AuditLog.id.desc())
        .limit(100)
    )
    return [AuditLogRead.model_validate(r) for r in rows]
