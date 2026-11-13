"""API errors in the format from docs/backend/API_DESIGN.md §1:

{"error": {"code": "...", "message": "...", "request_id": "..."}}
"""

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logger import request_ctx_var


class APIError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.extra = extra or {}
        super().__init__(self.message)


class UnauthenticatedError(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Authentication required"


class InvalidCredentialsError(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_credentials"
    message = "Invalid email or password"


class TokenExpiredError(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "token_expired"
    message = "Token has expired"


class SessionRevokedError(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "session_revoked"
    message = "Session has been revoked"


class PermissionDeniedError(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action"

    def __init__(self, permission: str | None = None) -> None:
        super().__init__(extra={"permission": permission} if permission else None)


class OrganizationSuspendedError(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "organization_suspended"
    message = "Organization is suspended"


class OrganizationPendingDeletionError(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "organization_pending_deletion"
    message = "Organization is scheduled for deletion"


class PasswordConfirmationError(APIError):
    """Wrong password on a re-authenticated action (transfer ownership,
    delete organization). Not 401: the session itself is still valid."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "password_incorrect"
    message = "Password is incorrect"


class PlanLimitReachedError(APIError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "plan_limit_reached"
    message = "Your plan limit has been reached"

    def __init__(self, limit: str, maximum: int) -> None:
        super().__init__(
            f"Your plan allows at most {maximum} {limit}",
            extra={"limit": limit, "max": maximum},
        )


class MfaRequiredError(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "mfa_required"
    message = "Multi-factor authentication is required"


class EmbeddingUnavailableError(APIError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "embedding_unavailable"
    message = "The embedding service is unavailable"


class FeatureNotInPlanError(APIError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "feature_not_in_plan"
    message = "Your plan doesn't include this feature"

    def __init__(self, feature: str) -> None:
        super().__init__(extra={"feature": feature})


class NotFoundError(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Not found"


class ConflictError(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "ConflictError"


class ValidationFailedError(APIError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"
    message = "Request validation failed"


def _error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    ctx = request_ctx_var.get()
    return {
        "error": {
            "code": code,
            "message": message,
            **extra,
            "request_id": ctx.request_id if ctx else None,
        }
    }


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    return JSONResponse(
        _error_body(exc.code, exc.message, **exc.extra), status_code=exc.status_code
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "issue": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        _error_body("validation_error", "Request validation failed", details=details),
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
