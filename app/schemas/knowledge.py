"""Knowledge bases, documents and debug search (docs/ai/RAG_ARCHITECTURE.md)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from app.models.enums import KbDocumentStatus


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str | None
    embedding_model: str
    embedding_dimensions: int
    chunk_size_tokens: int
    chunk_overlap_tokens: int
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime


class FaqPair(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=10_000)


class DocumentCreate(BaseModel):
    """One of: text (pasted), faq (question/answer pairs), url (single page)."""

    source_type: Literal["text", "faq", "url"]
    title: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=500_000)
    faq: list[FaqPair] | None = Field(default=None, max_length=500)
    url: AnyHttpUrl | None = None
    # "policy" documents are searched by policy_search (refunds, shipping...).
    category: Literal["general", "policy"] = "general"

    @model_validator(mode="after")
    def _source_present(self) -> "DocumentCreate":
        need = {"text": self.text, "faq": self.faq, "url": self.url}[self.source_type]
        if not need:
            raise ValueError(
                f"'{self.source_type}' documents need the '{self.source_type}' field"
            )
        if self.source_type != "url" and not self.title:
            raise ValueError("title is required for text and faq documents")
        return self


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source_type: str
    source_url: str | None
    status: KbDocumentStatus
    error: str | None
    category: str = "general"
    chunk_count: int = 0
    last_synced_at: datetime | None
    created_at: datetime


class ChunkRead(BaseModel):
    id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    metadata: dict[str, Any]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    # Default: every knowledge base of the workspace.
    knowledge_base_ids: list[uuid.UUID] | None = None
    policy_only: bool = False
    top_n: int = Field(default=8, ge=1, le=20)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    content: str
    heading_path: list[str]
    source_url: str | None
    similarity: float | None
    keyword_rank: float | None
    score: float


class SearchResponse(BaseModel):
    context_sufficient: bool
    hits: list[SearchHit]
    # What the AI agent would receive (docs/ai/RAG_ARCHITECTURE.md §4).
    context_block: str
