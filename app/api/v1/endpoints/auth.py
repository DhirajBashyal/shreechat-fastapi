"""/api/v1/auth — register, login, refresh, logout, password reset, me."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Request, Response, status

from app.api.deps import CurrentUserDep, DbSession
from app.core.config import settings
from app.core.exceptions import UnauthenticatedError
from app.core.rate_limit import limiter
from app.core.security import utcnow
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    MeResponse,
    MessageResponse,
    RefreshRequest,
    RefreshResponse,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserRead,
)
from app.services import auth as auth_service
from app.services.access import list_memberships

router = APIRouter()

REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def _is_web(client_type: str | None) -> bool:
    return (client_type or "").lower() == "web"


def _is_remembered(tokens: auth_service.IssuedTokens) -> bool:
    window = tokens.session.expires_at - tokens.session.created_at
    return window > timedelta(hours=settings.REFRESH_TOKEN_SHORT_TTL_HOURS)


def _set_refresh_cookie(
    response: Response, tokens: auth_service.IssuedTokens, remember: bool
) -> None:
    max_age = int((tokens.session.expires_at - utcnow()).total_seconds())
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        # Remember me → persistent cookie; otherwise a browser-session cookie.
        max_age=max_age if remember else None,
        httponly=True,
        secure=not settings.is_local,
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=not settings.is_local,
        samesite="strict",
    )


def _token_fields(
    tokens: auth_service.IssuedTokens, response: Response, web: bool
) -> dict[str, object]:
    remember = _is_remembered(tokens)
    _set_refresh_cookie(response, tokens, remember)
    return {
        "access_token": tokens.access_token,
        "expires_in": settings.ACCESS_TOKEN_TTL_MINUTES * 60,
        "refresh_token": None if web else tokens.refresh_token,
        "refresh_expires_at": tokens.session.expires_at,
        "remember_me": remember,
    }


async def _token_response(
    session: DbSession,
    tokens: auth_service.IssuedTokens,
    response: Response,
    web: bool,
) -> TokenResponse:
    return TokenResponse(
        **_token_fields(tokens, response, web),  # type: ignore[arg-type]
        user=UserRead.model_validate(tokens.user),
        memberships=await list_memberships(session, tokens.user.id),
    )


ClientType = Annotated[
    str | None,
    Header(
        alias="X-Client-Type",
        description="Send `web` from browsers: the refresh token is then only set as an HttpOnly cookie",
    ),
]


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    summary="Register (new organization, or join one with an invitation token)",
)
@limiter.limit("10/hour")
async def signup(
    request: Request,
    response: Response,
    body: SignupRequest,
    session: DbSession,
    client_type: ClientType = None,
) -> TokenResponse:
    tokens = await auth_service.signup(session, body, request)
    return await _token_response(session, tokens, response, _is_web(client_type))


@router.post("/login", summary="Log in with email and password")
@limiter.limit("5/5minutes")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    session: DbSession,
    client_type: ClientType = None,
) -> TokenResponse:
    tokens = await auth_service.login(session, body, request)
    return await _token_response(session, tokens, response, _is_web(client_type))


@router.post("/refresh", summary="Rotate the refresh token, get a new access token")
@limiter.limit("30/minute")
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    body: RefreshRequest | None = None,
    refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    x_requested_with: Annotated[str | None, Header()] = None,
    client_type: ClientType = None,
) -> RefreshResponse:
    token = body.refresh_token if body and body.refresh_token else None
    if token is None and refresh_cookie:
        # Cookie-based refresh needs a custom header as CSRF protection
        # (a cross-site form post can't set it). docs/10 §8.
        if not x_requested_with:
            raise UnauthenticatedError("X-Requested-With header required")
        token = refresh_cookie
    if not token:
        raise UnauthenticatedError("Refresh token required")
    tokens = await auth_service.refresh(session, token, request)
    return RefreshResponse(**_token_fields(tokens, response, _is_web(client_type)))  # type: ignore[arg-type]


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out of this session",
)
async def logout(
    request: Request, current: CurrentUserDep, session: DbSession
) -> Response:
    await auth_service.logout(
        session, current.user.id, current.principal.session_id, request
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out of every session on every device",
)
async def logout_all(
    request: Request, current: CurrentUserDep, session: DbSession
) -> Response:
    await auth_service.logout_all(session, current.user.id, request)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password reset link (always 202)",
)
@limiter.limit("5/15minutes")
async def forgot_password(
    request: Request, body: ForgotPasswordRequest, session: DbSession
) -> MessageResponse:
    await auth_service.forgot_password(session, body.email, request)
    return MessageResponse(
        message="If an account exists for this email, a reset link has been sent."
    )


@router.post("/password/reset", summary="Set a new password with a reset token")
@limiter.limit("10/15minutes")
async def reset_password(
    request: Request, body: ResetPasswordRequest, session: DbSession
) -> MessageResponse:
    await auth_service.reset_password(session, body.token, body.new_password, request)
    return MessageResponse(
        message="Password updated. All sessions were signed out; please log in again."
    )


@router.get("/me", summary="Current user, organizations, roles and workspaces")
async def me(current: CurrentUserDep, session: DbSession) -> MeResponse:
    return MeResponse(
        user=UserRead.model_validate(current.user),
        memberships=await list_memberships(session, current.user.id),
    )
