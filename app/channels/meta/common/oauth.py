"""OAuth helpers for Messenger (Facebook Login) and Instagram (Instagram Login).

``state`` is 32 random bytes stored in Redis for 10 minutes and deleted on
first use (CSRF protection, binds the callback to the user + workspace that
started it). After the callback, the provider data (Pages, tokens) stays
server-side behind a ``selection_token`` for 10 minutes: tokens never reach
the browser.
"""

import json
import secrets
from typing import Any
from urllib.parse import urlencode

from redis.asyncio import Redis

from app.core.config import settings

STATE_TTL = 600
SELECTION_TTL = 600

MESSENGER_SCOPES = (
    "pages_messaging",
    "pages_show_list",
    "pages_manage_metadata",
    "business_management",
)
INSTAGRAM_SCOPES = ("instagram_business_basic", "instagram_business_manage_messages")


def redirect_uri(provider: str) -> str:
    base = settings.PUBLIC_API_URL.rstrip("/") + settings.API_V1_PREFIX
    return f"{base}/oauth/{'instagram' if provider == 'instagram' else 'meta'}/callback"


async def _put(redis: Redis, key: str, data: dict[str, Any], ttl: int) -> None:
    await redis.set(key, json.dumps(data, default=str), ex=ttl)


async def _take(redis: Redis, key: str) -> dict[str, Any] | None:
    raw = await redis.getdel(key)
    if raw is None:
        return None
    value = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    return value if isinstance(value, dict) else None


async def create_state(redis: Redis, data: dict[str, Any]) -> str:
    state = secrets.token_urlsafe(32)
    await _put(redis, f"oauth:{state}", data, STATE_TTL)
    return state


async def consume_state(redis: Redis, state: str) -> dict[str, Any] | None:
    """Single use: a replayed state returns None."""
    if not state or len(state) > 100:
        return None
    return await _take(redis, f"oauth:{state}")


async def create_selection(redis: Redis, data: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    await _put(redis, f"oauth:selection:{token}", data, SELECTION_TTL)
    return token


async def read_selection(redis: Redis, token: str) -> dict[str, Any] | None:
    if not token or len(token) > 100:
        return None
    raw = await redis.get(f"oauth:selection:{token}")
    if raw is None:
        return None
    value = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    return value if isinstance(value, dict) else None


async def drop_selection(redis: Redis, token: str) -> None:
    await redis.delete(f"oauth:selection:{token}")


def authorize_url(provider: str, state: str) -> str:
    if provider == "instagram":
        query = {
            "client_id": settings.META_INSTAGRAM_APP_ID,
            "redirect_uri": redirect_uri(provider),
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
            "state": state,
        }
        return "https://www.instagram.com/oauth/authorize?" + urlencode(query)
    query = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": redirect_uri(provider),
        "state": state,
        "scope": ",".join(MESSENGER_SCOPES),
        "response_type": "code",
    }
    return (
        f"https://www.facebook.com/{settings.META_GRAPH_API_VERSION}/dialog/oauth?"
        + urlencode(query)
    )
