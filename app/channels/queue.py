"""Channel work queues on Redis sorted sets (score = when the job is due).

    ch:inbound    inbound_webhook_events.id   → pipeline/inbound.process_event
    ch:outbound   messages.id                 → pipeline/outbound.deliver
    ch:media      attachments.id              → pipeline/media.download

A delayed retry is just a later score. ZADD of an existing member keeps one
entry, so enqueueing twice is harmless; ZREM decides which worker claims it.
Postgres stays the source of truth: the sweeper re-enqueues rows a lost
Redis entry would have stranded.
"""

import time

from redis.asyncio import Redis

INBOUND = "ch:inbound"
OUTBOUND = "ch:outbound"
MEDIA = "ch:media"


async def enqueue(
    redis: Redis,
    queue: str,
    member: str,
    *,
    delay: float = 0.0,
    now: float | None = None,
) -> None:
    await redis.zadd(queue, {member: (now or time.time()) + delay})


async def enqueue_many(redis: Redis, queue: str, members: list[str]) -> None:
    if members:
        now = time.time()
        await redis.zadd(queue, dict.fromkeys(members, now))


async def claim_due(
    redis: Redis, queue: str, *, batch: int = 50, now: float | None = None
) -> list[str]:
    claimed: list[str] = []
    for raw in await redis.zrangebyscore(
        queue, "-inf", now or time.time(), start=0, num=batch
    ):
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        if await redis.zrem(queue, member):
            claimed.append(member)
    return claimed


async def acquire_lock(redis: Redis, key: str, ttl: int) -> bool:
    return bool(await redis.set(key, "1", nx=True, ex=ttl))


async def release_lock(redis: Redis, key: str) -> None:
    await redis.delete(key)
