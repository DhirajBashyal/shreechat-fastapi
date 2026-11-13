"""/api/v1/platform — platform admin (our staff) API (docs/12 §7).

Sees every organization. Requires a platform_admins row (404 otherwise).
Every change is audited with actor_kind = 'platform_admin'.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.deps import DbSession, PlatformAdminDep
from app.models.enums import OrgStatus
from app.schemas.platform import (
    OrganizationCounts,
    PlatformAdminGrant,
    PlatformAdminRead,
    PlatformOrganizationDetail,
    PlatformOrganizationList,
    PlatformOrganizationUpdate,
)
from app.services import platform as platform_service

router = APIRouter()


@router.get("/stats", summary="How many organizations exist, by status")
async def stats(admin: PlatformAdminDep, session: DbSession) -> OrganizationCounts:
    return await platform_service.organization_counts(session)


@router.get(
    "/organizations",
    summary="All organizations: search by name, slug or owner email; filter by status",
)
async def list_organizations(
    admin: PlatformAdminDep,
    session: DbSession,
    q: Annotated[str | None, Query(max_length=255)] = None,
    org_status: Annotated[OrgStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlatformOrganizationList:
    return await platform_service.list_organizations(
        session, q=q, status=org_status, limit=limit, offset=offset
    )


@router.get("/organizations/{organization_id}", summary="Organization detail")
async def get_organization(
    organization_id: uuid.UUID, admin: PlatformAdminDep, session: DbSession
) -> PlatformOrganizationDetail:
    return await platform_service.get_organization(session, organization_id)


@router.patch(
    "/organizations/{organization_id}",
    summary="Suspend / reactivate, override limits, set feature flags",
)
async def update_organization(
    request: Request,
    organization_id: uuid.UUID,
    body: PlatformOrganizationUpdate,
    admin: PlatformAdminDep,
    session: DbSession,
) -> PlatformOrganizationDetail:
    return await platform_service.update_organization(
        session, admin.user.id, organization_id, body, request
    )


@router.get("/admins", summary="Platform admins")
async def list_admins(
    admin: PlatformAdminDep, session: DbSession
) -> list[PlatformAdminRead]:
    return await platform_service.list_admins(session)


@router.post(
    "/admins",
    status_code=status.HTTP_201_CREATED,
    summary="Make an existing user a platform admin",
)
async def grant_admin(
    request: Request,
    body: PlatformAdminGrant,
    admin: PlatformAdminDep,
    session: DbSession,
) -> list[PlatformAdminRead]:
    await platform_service.grant_admin(
        session, body.email, granted_by=admin.user.id, request=request
    )
    return await platform_service.list_admins(session)


@router.delete(
    "/admins/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a platform admin (not yourself)",
)
async def revoke_admin(
    request: Request, user_id: uuid.UUID, admin: PlatformAdminDep, session: DbSession
) -> Response:
    await platform_service.revoke_admin(
        session, user_id, revoked_by=admin.user.id, request=request
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
