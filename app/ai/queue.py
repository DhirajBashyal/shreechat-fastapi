"""The ``ai`` queue (docs/ai/AI_ARCHITECTURE.md §3, LANGGRAPH_WORKFLOW.md §7).

Redis keys:

    ai:due                    sorted set: conversation_id → when to run (epoch s)
    ai:debounce:{conv}        latest incoming message id (TTL 30 s)
    ai:lock:{conv}            one run per conversation at a time
    ai:running:{org}          runs in progress for the organization

``enqueue_incoming`` is called for every incoming message (by the inbound
pipeline; manually with ``make ai-reply``). Each call pushes the conversation's
due time to now + AI_DEBOUNCE_SECONDS and stores the message id, so a burst of
messages ("hi" / "I ordered last week" / "where is it?") produces one run for
the last one, which reads all unanswered messages anyway. The worker
(``make ai-worker``) claims due conversations, takes the per-conversation lock
and an organization slot, and runs ``on_incoming_message``. Busy conversations
and organizations at their limit are re-scheduled, not dropped.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.logger import get_logger
from app.models import Conversation

logger = get_logger()

DUE = "ai:due"
DEBOUNCE_TTL = 30
LOCK_TTL = settings.AI_RUN_DEADLINE_SECONDS + 30
RETRY_BUSY_SECONDS = 1.0
RETRY_LIMIT_SECONDS = 2.0

Handler = Callable[[AsyncSession, uuid.UUID], Awaitable[Any]]


def _debounce(conv: str) -> str:
    return f"ai:debounce:{conv}"


async def enqueue_incoming(
    redis: Redis,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    now: float | None = None,
) -> None:
    conv = str(conversation_id)
    due = (now or time.time()) + settings.AI_DEBOUNCE_SECONDS
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(_debounce(conv), str(message_id), ex=DEBOUNCE_TTL)
        pipe.zadd(DUE, {conv: due})  # last message wins: the due time moves
        await pipe.execute()


async def _reschedule(redis: Redis, conv: str, delay: float, now: float) -> None:
    # NX: a newer message may already have scheduled it later; keep that.
    await redis.zadd(DUE, {conv: now + delay}, nx=True)


async def process_due(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    handler: Handler,
    *,
    now: float | None = None,
    batch: int = 20,
) -> int:
    """Run every conversation that is due. Returns how many runs started."""
    now = now or time.time()
    started = 0
    for raw in await redis.zrangebyscore(DUE, "-inf", now, start=0, num=batch):
        conv = raw.decode() if isinstance(raw, bytes) else str(raw)
        if not await redis.zrem(DUE, conv):
            continue  # another worker claimed it
        message_id = await redis.get(_debounce(conv))
        if message_id is None:
            continue  # expired: nothing to answer
        lock = f"ai:lock:{conv}"
        if not await redis.set(lock, "1", nx=True, ex=LOCK_TTL):
            await _reschedule(redis, conv, RETRY_BUSY_SECONDS, now)
            continue
        org_key: str | None = None
        try:
            async with session_factory() as session:
                org_id = await session.scalar(
                    select(Conversation.organization_id).where(
                        Conversation.id == uuid.UUID(conv)
                    )
                )
            if org_id is None:
                continue
            org_key = f"ai:running:{org_id}"
            running = await redis.incr(org_key)
            await redis.expire(org_key, LOCK_TTL)
            if running > settings.AI_MAX_CONCURRENT_RUNS_PER_ORG:
                await redis.decr(org_key)
                org_key = None
                await _reschedule(redis, conv, RETRY_LIMIT_SECONDS, now)
                continue
            started += 1
            async with session_factory() as session:
                mid = uuid.UUID(
                    message_id.decode() if isinstance(message_id, bytes) else message_id
                )
                result = await handler(session, mid)
            logger.info(
                "AI run finished",
                extra={"conversation_id": conv, "result": repr(result)[:200]},
            )
        except Exception as exc:
            logger.error(
                "AI run crashed",
                extra={"conversation_id": conv, "error": repr(exc)[:300]},
            )
        finally:
            if org_key:
                await redis.decr(org_key)
            await redis.delete(lock)
    return started


async def run_worker(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    handler: Handler,
    stop: asyncio.Event,
    *,
    poll_seconds: float = 0.5,
) -> None:
    while not stop.is_set():
        try:
            await process_due(redis, session_factory, handler)
        except Exception as exc:  # Redis hiccup: keep the worker alive
            logger.error("AI queue poll failed", extra={"error": repr(exc)[:300]})
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass
