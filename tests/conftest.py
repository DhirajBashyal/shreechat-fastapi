"""Shared test fixtures.

Tests run against the database in DB_URL (use ENV_FILE=.env.test) and need
Redis for rate limiting. The schema must be migrated (alembic upgrade head).
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import limiter
from app.main import app

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
