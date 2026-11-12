from fastapi import APIRouter, Request

from app.core.constants import API_RATE_LIMIT
from app.core.rate_limit import limiter

router = APIRouter()


@router.get("")
@limiter.limit(API_RATE_LIMIT)
async def healthz(request: Request) -> str:
    return "ok!"
