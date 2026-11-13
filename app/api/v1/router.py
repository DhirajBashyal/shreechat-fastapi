from fastapi import APIRouter

from app.api.v1.endpoints import (
    ai,
    auth,
    health,
    knowledge,
    organizations,
    platform,
    web_widgets,
    widget,
    workspaces,
)

api_router = APIRouter()

# Register v1 endpoint routers here.
api_router.include_router(health.router, prefix="/health", tags=["Health"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(organizations.router)
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["Workspaces"])
api_router.include_router(knowledge.router, prefix="/workspaces", tags=["Knowledge"])
api_router.include_router(ai.org_router)
api_router.include_router(ai.ws_router, prefix="/workspaces", tags=["AI"])
api_router.include_router(platform.router, prefix="/platform", tags=["Platform admin"])
api_router.include_router(
    web_widgets.router, prefix="/workspaces", tags=["Website widget inboxes"]
)
api_router.include_router(
    widget.router, prefix="/widget", tags=["Website widget (visitor)"]
)
