"""Website widget inbox settings (docs/backend/API_DESIGN.md §12):

    POST /api/v1/workspaces/{ws}/channels/web-widget          create
    GET|PATCH|DELETE /api/v1/workspaces/{ws}/inboxes/{id}/web-widget
    POST /api/v1/workspaces/{ws}/inboxes/{id}/web-widget/identity-secret/rotate


docs/integrations/WEBSITE_CHAT.md §5, §7. All routes need ``inbox.manage``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import DbSession, require_permission
from app.core.tenancy import TenantContext
from app.schemas.widget import (
    IdentitySecretResponse,
    WebWidgetCreate,
    WebWidgetCreated,
    WebWidgetRead,
    WebWidgetSettings,
)
from app.services import web_widgets as service

router = APIRouter()

ManageInbox = Annotated[
    TenantContext, Depends(require_permission("inbox.manage", scope="workspace"))
]
ONE = "/{workspace_id}/inboxes/{inbox_id}/web-widget"


@router.post(
    "/{workspace_id}/channels/web-widget",
    status_code=status.HTTP_201_CREATED,
    summary="Create a website widget inbox (identity secret shown once)",
)
async def create_web_widget(
    body: WebWidgetCreate, ctx: ManageInbox, session: DbSession
) -> WebWidgetCreated:
    return await service.create(session, ctx, body)


@router.get(ONE, summary="Widget settings + embed code")
async def get_web_widget(
    inbox_id: uuid.UUID, ctx: ManageInbox, session: DbSession
) -> WebWidgetRead:
    return await service.get(session, ctx, inbox_id)


@router.patch(ONE, summary="Change widget settings")
async def update_web_widget(
    inbox_id: uuid.UUID, body: WebWidgetSettings, ctx: ManageInbox, session: DbSession
) -> WebWidgetRead:
    return await service.update(session, ctx, inbox_id, body)


@router.post(
    ONE + "/identity-secret/rotate",
    summary="Rotate the HMAC identity secret (shown once)",
)
async def rotate_identity_secret(
    inbox_id: uuid.UUID, ctx: ManageInbox, session: DbSession
) -> IdentitySecretResponse:
    return IdentitySecretResponse(
        identity_secret=await service.rotate_identity_secret(session, ctx, inbox_id)
    )


@router.delete(
    ONE, status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect the widget"
)
async def disconnect_web_widget(
    inbox_id: uuid.UUID, ctx: ManageInbox, session: DbSession
) -> Response:
    await service.disconnect(session, ctx, inbox_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
