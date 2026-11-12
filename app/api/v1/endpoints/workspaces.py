"""/api/v1/workspaces/{workspace_id} — workspace-scoped routes (tenant "W").

Owners and administrators can open every workspace of their organization;
agents only the workspaces they were added to. Anything else is a 404.
"""

from fastapi import APIRouter

from app.api.deps import DbSession, WorkspaceContext
from app.schemas.tenancy import MemberRead, WorkspaceRead
from app.services import tenancy as tenancy_service

router = APIRouter()


@router.get("/{workspace_id}", summary="Workspace details")
async def get_workspace(ctx: WorkspaceContext, session: DbSession) -> WorkspaceRead:
    return WorkspaceRead.model_validate(
        await tenancy_service.get_workspace(session, ctx)
    )


@router.get("/{workspace_id}/members", summary="Members working in this workspace")
async def list_workspace_members(
    ctx: WorkspaceContext, session: DbSession
) -> list[MemberRead]:
    return await tenancy_service.list_members(
        session, ctx, workspace_id=ctx.require_workspace()
    )
