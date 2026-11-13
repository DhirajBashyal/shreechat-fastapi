from contextlib import asynccontextmanager
from typing import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis import asyncio as aioredis
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import api_router as api_v1_router
from app.api.widget_ws import router as widget_ws_router
from app.core import metrics
from app.core.config import settings
from app.core.db import sessionmanager
from app.core.exceptions import APIError, api_error_handler, validation_error_handler
from app.core.logger import RequestContextVar, get_logger, request_ctx_var
from app.core.rate_limit import limiter
from app.schemas.common import ErrorResponseSchema

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize db pool
    if not sessionmanager.session_factory:
        sessionmanager.init_db()
    # Initialize redis
    app.state.redis = await aioredis.from_url(settings.REDIS_URL)
    yield
    await sessionmanager.close()
    await app.state.redis.aclose()


app = FastAPI(
    title="Shree Chat AI",
    lifespan=lifespan,
    responses={
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponseSchema,
            "description": "Rate Limit Response",
        }
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter

app.add_exception_handler(APIError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceed_handler(request: Request, exc: RateLimitExceeded):
    response = JSONResponse(
        {
            "error": {
                "code": "rate_limited",
                "message": "Too many requests, please try again later",
            }
        },
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    response = request.app.state.limiter._inject_headers(
        response, getattr(request.state, "view_rate_limit", None)
    )
    return response


WIDGET_PREFIX = f"{settings.API_V1_PREFIX}/widget"
WIDGET_MAX_BODY_BYTES = 64 * 1024


@app.middleware("http")
async def widget_body_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Visitor requests are capped at 64 KB (docs/integrations/WEBSITE_CHAT.md §3)."""
    if request.url.path.startswith(WIDGET_PREFIX):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > WIDGET_MAX_BODY_BYTES:
            return JSONResponse(
                {
                    "error": {
                        "code": "payload_too_large",
                        "message": "Request body is limited to 64 KB",
                    }
                },
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
    return await call_next(request)


@app.middleware("http")
async def logging_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = str(uuid4())
    request_path = f"{request.method} {request.url.path}"
    request_ctx_var.set(
        RequestContextVar(request_id=request_id, request_path=request_path)
    )

    extra = {}
    if settings.ENV == "local":
        extra["query"] = request.query_params  # type: ignore
    logger.info("Request log", extra=extra)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    """Prometheus scrape endpoint (docs/ai/AI_ARCHITECTURE.md §8). Disabled with
    METRICS_ENABLED=false; protected by METRICS_TOKEN when set."""
    if not settings.METRICS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if (
        settings.METRICS_TOKEN
        and request.headers.get("authorization") != f"Bearer {settings.METRICS_TOKEN}"
    ):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/", tags=["Health"], include_in_schema=False)
async def root() -> str:
    """Unversioned liveness probe for load balancers."""
    return "ok!"


app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)
app.include_router(widget_ws_router)
