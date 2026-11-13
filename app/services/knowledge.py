"""Knowledge bases and documents for a workspace."""

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rag import retriever
from app.ai.rag.fetch import FetchError, check_url
from app.ai.rag.ingest import ingest_document
from app.core.constants import KB_CHUNK_OVERLAP_TOKENS, KB_CHUNK_TOKENS
from app.core.exceptions import (
    ConflictError,
    EmbeddingUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.security import utcnow
from app.core.tenancy import TenantContext
from app.models import KbChunk, KbDocument, KnowledgeBase
from app.schemas.knowledge import (
    ChunkRead,
    DocumentCreate,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.services import embeddings


async def _kb(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID
) -> KnowledgeBase:
    kb = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.workspace_id == ctx.require_workspace(),
            KnowledgeBase.deleted_at.is_(None),
        )
    )
    if kb is None:
        raise NotFoundError()
    return kb


async def _doc(
    session: AsyncSession, kb: KnowledgeBase, doc_id: uuid.UUID
) -> KbDocument:
    doc = await session.scalar(
        select(KbDocument).where(
            KbDocument.id == doc_id,
            KbDocument.knowledge_base_id == kb.id,
            KbDocument.deleted_at.is_(None),
        )
    )
    if doc is None:
        raise NotFoundError()
    return doc


async def _kb_read(session: AsyncSession, kb: KnowledgeBase) -> KnowledgeBaseRead:
    docs = await session.scalar(
        select(func.count()).where(
            KbDocument.knowledge_base_id == kb.id, KbDocument.deleted_at.is_(None)
        )
    )
    chunks = await session.scalar(
        select(func.count()).where(KbChunk.knowledge_base_id == kb.id)
    )
    read = KnowledgeBaseRead.model_validate(kb)
    read.document_count, read.chunk_count = docs or 0, chunks or 0
    return read


async def _doc_read(session: AsyncSession, doc: KbDocument) -> DocumentRead:
    read = DocumentRead.model_validate(doc)
    read.category = (doc.document_metadata or {}).get("category", "general")
    read.chunk_count = (
        await session.scalar(select(func.count()).where(KbChunk.document_id == doc.id))
        or 0
    )
    return read


# --- Knowledge bases -------------------------------------------------------------


async def list_kbs(
    session: AsyncSession, ctx: TenantContext
) -> list[KnowledgeBaseRead]:
    kbs = await session.scalars(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.workspace_id == ctx.require_workspace(),
            KnowledgeBase.deleted_at.is_(None),
        )
        .order_by(KnowledgeBase.created_at)
    )
    return [await _kb_read(session, kb) for kb in kbs]


async def get_kb(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID
) -> KnowledgeBaseRead:
    return await _kb_read(session, await _kb(session, ctx, kb_id))


async def create_kb(
    session: AsyncSession, ctx: TenantContext, data: KnowledgeBaseCreate
) -> KnowledgeBaseRead:
    kb = KnowledgeBase(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=ctx.require_workspace(),
        name=data.name,
        description=data.description,
        # Sized for the local embedding model's 128-token window.
        chunk_size_tokens=KB_CHUNK_TOKENS,
        chunk_overlap_tokens=KB_CHUNK_OVERLAP_TOKENS,
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return await _kb_read(session, kb)


async def update_kb(
    session: AsyncSession,
    ctx: TenantContext,
    kb_id: uuid.UUID,
    data: KnowledgeBaseUpdate,
) -> KnowledgeBaseRead:
    kb = await _kb(session, ctx, kb_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(kb, key, value)
    await session.commit()
    await session.refresh(kb)
    return await _kb_read(session, kb)


async def delete_kb(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID
) -> None:
    kb = await _kb(session, ctx, kb_id)
    now = utcnow()
    await session.execute(delete(KbChunk).where(KbChunk.knowledge_base_id == kb.id))
    await session.execute(
        update(KbDocument)
        .where(KbDocument.knowledge_base_id == kb.id, KbDocument.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    kb.deleted_at = now
    await session.commit()


# --- Documents -------------------------------------------------------------------


async def list_documents(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID
) -> list[DocumentRead]:
    kb = await _kb(session, ctx, kb_id)
    docs = await session.scalars(
        select(KbDocument)
        .where(KbDocument.knowledge_base_id == kb.id, KbDocument.deleted_at.is_(None))
        .order_by(KbDocument.created_at)
    )
    return [await _doc_read(session, d) for d in docs]


async def add_document(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, data: DocumentCreate
) -> DocumentRead:
    kb = await _kb(session, ctx, kb_id)
    url = str(data.url) if data.url else None
    if url:
        try:
            await check_url(url)
        except FetchError as exc:
            raise ValidationFailedError(str(exc), code="url_not_allowed") from exc
    metadata: dict[str, object] = {"category": data.category}
    if data.source_type == "text":
        metadata["text"] = data.text
    elif data.source_type == "faq":
        metadata["faq"] = [p.model_dump() for p in data.faq or []]
    doc = KbDocument(
        id=uuid.uuid4(),
        organization_id=ctx.organization_id,
        workspace_id=kb.workspace_id,
        knowledge_base_id=kb.id,
        title=data.title or url or "Untitled",
        source_type=data.source_type,
        source_url=url,
        document_metadata=metadata,
        created_by=ctx.principal.user_id,
    )
    session.add(doc)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("This URL is already in the knowledge base") from None
    doc = await ingest_document(session, kb, doc)
    return await _doc_read(session, doc)


async def resync_document(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, doc_id: uuid.UUID
) -> DocumentRead:
    kb = await _kb(session, ctx, kb_id)
    doc = await ingest_document(
        session, kb, await _doc(session, kb, doc_id), force=True
    )
    return await _doc_read(session, doc)


async def delete_document(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, doc_id: uuid.UUID
) -> None:
    kb = await _kb(session, ctx, kb_id)
    doc = await _doc(session, kb, doc_id)
    await session.execute(delete(KbChunk).where(KbChunk.document_id == doc.id))
    doc.deleted_at = utcnow()
    await session.commit()


async def list_chunks(
    session: AsyncSession, ctx: TenantContext, kb_id: uuid.UUID, doc_id: uuid.UUID
) -> list[ChunkRead]:
    doc = await _doc(session, await _kb(session, ctx, kb_id), doc_id)
    chunks = await session.scalars(
        select(KbChunk)
        .where(KbChunk.document_id == doc.id)
        .order_by(KbChunk.chunk_index)
    )
    return [
        ChunkRead(
            id=c.id,
            chunk_index=c.chunk_index,
            content=c.content,
            token_count=c.token_count,
            metadata=c.chunk_metadata or {},
        )
        for c in chunks
    ]


# --- Search ----------------------------------------------------------------------


async def search(
    session: AsyncSession, ctx: TenantContext, data: SearchRequest
) -> SearchResponse:
    workspace_id = ctx.require_workspace()
    valid = list(
        await session.scalars(
            select(KnowledgeBase.id).where(
                KnowledgeBase.workspace_id == workspace_id,
                KnowledgeBase.deleted_at.is_(None),
                *(
                    [KnowledgeBase.id.in_(data.knowledge_base_ids)]
                    if data.knowledge_base_ids is not None
                    else []
                ),
            )
        )
    )
    if data.knowledge_base_ids is not None and set(valid) != set(
        data.knowledge_base_ids
    ):
        raise NotFoundError("Knowledge base not found")
    try:
        result = await retriever.hybrid_search(
            session,
            workspace_id=workspace_id,
            knowledge_base_ids=valid,
            query=data.query,
            top_n=data.top_n,
            policy_only=data.policy_only,
        )
    except embeddings.EmbeddingError as exc:
        raise EmbeddingUnavailableError() from exc
    context_block, _ = retriever.render_context(result.hits)
    return SearchResponse(
        context_sufficient=result.context_sufficient,
        context_block=context_block,
        hits=[
            SearchHit(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                knowledge_base_id=h.knowledge_base_id,
                title=h.title,
                content=h.content,
                heading_path=h.metadata.get("heading_path") or [],
                source_url=h.metadata.get("source_url"),
                similarity=h.similarity,
                keyword_rank=h.keyword_rank,
                score=h.score,
            )
            for h in result.hits
        ],
    )
