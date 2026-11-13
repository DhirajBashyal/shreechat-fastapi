"""File storage (docs/backend/STORAGE_ARCHITECTURE.md). Local backend only today.

Files are written under ``STORAGE_LOCAL_PATH/<org>/<yyyy>/<mm>/<uuid>`` and
recorded in ``stored_files`` with their SHA-256. An S3 backend plugs in
behind ``save``/``read`` later (same ``storage_key`` scheme).
"""

import hashlib
import mimetypes
import uuid
from pathlib import Path

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import utcnow
from app.models import StoredFile

# Never trust a provider/visitor content type blindly: sniff the common ones.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
)


class StorageError(Exception):
    pass


def sniff(data: bytes, declared: str | None) -> str:
    head = data[:16]
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[4:8] == b"ftyp":
        return (
            declared
            if declared and declared.startswith(("video/", "audio/"))
            else "video/mp4"
        )
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    declared = (declared or "").split(";")[0].strip().lower()
    # A declared image/pdf that doesn't look like one is downgraded.
    if declared.startswith("image/") or declared == "application/pdf":
        return "application/octet-stream"
    return declared or "application/octet-stream"


def _root() -> Path:
    return Path(settings.STORAGE_LOCAL_PATH).resolve()


async def save(
    session: AsyncSession,
    organization_id: uuid.UUID,
    data: bytes,
    *,
    filename: str | None,
    content_type: str | None,
    expected_sha256: str | None = None,
    metadata: dict[str, object] | None = None,
) -> StoredFile:
    if settings.STORAGE_BACKEND != "local":
        raise StorageError(
            f"storage backend {settings.STORAGE_BACKEND!r} isn't available"
        )
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and expected_sha256.lower() != digest:
        raise StorageError("checksum mismatch")
    mime = sniff(data, content_type)
    file_id = uuid.uuid4()
    now = utcnow()
    key = f"{organization_id}/{now:%Y}/{now:%m}/{file_id}"
    path = _root() / key
    await anyio.to_thread.run_sync(
        lambda: path.parent.mkdir(parents=True, exist_ok=True)
    )
    await anyio.Path(path).write_bytes(data)
    name = filename or f"file{mimetypes.guess_extension(mime) or ''}"
    row = StoredFile(
        id=file_id,
        organization_id=organization_id,
        storage_backend="local",
        storage_key=key,
        filename=name[:512],
        content_type=mime,
        byte_size=len(data),
        checksum_sha256=digest,
        file_metadata=metadata or {},
        scan_status="skipped",
    )
    session.add(row)
    return row


async def read(stored: StoredFile) -> bytes:
    path = (_root() / stored.storage_key).resolve()
    if _root() not in path.parents:
        raise StorageError("bad storage key")
    return await anyio.Path(path).read_bytes()
