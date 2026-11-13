"""Hybrid retrieval over kb_chunks (docs/ai/RAG_ARCHITECTURE.md §3).

Vector similarity (pgvector HNSW, cosine) + keyword search (PostgreSQL FTS),
fused with Reciprocal Rank Fusion. Always filtered by workspace and by the
knowledge-base ids the caller is allowed to search (never from user input
directly), and by not-deleted documents.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbChunk, KbDocument
from app.services import embeddings

CANDIDATES = 20
RRF_K = 60
# Thresholds are configuration, tuned for paraphrase-multilingual-MiniLM-L12-v2.
MIN_SIMILARITY = 0.30
MIN_POLICY_SIMILARITY = 0.40
MIN_KEYWORD_RANK = 0.1


@dataclass
class Hit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    content: str
    metadata: dict[str, Any]
    similarity: float | None = None
    keyword_rank: float | None = None
    score: float = 0.0


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    context_sufficient: bool = False


def rrf(vector_hits: list[Hit], keyword_hits: list[Hit], top_n: int) -> list[Hit]:
    by_id: dict[uuid.UUID, Hit] = {}
    for ranked in (vector_hits, keyword_hits):
        for rank, hit in enumerate(ranked):
            merged = by_id.setdefault(hit.chunk_id, hit)
            merged.similarity = (
                merged.similarity if hit.similarity is None else hit.similarity
            )
            merged.keyword_rank = (
                merged.keyword_rank if hit.keyword_rank is None else hit.keyword_rank
            )
            merged.score += 1 / (RRF_K + rank + 1)
    return sorted(by_id.values(), key=lambda h: h.score, reverse=True)[:top_n]


async def hybrid_search(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    knowledge_base_ids: list[uuid.UUID],
    query: str,
    top_n: int = 8,
    policy_only: bool = False,
) -> SearchResult:
    if not knowledge_base_ids or not query.strip():
        return SearchResult()
    [query_vector] = await embeddings.embed_texts([query])
    min_similarity = MIN_POLICY_SIMILARITY if policy_only else MIN_SIMILARITY

    filters = [
        KbChunk.workspace_id == workspace_id,
        KbChunk.knowledge_base_id.in_(knowledge_base_ids),
        KbDocument.deleted_at.is_(None),
    ]
    if policy_only:
        filters.append(KbDocument.document_metadata["category"].astext == "policy")

    # Keep scanning the HNSW graph when filters remove candidates (pgvector ≥ 0.8).
    for name, value in (
        ("hnsw.ef_search", "100"),
        ("hnsw.iterative_scan", "relaxed_order"),
    ):
        await session.execute(select(func.set_config(name, value, True)))

    distance = KbChunk.embedding.cosine_distance(query_vector)
    vector_rows = (
        await session.execute(
            select(KbChunk, KbDocument.title, (1 - distance).label("similarity"))
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(*filters)
            .order_by(distance)
            .limit(CANDIDATES)
        )
    ).all()

    tsquery = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank_cd(KbChunk.search_vector, tsquery)
    keyword_rows = (
        await session.execute(
            select(KbChunk, KbDocument.title, rank.label("rank"))
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(*filters, KbChunk.search_vector.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(CANDIDATES)
        )
    ).all()

    def hit(chunk: KbChunk, title: str) -> Hit:
        return Hit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            knowledge_base_id=chunk.knowledge_base_id,
            title=title,
            content=chunk.content,
            metadata=chunk.chunk_metadata or {},
        )

    vector_hits = []
    for chunk, title, similarity in vector_rows:
        if float(similarity) >= min_similarity:
            h = hit(chunk, title)
            h.similarity = float(similarity)
            vector_hits.append(h)
    keyword_hits = []
    for chunk, title, kw_rank in keyword_rows:
        h = hit(chunk, title)
        h.keyword_rank = float(kw_rank)
        keyword_hits.append(h)

    hits = rrf(vector_hits, keyword_hits, top_n)
    sufficient = bool(vector_hits) or any(
        (h.keyword_rank or 0) >= MIN_KEYWORD_RANK for h in keyword_hits
    )
    return SearchResult(hits=hits, context_sufficient=sufficient)


def render_context(
    hits: list[Hit], max_tokens: int = 1500
) -> tuple[str, list[dict[str, Any]]]:
    """<context> block with numbered sources + the citation list (§4)."""
    from app.ai.rag.chunking import estimate_tokens

    lines, citations, used = ["<context>"], [], 0
    for n, h in enumerate(hits, 1):
        cost = estimate_tokens(h.content)
        if used + cost > max_tokens:
            break
        heading = " › ".join(h.metadata.get("heading_path") or [])
        label = f"{h.title} › {heading}" if heading else h.title
        url = h.metadata.get("source_url")
        lines.append(f"[{n}] {label}" + (f" ({url})" if url else ""))
        lines.append(h.content)
        lines.append("")
        citations.append(
            {
                "n": n,
                "chunk_id": str(h.chunk_id),
                "document_id": str(h.document_id),
                "title": h.title,
                "url": url,
            }
        )
        used += cost
    lines.append("</context>")
    return "\n".join(lines), citations
