"""Request dependencies: authentication, tenant context and permissions.

    get_current_user        Bearer access token → active session → User
    get_org_context         /organizations/{organization_id}/...  (tenant "O")
    get_workspace_context   /workspaces/{workspace_id}/...        (tenant "W")
    require_permission(c)   403 unless the caller's role grants permission c

Resources of organizations/workspaces the caller doesn't belong to always
return 404 (never 403), so ids from other tenants can't be probed.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.exceptions import (
    NotFoundError,
    OrganizationSuspendedError,
    PermissionDeniedError,
    SessionRevokedError,
    UnauthenticatedError,
)
from app.core.security import decode_jwt, utcnow
from app.core.tenancy import Principal, TenantContext, bind_tenant
from app.models import User, UserSession, Workspace
from app.models.enums import MembershipRole, OrgStatus
from app.services.access import (
    ResolvedMembership,
    can_access_workspace,
    resolve_membership,
)

DbSession = Annotated[AsyncSession, Depends(get_db)]

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    principal: Principal


async def get_current_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None:
        raise UnauthenticatedError()
    claims = decode_jwt(credentials.credentials, "access")
    try:
        user_id = uuid.UUID(claims["sub"])
        session_id = uuid.UUID(claims["sid"])
    except (KeyError, ValueError):
        raise UnauthenticatedError("Invalid token") from None

    # The session must still be live: logout / password reset take effect
    # immediately instead of when the 15-minute access token expires.
    row = (
        await session.execute(
            select(User, UserSession)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
                UserSession.id == session_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise UnauthenticatedError()
    user, user_session = row
    if user_session.revoked_at is not None or user_session.expires_at <= utcnow():
        raise SessionRevokedError()
    return CurrentUser(user=user, principal=Principal(user_id, session_id))


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def _context(
    resolved: ResolvedMembership,
    principal: Principal,
    workspace_id: uuid.UUID | None,
) -> TenantContext:
    if resolved.organization.status != OrgStatus.ACTIVE:
        raise OrganizationSuspendedError()
    m = resolved.membership
    return TenantContext(
        organization_id=resolved.organization.id,
        workspace_id=workspace_id,
        principal=principal,
        membership_id=m.id,
        membership_role=m.role,
        custom_role_id=m.custom_role_id,
        permissions=resolved.permissions,
    )


async def get_org_context(
    organization_id: uuid.UUID, current: CurrentUserDep, session: DbSession
) -> TenantContext:
    resolved = await resolve_membership(session, current.user.id, organization_id)
    if resolved is None:
        raise NotFoundError()
    ctx = _context(resolved, current.principal, None)
    await bind_tenant(session, ctx.organization_id)
    return ctx


async def get_workspace_context(
    workspace_id: uuid.UUID, current: CurrentUserDep, session: DbSession
) -> TenantContext:
    org_id = await session.scalar(
        select(Workspace.organization_id).where(
            Workspace.id == workspace_id, Workspace.deleted_at.is_(None)
        )
    )
    if org_id is None:
        raise NotFoundError()
    resolved = await resolve_membership(session, current.user.id, org_id)
    if resolved is None or not await can_access_workspace(
        session, resolved, workspace_id
    ):
        raise NotFoundError()
    ctx = _context(resolved, current.principal, workspace_id)
    await bind_tenant(session, ctx.organization_id)
    return ctx


OrgContext = Annotated[TenantContext, Depends(get_org_context)]
WorkspaceContext = Annotated[TenantContext, Depends(get_workspace_context)]


def require_permission(
    code: str, *, scope: str = "organization"
) -> Callable[..., Awaitable[TenantContext]]:
    """Dependency factory: resolve the tenant context and require ``code``.

    ``scope="organization"`` for /organizations/{organization_id}/... routes,
    ``scope="workspace"`` for /workspaces/{workspace_id}/... routes.
    """
    if scope == "workspace":

        async def ws_checker(ctx: WorkspaceContext) -> TenantContext:
            if not ctx.has(code):
                raise PermissionDeniedError(code)
            return ctx

        return ws_checker

    async def org_checker(ctx: OrgContext) -> TenantContext:
        if not ctx.has(code):
            raise PermissionDeniedError(code)
        return ctx

    return org_checker


def require_owner() -> Callable[..., Awaitable[TenantContext]]:
    async def checker(ctx: OrgContext) -> TenantContext:
        if ctx.membership_role != MembershipRole.OWNER:
            raise PermissionDeniedError("owner")
        return ctx

    return checker
