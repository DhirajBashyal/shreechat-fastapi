from fastapi import APIRouter

from app.api.v1.endpoints import auth, health, organizations, workspaces

api_router = APIRouter()

# Register v1 endpoint routers here.
api_router.include_router(health.router, prefix="/health", tags=["Health"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(organizations.router)
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["Workspaces"])
