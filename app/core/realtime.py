"""Realtime publishing over Redis pub/sub (docs/backend/REALTIME_COMMUNICATION.md).

Channels (§3):

    rt:org:{organization_id}        agents' sockets (filtered per socket by visibility)
    rt:contact:{realtime_token}     website widget sockets of one contact identity

Publish only after the database commit (the outbox relay does that), so
clients never see uncommitted data. Pub/sub is at-most-once: clients refetch
over REST after reconnecting (§7).
"""

import json
import uuid
from typing import Any

from redis.asyncio import Redis

from app.core.logger import get_logger
from app.core.security import utcnow

logger = get_logger()


def org_channel(organization_id: uuid.UUID | str) -> str:
    return f"rt:org:{organization_id}"


def contact_channel(realtime_token: str) -> str:
    return f"rt:contact:{realtime_token}"


def envelope(
    event: str,
    data: dict[str, Any],
    *,
    workspace_id: uuid.UUID | str | None = None,
    audience: str = "members",
) -> dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "event": event,
        "ts": utcnow().isoformat().replace("+00:00", "Z"),
        "workspace_id": str(workspace_id) if workspace_id else None,
        "audience": audience,
        "data": data,
    }


async def publish(
    redis: Redis,
    channel: str,
    event: str,
    data: dict[str, Any],
    *,
    workspace_id: uuid.UUID | str | None = None,
    audience: str = "members",
) -> bool:
    """Publish one event. Returns False (and logs) when Redis is unavailable."""
    body = json.dumps(
        envelope(event, data, workspace_id=workspace_id, audience=audience),
        default=str,
    )
    try:
        await redis.publish(channel, body)
    except Exception as exc:
        logger.warning(
            "Realtime publish failed",
            extra={"channel": channel.split(":")[1], "error": repr(exc)[:200]},
        )
        return False
    return True
