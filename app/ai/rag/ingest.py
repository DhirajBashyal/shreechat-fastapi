"""Knowledge document ingestion (docs/ai/RAG_ARCHITECTURE.md §2).

    pending → processing → ready | failed

fetch / read source → extract text → normalise + sha256 → (unchanged? done)
→ chunk → embed (local service, batches of 64) → delete old chunks and insert
new ones in ONE transaction, so search never sees a half-updated document.

Runs inline in the request for now (there is no job queue yet); the function is
the same one a worker task will call.
"""

import hashlib
import uuid
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rag.chunking import Chunk, chunk_text, faq_chunks, html_to_text, normalize
from app.ai.rag.fetch import FetchError, fetch_url
from app.core.constants import EMBEDDING_MODEL_NAME
from app.core.logger import get_logger
from app.core.security import utcnow
from app.models import KbChunk, KbDocument, KnowledgeBase
from app.models.enums import KbDocumentStatus
from app.services import embeddings

logger = get_logger()

MAX_SOURCE_CHARS = 500_000


class IngestError(Exception):
    pass


async def _source_text(doc: KbDocument) -> tuple[str, list[Chunk] | None]:
    """(text used for the content hash, pre-built chunks for FAQs)."""
    meta: dict[str, Any] = doc.document_metadata or {}
    if doc.source_type == "faq":
        pairs = [(p["question"], p["answer"]) for p in meta.get("faq", [])]
        text = "\n".join(f"{q}\n{a}" for q, a in pairs)
        return text, faq_chunks(pairs)
    if doc.source_type == "text":
        return normalize(meta.get("text", "")), None
    if doc.source_type == "url":
        assert doc.source_url is not None
        try:
            page = await fetch_url(doc.source_url)
        except FetchError as exc:
            raise IngestError(f"Could not fetch URL: {exc}") from exc
        if page.content_type == "text/html":
            title, text = html_to_text(page.text)
            if title and doc.title == doc.source_url:
                doc.title = title[:512]
        else:
            text = normalize(page.text)
        return text, None
    raise IngestError(f"Source type '{doc.source_type}' is not supported yet")


async def ingest_document(
    session: AsyncSession, kb: KnowledgeBase, doc: KbDocument, *, force: bool = False
) -> KbDocument:
    """Ingest one document; always commits and returns it with its final status."""
    doc.status = KbDocumentStatus.PROCESSING
    doc.error = None
    await session.commit()
    try:
        text, prebuilt = await _source_text(doc)
        if not text.strip():
            raise IngestError("The document has no text")
        if len(text) > MAX_SOURCE_CHARS:
            raise IngestError("The document is longer than 500,000 characters")
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        if content_hash == doc.content_hash and not force:
            doc.status = KbDocumentStatus.READY
            doc.last_synced_at = utcnow()
            await session.commit()
            return doc

        chunks = prebuilt or chunk_text(
            text,
            max_tokens=kb.chunk_size_tokens,
            overlap_tokens=kb.chunk_overlap_tokens,
        )
        try:
            vectors = await embeddings.embed_texts([c.content for c in chunks])
        except embeddings.EmbeddingError as exc:
            raise IngestError(f"Embedding service failed: {exc}") from exc

        # Atomic replace: old chunks out, new chunks in, same transaction.
        await session.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
        session.add_all(
            KbChunk(
                id=uuid.uuid4(),
                organization_id=doc.organization_id,
                workspace_id=doc.workspace_id,
                knowledge_base_id=doc.knowledge_base_id,
                document_id=doc.id,
                chunk_index=i,
                content=c.content,
                token_count=c.token_count,
                embedding=v,
                embedding_model=EMBEDDING_MODEL_NAME,
                chunk_metadata={
                    "heading_path": c.heading_path,
                    "source_url": doc.source_url,
                    "title": doc.title,
                },
            )
            for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
        )
        doc.content_hash = content_hash
        doc.status = KbDocumentStatus.READY
        doc.last_synced_at = utcnow()
        await session.commit()
    except IngestError as exc:
        await session.rollback()
        doc.status = KbDocumentStatus.FAILED
        doc.error = str(exc)[:1000]
        await session.commit()
        logger.warning(
            "Knowledge document ingestion failed",
            extra={"document_id": str(doc.id), "error": str(exc)},
        )
    await session.refresh(doc)
    return doc
