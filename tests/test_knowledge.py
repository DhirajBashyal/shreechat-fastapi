"""Knowledge bases and RAG retrieval (docs/ai/RAG_ARCHITECTURE.md).

The embedding service is replaced by a deterministic fake (hashed bag of
words, 384 dims), so these tests don't need embedding_service running.
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.rag.chunking import chunk_text, estimate_tokens, html_to_text
from app.ai.rag.fetch import FetchError, check_url
from app.ai.rag.retriever import Hit, rrf
from app.core.constants import EMBEDDING_MAX_TOKENS
from app.services import embeddings
from tests.conftest import API, bearer, signup, unique_email


@pytest.fixture
def broken_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(texts: list[str]) -> list[list[float]]:
        raise embeddings.EmbeddingError("service down")

    monkeypatch.setattr(embeddings, "embed_texts", fail)


def _ws(tokens: dict[str, Any]) -> str:
    return str(tokens["memberships"][0]["workspaces"][0]["id"])


POLICY = (
    "# Refunds\n## Timelines\nRefunds are processed within 5-7 business days to the "
    "original payment method.\n## Damaged items\nIf an item arrived damaged send a "
    "photo within 14 days and we replace it.\n# Shipping\nWe ship to Canada and Mexico."
)


def _kb_with_policy(client: TestClient, owner: dict[str, Any]) -> tuple[str, str]:
    ws = _ws(owner)
    kb = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases",
        json={"name": "Help"},
        headers=bearer(owner),
    )
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]
    doc = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents",
        json={
            "source_type": "text",
            "title": "Refund policy",
            "text": POLICY,
            "category": "policy",
        },
        headers=bearer(owner),
    )
    assert doc.status_code == 201, doc.text
    assert doc.json()["status"] == "ready" and doc.json()["chunk_count"] == 3
    return ws, kb_id


# --- Unit: chunking, fusion, URL safety ------------------------------------------


def test_chunks_fit_the_embedding_model_window() -> None:
    text = "# Guide\n" + " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, max_tokens=100, overlap_tokens=15)
    assert len(chunks) > 10
    assert all(estimate_tokens(c.content) <= EMBEDDING_MAX_TOKENS for c in chunks)
    assert all(c.heading_path == ["Guide"] for c in chunks)
    # Overlap: each chunk starts with the end of the previous one.
    assert chunks[1].content.split("\n", 1)[1].split()[0] in chunks[0].content


def test_heading_paths_and_html_extraction() -> None:
    title, text = html_to_text(
        "<html><title>Help</title><nav>Menu</nav><h1>Refunds</h1><p>Within 5 days.</p>"
        "<script>evil()</script><h2>Damaged</h2><p>Send a photo.</p></html>"
    )
    assert title == "Help" and "Menu" not in text and "evil" not in text
    chunks = chunk_text(text, max_tokens=100, overlap_tokens=15)
    assert [c.heading_path for c in chunks] == [["Refunds"], ["Refunds", "Damaged"]]


def test_rrf_rewards_hits_found_by_both_searches() -> None:
    import uuid

    def h(i: int) -> Hit:
        return Hit(uuid.UUID(int=i), uuid.UUID(int=0), uuid.UUID(int=0), "t", "c", {})

    fused = rrf([h(1), h(2), h(3)], [h(3), h(4)], top_n=3)
    assert fused[0].chunk_id == uuid.UUID(int=3)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8001/docs",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "ftp://example.com/file",
        "http://example.com:8080/",
    ],
)
def test_internal_urls_are_refused(url: str) -> None:
    with pytest.raises(FetchError):
        asyncio.run(check_url(url))


# --- API ---------------------------------------------------------------------------


def test_ingest_and_search(
    client: TestClient, fake_embeddings: list[list[str]]
) -> None:
    owner = signup(client)
    ws, kb_id = _kb_with_policy(client, owner)

    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-search",
        json={"query": "refunds processed business days"},
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context_sufficient"]
    assert body["hits"][0]["heading_path"] == ["Refunds", "Timelines"]
    assert "[1] Refund policy › Refunds › Timelines" in body["context_block"]

    # Policy-only search sees policy documents; a general doc is excluded.
    client.post(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents",
        json={
            "source_type": "faq",
            "title": "FAQ",
            "faq": [{"question": "refunds?", "answer": "See policy"}],
        },
        headers=bearer(owner),
    )
    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-search",
        json={"query": "refunds", "policy_only": True},
        headers=bearer(owner),
    )
    assert {h["title"] for h in r.json()["hits"]} == {"Refund policy"}


def test_resync_skips_unchanged_and_delete_removes_chunks(
    client: TestClient, fake_embeddings: list[list[str]]
) -> None:
    owner = signup(client)
    ws, kb_id = _kb_with_policy(client, owner)
    docs = client.get(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents",
        headers=bearer(owner),
    ).json()
    doc_id = docs[0]["id"]
    chunks = client.get(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
        headers=bearer(owner),
    ).json()
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]

    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents/{doc_id}/resync",
        headers=bearer(owner),
    )
    assert r.json()["status"] == "ready" and r.json()["chunk_count"] == 3

    assert (
        client.delete(
            f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents/{doc_id}",
            headers=bearer(owner),
        ).status_code
        == 204
    )
    kb = client.get(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}", headers=bearer(owner)
    ).json()
    assert kb["document_count"] == 0 and kb["chunk_count"] == 0


def test_embedding_outage_marks_document_failed(
    client: TestClient, broken_embeddings: None
) -> None:
    owner = signup(client)
    ws = _ws(owner)
    kb_id = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases",
        json={"name": "Help"},
        headers=bearer(owner),
    ).json()["id"]
    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb_id}/documents",
        json={"source_type": "text", "title": "T", "text": "hello world"},
        headers=bearer(owner),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "failed" and "Embedding service" in r.json()["error"]
    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-search",
        json={"query": "hello"},
        headers=bearer(owner),
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "embedding_unavailable"


def test_agents_list_but_cannot_manage(
    client: TestClient,
    sent_emails: list[dict[str, str]],
    fake_embeddings: list[list[str]],
) -> None:
    import re as _re

    owner = signup(client)
    ws, kb_id = _kb_with_policy(client, owner)
    org = owner["memberships"][0]["organization_id"]
    email = unique_email("agent")
    client.post(
        f"{API}/organizations/{org}/invitations",
        json={"email": email, "role": "agent"},
        headers=bearer(owner),
    )
    token = _re.search(r"token=([\w\-]+)", sent_emails[-1]["body"])
    assert token is not None
    agent = signup(client, email=email, invitation_token=token.group(1))
    client.patch(
        f"{API}/organizations/{org}/members/{agent['memberships'][0]['membership_id']}",
        json={"workspace_ids": [ws]},
        headers=bearer(owner),
    )
    assert [
        k["id"]
        for k in client.get(
            f"{API}/workspaces/{ws}/knowledge-bases", headers=bearer(agent)
        ).json()
    ] == [kb_id]
    for method, path, body in (
        ("POST", f"/workspaces/{ws}/knowledge-bases", {"name": "x"}),
        ("GET", f"/workspaces/{ws}/knowledge-bases/{kb_id}/documents", None),
        ("POST", f"/workspaces/{ws}/knowledge-search", {"query": "refund"}),
    ):
        r = client.request(method, API + path, json=body, headers=bearer(agent))
        assert r.status_code == 403, (path, r.text)


def test_knowledge_is_isolated_between_tenants(
    client: TestClient, fake_embeddings: list[list[str]]
) -> None:
    a, b = signup(client), signup(client)
    ws_a, kb_a = _kb_with_policy(client, a)
    ws_b, kb_b = _kb_with_policy(client, b)
    hits = client.post(
        f"{API}/workspaces/{ws_a}/knowledge-search",
        json={"query": "refunds"},
        headers=bearer(a),
    ).json()["hits"]
    assert hits and {h["knowledge_base_id"] for h in hits} == {kb_a}
    # B's knowledge base id in A's workspace: not found, never searched.
    r = client.post(
        f"{API}/workspaces/{ws_a}/knowledge-search",
        json={"query": "refunds", "knowledge_base_ids": [kb_b]},
        headers=bearer(a),
    )
    assert r.status_code == 404
    assert (
        client.get(
            f"{API}/workspaces/{ws_b}/knowledge-bases", headers=bearer(a)
        ).status_code
        == 404
    )
