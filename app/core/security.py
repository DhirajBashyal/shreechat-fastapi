"""Passwords (argon2id), opaque tokens (SHA-256 hashed) and JWTs.

See docs/10_AUTHENTICATION_AND_RBAC.md §2.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.exceptions import TokenExpiredError, UnauthenticatedError

# argon2id, m=64 MiB, t=3, p=4 (docs §2.1). Hashes are re-computed on login
# when these parameters change.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# Verified against when the email is unknown, so login takes the same time
# whether or not the account exists.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")

JWT_ALGORITHM = "HS256"
_DEV_FALLBACK_SECRET = "dev-only-insecure-secret-change-me-" + "x" * 32


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(
            password_hash
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# ---------------------------------------------------------------------------
# Opaque tokens (refresh tokens, invitations): only the SHA-256 is stored
# ---------------------------------------------------------------------------


def generate_token(prefix: str = "") -> str:
    """256-bit random URL-safe token."""
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# JWTs
# ---------------------------------------------------------------------------


def _secret() -> str:
    if settings.JWT_SECRET_KEY:
        return settings.JWT_SECRET_KEY
    if settings.is_local:
        return _DEV_FALLBACK_SECRET
    raise RuntimeError("JWT_SECRET_KEY must be set outside local development")


def encode_jwt(claims: dict[str, Any], ttl: timedelta) -> str:
    now = utcnow()
    payload = {
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def decode_jwt(token: str, expected_type: str) -> dict[str, Any]:
    """Verify signature, exp, iat, iss and typ. Algorithms are pinned."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _secret(),
            algorithms=[JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError() from e
    except jwt.PyJWTError as e:
        raise UnauthenticatedError("Invalid token") from e
    if payload.get("typ") != expected_type:
        raise UnauthenticatedError("Invalid token type")
    return payload


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    """Access tokens carry NO tenant or role claims (docs §2.2): memberships
    and permissions are resolved per request."""
    return encode_jwt(
        {"sub": str(user_id), "sid": str(session_id), "typ": "access"},
        timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
    )


def password_fingerprint(password_hash: str | None) -> str:
    """Short digest of the current password hash. Embedded in reset tokens so a
    token stops working as soon as the password changes (single use)."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def create_password_reset_token(user_id: uuid.UUID, password_hash: str | None) -> str:
    return encode_jwt(
        {
            "sub": str(user_id),
            "typ": "password_reset",
            "pfp": password_fingerprint(password_hash),
        },
        timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
    )
