"""Authentication flows (docs/10_AUTHENTICATION_AND_RBAC.md §2, docs/09 §8).

Sessions: a login creates a ``user_sessions`` row holding the SHA-256 of an
opaque refresh token. Refreshing rotates the token inside the same
``family_id``; presenting an already-rotated token revokes the whole family
(token theft signal).

Remember me: decides the refresh window. Checked = 30 days sliding,
unchecked = 24 hours sliding; either way a family can't outlive 90 days.
"""

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    InvalidCredentialsError,
    NotFoundError,
    SessionRevokedError,
    TokenExpiredError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.core.logger import get_logger
from app.core.security import (
    constant_time_equals,
    create_access_token,
    create_password_reset_token,
    decode_jwt,
    generate_token,
    hash_password,
    hash_token,
    password_fingerprint,
    password_needs_rehash,
    utcnow,
    verify_password,
)
from app.models import (
    Invitation,
    Organization,
    OrganizationMembership,
    Subscription,
    SubscriptionPlan,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from app.models.enums import MembershipRole, SubscriptionState
from app.schemas.auth import LoginRequest, SignupRequest
from app.services.audit import audit, client_ip
from app.services.mailer import send_email

logger = get_logger()

REFRESH_TOKEN_PREFIX = "rt_"


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    user: User
    session: UserSession
    access_token: str
    refresh_token: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:50] or "org"


async def _unique_org_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug = base
    while await session.scalar(
        select(Organization.id).where(
            Organization.slug == slug, Organization.deleted_at.is_(None)
        )
    ):
        slug = f"{base}-{secrets.token_hex(3)}"
    return slug


async def get_active_user_by_email(session: AsyncSession, email: str) -> User | None:
    # users.email is citext, so this comparison is case-insensitive.
    return await session.scalar(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )


def _refresh_ttl(remember_me: bool) -> timedelta:
    if remember_me:
        return timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    return timedelta(hours=settings.REFRESH_TOKEN_SHORT_TTL_HOURS)


async def _issue_session(
    session: AsyncSession,
    user: User,
    request: Request | None,
    *,
    ttl: timedelta,
    family_id: uuid.UUID | None = None,
    hard_deadline: datetime | None = None,
) -> tuple[UserSession, str]:
    now = utcnow()
    expires_at = now + ttl
    if hard_deadline is not None:
        expires_at = min(expires_at, hard_deadline)
    refresh_token = generate_token(REFRESH_TOKEN_PREFIX)
    user_session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash=hash_token(refresh_token),
        family_id=family_id or uuid.uuid4(),
        user_agent=request.headers.get("user-agent") if request else None,
        ip_address=client_ip(request),
        expires_at=expires_at,
        created_at=now,
        last_used_at=now,
    )
    session.add(user_session)
    return user_session, refresh_token


async def _tokens_for(
    session: AsyncSession, user: User, request: Request | None, remember_me: bool
) -> IssuedTokens:
    user_session, refresh_token = await _issue_session(
        session, user, request, ttl=_refresh_ttl(remember_me)
    )
    return IssuedTokens(
        user=user,
        session=user_session,
        access_token=create_access_token(user.id, user_session.id),
        refresh_token=refresh_token,
    )


async def _revoke_user_sessions(
    session: AsyncSession, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None
) -> None:
    stmt = (
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    if except_id is not None:
        stmt = stmt.where(UserSession.id != except_id)
    await session.execute(stmt)


async def _load_open_invitation(session: AsyncSession, token: str) -> Invitation:
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.token_hash == hash_token(token))
        .with_for_update()
    )
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.expires_at <= utcnow()
    ):
        raise NotFoundError("Invitation is invalid or has expired")
    org = await session.get(Organization, invitation.organization_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("Invitation is invalid or has expired")
    return invitation


async def join_organization_from_invitation(
    session: AsyncSession, user: User, invitation: Invitation
) -> OrganizationMembership:
    existing = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == invitation.organization_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    if existing is not None:
        raise ConflictError("You are already a member of this organization")
    membership = OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=invitation.organization_id,
        user_id=user.id,
        role=invitation.role,
        custom_role_id=invitation.custom_role_id,
        invited_by=invitation.invited_by,
    )
    session.add(membership)
    invitation.accepted_at = utcnow()
    return membership


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


async def signup(
    session: AsyncSession, data: SignupRequest, request: Request
) -> IssuedTokens:
    """Register a user.

    Without an invitation: creates user + organization + owner membership +
    default workspace + trial subscription, in one transaction (docs/09 §8).
    With an invitation: creates the user and joins the inviting organization.
    """
    if await get_active_user_by_email(session, data.email):
        raise ConflictError(
            "An account with this email already exists", code="duplicate"
        )

    invitation: Invitation | None = None
    if data.invitation_token:
        invitation = await _load_open_invitation(session, data.invitation_token)
        if invitation.email.lower() != data.email.lower():
            raise ValidationFailedError(
                "Invitation was sent to a different email address"
            )
    elif not data.organization_name:
        raise ValidationFailedError("organization_name is required")

    now = utcnow()
    user = User(
        id=uuid.uuid4(),
        email=data.email,
        name=data.name,
        password_hash=hash_password(data.password),
        # The invitation email proves the address.
        email_verified_at=now if invitation else None,
        last_login_at=now,
    )
    session.add(user)
    await session.flush()

    if invitation is not None:
        membership = await join_organization_from_invitation(session, user, invitation)
        audit(
            session,
            action="invitation.accepted",
            resource_type="organization_membership",
            resource_id=membership.id,
            actor_id=user.id,
            organization_id=invitation.organization_id,
            request=request,
        )
    else:
        assert data.organization_name is not None
        org = Organization(
            id=uuid.uuid4(),
            name=data.organization_name,
            slug=await _unique_org_slug(session, data.organization_name),
            created_by=user.id,
        )
        session.add(org)
        await session.flush()
        membership = OrganizationMembership(
            id=uuid.uuid4(),
            organization_id=org.id,
            user_id=user.id,
            role=MembershipRole.OWNER,
        )
        workspace = Workspace(
            id=uuid.uuid4(), organization_id=org.id, name="Main", slug="main"
        )
        session.add_all([membership, workspace])
        await session.flush()
        session.add(
            WorkspaceMembership(
                organization_id=org.id,
                workspace_id=workspace.id,
                membership_id=membership.id,
            )
        )
        plan_id = await session.scalar(
            select(SubscriptionPlan.id).where(
                SubscriptionPlan.code == settings.TRIAL_PLAN_CODE
            )
        )
        if plan_id is not None:
            trial_end = now + timedelta(days=settings.TRIAL_DAYS)
            session.add(
                Subscription(
                    organization_id=org.id,
                    plan_id=plan_id,
                    state=SubscriptionState.TRIALING,
                    current_period_start=now,
                    current_period_end=trial_end,
                    trial_ends_at=trial_end,
                )
            )
        else:
            logger.warning(
                "Trial plan not found; organization created without a subscription",
                extra={"plan_code": settings.TRIAL_PLAN_CODE},
            )
        audit(
            session,
            action="organization.created",
            resource_type="organization",
            resource_id=org.id,
            actor_id=user.id,
            organization_id=org.id,
            request=request,
        )

    tokens = await _tokens_for(session, user, request, data.remember_me)
    audit(
        session,
        action="auth.signup",
        resource_type="user",
        resource_id=user.id,
        actor_id=user.id,
        request=request,
    )
    await session.commit()
    return tokens


async def login(
    session: AsyncSession, data: LoginRequest, request: Request
) -> IssuedTokens:
    user = await get_active_user_by_email(session, data.email)
    # verify_password checks a dummy hash when the user is unknown, so both
    # failure cases take the same time and return the same error.
    if not verify_password(data.password, user.password_hash if user else None):
        audit(
            session,
            action="auth.login_failed",
            resource_type="user",
            resource_id=user.id if user else None,
            actor_kind="system",
            changes={"email": data.email},
            request=request,
        )
        await session.commit()
        raise InvalidCredentialsError()
    assert user is not None and user.password_hash is not None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    user.last_login_at = utcnow()
    tokens = await _tokens_for(session, user, request, data.remember_me)
    audit(
        session,
        action="auth.login",
        resource_type="user_session",
        resource_id=tokens.session.id,
        actor_id=user.id,
        changes={"remember_me": data.remember_me},
        request=request,
    )
    await session.commit()
    return tokens


async def refresh(
    session: AsyncSession, refresh_token: str, request: Request
) -> IssuedTokens:
    """Rotate a refresh token. Reusing a rotated token revokes its family."""
    current = await session.scalar(
        select(UserSession)
        .where(UserSession.refresh_token_hash == hash_token(refresh_token))
        .with_for_update()
    )
    if current is None:
        raise UnauthenticatedError("Invalid refresh token")

    now = utcnow()
    if current.revoked_at is not None:
        # Reuse of a rotated/revoked token = theft signal: kill the family.
        await session.execute(
            update(UserSession)
            .where(
                UserSession.family_id == current.family_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        audit(
            session,
            action="auth.refresh_reuse_detected",
            resource_type="user_session",
            resource_id=current.id,
            actor_id=current.user_id,
            request=request,
        )
        await session.commit()
        raise SessionRevokedError()
    if current.expires_at <= now:
        raise TokenExpiredError("Refresh token has expired")

    user = await session.get(User, current.user_id)
    if user is None or user.deleted_at is not None:
        raise UnauthenticatedError()

    # Keep the same sliding window (remember me or not), capped by the
    # family's absolute lifetime.
    family_started = await session.scalar(
        select(func.min(UserSession.created_at)).where(
            UserSession.family_id == current.family_id
        )
    )
    hard_deadline = (family_started or now) + timedelta(
        days=settings.REFRESH_TOKEN_ABSOLUTE_MAX_DAYS
    )
    new_session, new_refresh = await _issue_session(
        session,
        user,
        request,
        ttl=current.expires_at - current.created_at,
        family_id=current.family_id,
        hard_deadline=hard_deadline,
    )
    if new_session.expires_at <= now:
        raise SessionRevokedError("Session reached its maximum lifetime")
    await session.flush()
    current.revoked_at = now
    current.replaced_by_id = new_session.id
    await session.commit()
    return IssuedTokens(
        user=user,
        session=new_session,
        access_token=create_access_token(user.id, new_session.id),
        refresh_token=new_refresh,
    )


async def logout(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, request: Request
) -> None:
    await session.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    audit(
        session,
        action="auth.logout",
        resource_type="user_session",
        resource_id=session_id,
        actor_id=user_id,
        request=request,
    )
    await session.commit()


async def logout_all(
    session: AsyncSession, user_id: uuid.UUID, request: Request
) -> None:
    await _revoke_user_sessions(session, user_id)
    audit(
        session,
        action="auth.logout_all",
        resource_type="user",
        resource_id=user_id,
        actor_id=user_id,
        request=request,
    )
    await session.commit()


async def forgot_password(session: AsyncSession, email: str, request: Request) -> None:
    """Email a reset link. The caller always answers 202, so the response never
    reveals whether the email has an account."""
    user = await get_active_user_by_email(session, email)
    if user is None:
        return
    token = create_password_reset_token(user.id, user.password_hash)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}"
    await send_email(
        user.email,
        "Reset your password",
        f"Hi {user.name},\n\nUse this link to set a new password. It expires in "
        f"{settings.PASSWORD_RESET_TTL_MINUTES} minutes and works once:\n\n{link}\n\n"
        "If you didn't ask for this, you can ignore this email.",
    )
    audit(
        session,
        action="auth.password_reset_requested",
        resource_type="user",
        resource_id=user.id,
        actor_id=user.id,
        request=request,
    )
    await session.commit()


async def reset_password(
    session: AsyncSession, token: str, new_password: str, request: Request
) -> None:
    invalid = UnauthenticatedError("Reset link is invalid or has expired")
    try:
        claims = decode_jwt(token, "password_reset")
        user_id = uuid.UUID(claims["sub"])
    except (TokenExpiredError, UnauthenticatedError, ValueError, KeyError):
        raise invalid from None

    user = await session.get(User, user_id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise invalid
    # Fingerprint of the hash at issue time: once the password changes the
    # token no longer matches, which makes it single use.
    if not constant_time_equals(
        str(claims.get("pfp", "")), password_fingerprint(user.password_hash)
    ):
        raise invalid

    user.password_hash = hash_password(new_password)
    await _revoke_user_sessions(session, user.id)
    audit(
        session,
        action="auth.password_reset",
        resource_type="user",
        resource_id=user.id,
        actor_id=user.id,
        request=request,
    )
    await session.commit()


async def accept_invitation(
    session: AsyncSession, user: User, token: str, request: Request
) -> OrganizationMembership:
    """A logged-in user accepts an invitation sent to their email."""
    invitation = await _load_open_invitation(session, token)
    if invitation.email.lower() != user.email.lower():
        raise NotFoundError("Invitation is invalid or has expired")
    membership = await join_organization_from_invitation(session, user, invitation)
    audit(
        session,
        action="invitation.accepted",
        resource_type="organization_membership",
        resource_id=membership.id,
        actor_id=user.id,
        organization_id=invitation.organization_id,
        request=request,
    )
    await session.commit()
    return membership
