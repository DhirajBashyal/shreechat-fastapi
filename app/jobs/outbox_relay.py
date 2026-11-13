"""Outbox relay: publishes committed events to realtime channels and delivers
replies to website-widget visitors.

    make outbox-relay

Stops cleanly on SIGINT/SIGTERM after the current batch.
"""

import asyncio
import signal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.db import sessionmanager
from app.core.logger import get_logger
from app.services.outbox_relay import run_relay

logger = get_logger()


async def main() -> None:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    redis = Redis.from_url(settings.REDIS_URL)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("Outbox relay started")
    try:
        await run_relay(sessionmanager.session_factory, redis, stop)
    finally:
        await redis.aclose()
        await sessionmanager.close()
        logger.info("Outbox relay stopped")


if __name__ == "__main__":
    asyncio.run(main())
