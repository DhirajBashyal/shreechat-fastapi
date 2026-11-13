"""Shared test fixtures.

Tests run against the database in DB_URL (use ENV_FILE=.env.test) and need
Redis for rate limiting. The schema must be migrated (alembic upgrade head).
"""

import asyncio
import hashlib
import math
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.constants import EMBEDDING_DIMENSIONS
from app.core.rate_limit import limiter
from app.main import app
from app.services import embeddings

API = "/api/v1"


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    # One client for the whole session: the app (and its async DB pool) runs
    # on a single event loop.
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    limiter.reset()


@pytest.fixture
def sent_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Capture outgoing emails instead of sending/logging them."""
    outbox: list[dict[str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> None:
        outbox.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr("app.services.auth.send_email", fake_send)
    monkeypatch.setattr("app.services.tenancy.send_email", fake_send)
    return outbox


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


PASSWORD = "Sup3r-secret-pw"


def signup(
    client: TestClient,
    *,
    email: str | None = None,
    org: str | None = "Acme",
    invitation_token: str | None = None,
    remember_me: bool = False,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Test User",
        "email": email or unique_email(),
        "password": PASSWORD,
        "remember_me": remember_me,
    }
    if invitation_token:
        body["invitation_token"] = invitation_token
    else:
        body["organization_name"] = org
    r = client.post(f"{API}/auth/signup", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    data["_email"] = body["email"]
    return data


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def run_sql(statement: str, **params: Any) -> list[Any]:
    """Run SQL on a separate connection, for state the API can't create
    (suspension, plan overrides, platform admins, conversations)."""

    async def run() -> list[Any]:
        engine = create_async_engine(settings.DB_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


# --- Fake embedding service (deterministic hashed bag of words, 384 dims) ---


def _fake_vector(text: str) -> list[float]:
    v = [0.0] * EMBEDDING_DIMENSIONS
    for word in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % EMBEDDING_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_texts", fake)
    return calls
