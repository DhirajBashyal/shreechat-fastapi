import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import MembershipRole

PASSWORD_MIN_LENGTH = 10  # docs/10_AUTHENTICATION_AND_RBAC.md §2.1


def _check_password(v: str) -> str:
    if len(v) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"must be at least {PASSWORD_MIN_LENGTH} characters")
    if v.strip() != v:
        raise ValueError("must not start or end with whitespace")
    return v


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(max_length=128)
    # Required unless signing up through an invitation.
    organization_name: str | None = Field(default=None, min_length=1, max_length=255)
    # Join an existing organization instead of creating one.
    invitation_token: str | None = None
    remember_me: bool = False

    _validate_password = field_validator("password")(_check_password)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)
    remember_me: bool = False


class RefreshRequest(BaseModel):
    # Mobile/non-browser clients send it in the body; browsers use the cookie.
    refresh_token: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(max_length=128)

    _validate_password = field_validator("new_password")(_check_password)


class AcceptInvitationRequest(BaseModel):
    token: str


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str | None
    email: str
    email_verified_at: datetime | None


class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class MembershipRead(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    membership_id: uuid.UUID
    role: MembershipRole
    custom_role_id: uuid.UUID | None
    permissions: list[str]
    workspaces: list[WorkspaceSummary]


class MeResponse(BaseModel):
    user: UserRead
    memberships: list[MembershipRead]


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    # Omitted (null) for browser clients (X-Client-Type: web): they get it
    # as an HttpOnly cookie instead.
    refresh_token: str | None
    refresh_expires_at: datetime
    remember_me: bool


class TokenResponse(RefreshResponse, MeResponse):
    pass


class MessageResponse(BaseModel):
    message: str
