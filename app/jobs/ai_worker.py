"""AI queue worker: runs the agent for conversations whose debounce expired.

    make ai-worker

Stops cleanly on SIGINT/SIGTERM after the current batch.
"""

import asyncio
import signal

from redis.asyncio import Redis

from app.ai.handlers import on_incoming_message
from app.ai.queue import run_worker
from app.core.config import settings
from app.core.db import sessionmanager
from app.core.logger import get_logger

logger = get_logger()


async def main() -> None:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    redis = Redis.from_url(settings.REDIS_URL)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info(
        "AI worker started",
        extra={"max_runs_per_org": settings.AI_MAX_CONCURRENT_RUNS_PER_ORG},
    )
    try:
        await run_worker(
            redis, sessionmanager.session_factory, on_incoming_message, stop
        )
    finally:
        await redis.aclose()
        await sessionmanager.close()
        logger.info("AI worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
