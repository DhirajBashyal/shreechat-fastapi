"""Run the AI agent for one incoming message.

    make ai-reply MESSAGE=<message uuid>            run now, in this process
    make ai-reply MESSAGE=<message uuid> QUEUE=1    enqueue for `make ai-worker`
                                                    (debounced like real traffic)

Until channels (Phase 4) exist, this is how to trigger a live run on a real
conversation. Prints the outcome or why the AI didn't run.
"""

import asyncio
import sys
import uuid

from app.ai.handlers import on_incoming_message
from app.core.db import sessionmanager


async def enqueue(message_id: uuid.UUID) -> None:
    from redis.asyncio import Redis

    from app.ai.queue import enqueue_incoming
    from app.core.config import settings
    from app.models import Message

    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    try:
        async with sessionmanager.session_factory() as session:
            message = await session.get(Message, message_id)
    finally:
        await sessionmanager.close()
    if message is None:
        sys.exit("message not found")
    redis = Redis.from_url(settings.REDIS_URL)
    try:
        await enqueue_incoming(redis, message.conversation_id, message.id)
    finally:
        await redis.aclose()
    sys.stdout.write(f"queued conversation {message.conversation_id}\n")


async def main(message_id: uuid.UUID) -> None:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    try:
        async with sessionmanager.session_factory() as session:
            result = await on_incoming_message(session, message_id)
    finally:
        await sessionmanager.close()
    if isinstance(result, str):
        sys.stdout.write(f"AI did not run: {result}\n")
    else:
        sys.stdout.write(
            f"outcome={result.outcome} run_id={result.run_id} handoff_reason={result.handoff_reason} "
            f"duplicate={result.duplicate}\n"
        )


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: python -m app.jobs.ai_reply <message uuid> [--queue]")
    target = uuid.UUID(sys.argv[1])
    asyncio.run(enqueue(target) if sys.argv[2:] == ["--queue"] else main(target))
