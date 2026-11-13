"""/api/v1/workspaces/{workspace_id}/knowledge-bases — RAG knowledge (docs/ai/RAG_ARCHITECTURE.md).

Listing knowledge bases: any member of the workspace. Everything else:
``ai.knowledge.manage`` (docs/ai/AI_ROLES_AND_PERMISSIONS.md §2, §6).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import DbSession, WorkspaceContext, require_permission
from app.core.tenancy import TenantContext
from app.schemas.knowledge import (
    ChunkRead,
    DocumentCreate,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    SearchRequest,
    SearchResponse,
)
from app.services import knowledge as service

router = APIRouter()

ManageKnowledge = Annotated[
    TenantContext, Depends(require_permission("ai.knowledge.manage", scope="workspace"))
]
KB = "/{workspace_id}/knowledge-bases"


@router.get(KB, summary="Knowledge bases of the workspace")
async def list_kbs(
    ctx: WorkspaceContext, session: DbSession
) -> list[KnowledgeBaseRead]:
    return await service.list_kbs(session, ctx)


@router.post(KB, status_code=status.HTTP_201_CREATED)
async def create_kb(
    body: KnowledgeBaseCreate, ctx: ManageKnowledge, session: DbSession
) -> KnowledgeBaseRead:
    return await service.create_kb(session, ctx, body)


@router.get(KB + "/{kb_id}")
async def get_kb(
    kb_id: uuid.UUID, ctx: WorkspaceContext, session: DbSession
) -> KnowledgeBaseRead:
    return await service.get_kb(session, ctx, kb_id)


@router.patch(KB + "/{kb_id}")
async def update_kb(
    kb_id: uuid.UUID,
    body: KnowledgeBaseUpdate,
    ctx: ManageKnowledge,
    session: DbSession,
) -> KnowledgeBaseRead:
    return await service.update_kb(session, ctx, kb_id, body)


@router.delete(KB + "/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(
    kb_id: uuid.UUID, ctx: ManageKnowledge, session: DbSession
) -> Response:
    await service.delete_kb(session, ctx, kb_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(KB + "/{kb_id}/documents")
async def list_documents(
    kb_id: uuid.UUID, ctx: ManageKnowledge, session: DbSession
) -> list[DocumentRead]:
    return await service.list_documents(session, ctx, kb_id)


@router.post(
    KB + "/{kb_id}/documents",
    status_code=status.HTTP_201_CREATED,
    summary="Add text, FAQ pairs or a URL; chunked and embedded right away",
)
async def add_document(
    kb_id: uuid.UUID, body: DocumentCreate, ctx: ManageKnowledge, session: DbSession
) -> DocumentRead:
    return await service.add_document(session, ctx, kb_id, body)


@router.post(
    KB + "/{kb_id}/documents/{doc_id}/resync", summary="Fetch / re-embed again"
)
async def resync_document(
    kb_id: uuid.UUID, doc_id: uuid.UUID, ctx: ManageKnowledge, session: DbSession
) -> DocumentRead:
    return await service.resync_document(session, ctx, kb_id, doc_id)


@router.delete(
    KB + "/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    kb_id: uuid.UUID, doc_id: uuid.UUID, ctx: ManageKnowledge, session: DbSession
) -> Response:
    await service.delete_document(session, ctx, kb_id, doc_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    KB + "/{kb_id}/documents/{doc_id}/chunks", summary="Chunks of a document (debug)"
)
async def list_chunks(
    kb_id: uuid.UUID, doc_id: uuid.UUID, ctx: ManageKnowledge, session: DbSession
) -> list[ChunkRead]:
    return await service.list_chunks(session, ctx, kb_id, doc_id)


@router.post(
    "/{workspace_id}/knowledge-search",
    summary="Debug search: what the AI would retrieve for a question",
)
async def search(
    body: SearchRequest, ctx: ManageKnowledge, session: DbSession
) -> SearchResponse:
    return await service.search(session, ctx, body)
