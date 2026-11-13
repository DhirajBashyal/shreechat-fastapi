"""Inbound media download (WHATSAPP.md §4 Media download, MESSAGE_PROCESSING.md §9).

The message shows immediately with a placeholder; this fetches the bytes
(WhatsApp: Graph media id + bearer token; Messenger/Instagram: CDN URLs that
expire, so promptly), checks size + SHA-256, sniffs the type, stores the file
and links ``attachments.file_id``. 5 tries with backoff, then
``meta.download_error`` ("Media unavailable" in the UI).
"""

import uuid

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels import queue
from app.channels.base import ChannelError
from app.channels.registry import load_adapter
from app.channels.types import InboundAttachment
from app.core import storage
from app.core.config import settings
from app.core.logger import get_logger
from app.core.tenancy import bind_tenant
from app.models import Attachment, ChannelConnection, EventOutbox, Message

logger = get_logger()

BACKOFF = (15, 60, 300, 900)
MAX_ATTEMPTS = 5


async def download(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    http: httpx.AsyncClient,
    attachment_id: uuid.UUID,
) -> str:
    async with session_factory() as s:
        att = await s.scalar(
            select(Attachment).where(Attachment.id == attachment_id).with_for_update()
        )
        if att is None or att.file_id is not None:
            return "skipped"
        await bind_tenant(s, att.organization_id)
        msg = await s.get(Message, att.message_id)
        assert msg is not None
        connection = await s.scalar(
            select(ChannelConnection).where(ChannelConnection.inbox_id == msg.inbox_id)
        )
        if connection is None:
            return "skipped"
        meta = dict(att.meta or {})
        attempts = int(meta.get("download_attempts", 0)) + 1
        try:
            adapter = await load_adapter(s, connection, http)
            data, mime = await adapter.download_media(
                InboundAttachment(
                    kind=att.kind.value,  # type: ignore[arg-type]
                    provider_media_id=meta.get("provider_media_id"),
                    url=None if meta.get("provider_media_id") else att.external_url,
                    mime_type=meta.get("mime_type"),
                    sha256=meta.get("sha256"),
                ),
                settings.MAX_UPLOAD_MB * 1024 * 1024,
            )
            stored = await storage.save(
                s,
                att.organization_id,
                data,
                filename=meta.get("filename"),
                content_type=mime or meta.get("mime_type"),
                expected_sha256=None,  # Meta's sha256 is of the encrypted blob for some types
                metadata={
                    "source": connection.provider.value,
                    "attachment_id": str(att.id),
                },
            )
            await s.flush()
            att.file_id = stored.id
            meta.pop("download_error", None)
            meta["download_attempts"] = attempts
            att.meta = meta
        except (ChannelError, storage.StorageError) as exc:
            retryable = isinstance(exc, ChannelError) and exc.retryable
            meta["download_attempts"] = attempts
            if retryable and attempts < MAX_ATTEMPTS:
                att.meta = meta
                await s.commit()
                await queue.enqueue(
                    redis, queue.MEDIA, str(att.id), delay=BACKOFF[attempts - 1]
                )
                return "retry"
            meta["download_error"] = getattr(exc, "code", "storage_error")
            att.meta = meta
            await s.commit()
            logger.warning(
                "Media download failed",
                extra={"attachment_id": str(att.id), "error": repr(exc)[:200]},
            )
            return "failed"
        s.add(
            EventOutbox(
                organization_id=msg.organization_id,
                workspace_id=msg.workspace_id,
                event_name="message.updated",
                aggregate_type="conversation",
                aggregate_id=msg.conversation_id,
                payload={
                    "conversation_id": str(msg.conversation_id),
                    "message_id": str(msg.id),
                    "changes": ["attachments"],
                },
            )
        )
        await s.commit()
        return "stored"
