"""Platform admin API (docs/12 §7): our staff see every organization."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.conftest import API, bearer, run_sql, signup


def _make_platform_admin(tokens: dict[str, Any]) -> None:
    run_sql(
        "INSERT INTO platform_admins (user_id) VALUES (:u) ON CONFLICT DO NOTHING",
        u=tokens["user"]["id"],
    )


def _org(tokens: dict[str, Any]) -> str:
    return str(tokens["memberships"][0]["organization_id"])


def test_platform_api_is_hidden_from_everyone_else(client: TestClient) -> None:
    owner = signup(client)
    for path in ("/platform/stats", "/platform/organizations", "/platform/admins"):
        assert client.get(f"{API}{path}", headers=bearer(owner)).status_code == 404
    assert client.get(f"{API}/platform/organizations").status_code == 401
    me = client.get(f"{API}/auth/me", headers=bearer(owner)).json()
    assert me["is_platform_admin"] is False


def test_platform_admin_sees_all_organizations(client: TestClient) -> None:
    staff = signup(client, org="Staff Co")
    _make_platform_admin(staff)
    acme = signup(client, org="Platformtest Acme")
    globex = signup(client, org="Platformtest Globex")

    assert client.get(f"{API}/auth/me", headers=bearer(staff)).json()[
        "is_platform_admin"
    ]

    stats = client.get(f"{API}/platform/stats", headers=bearer(staff)).json()
    assert stats["total"] >= 3
    assert stats["total"] == (
        stats["active"] + stats["suspended"] + stats["pending_deletion"]
    )

    r = client.get(
        f"{API}/platform/organizations",
        params={"q": "platformtest", "limit": 1},
        headers=bearer(staff),
    )
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["matching"] >= 2 and len(page["items"]) == 1
    assert page["counts"] == stats

    # Search by owner email works too.
    r = client.get(
        f"{API}/platform/organizations",
        params={"q": globex["_email"]},
        headers=bearer(staff),
    )
    [item] = r.json()["items"]
    assert item["id"] == _org(globex)
    assert item["owner_email"] == globex["_email"]
    assert item["member_count"] == 1 and item["workspace_count"] == 1

    detail = client.get(
        f"{API}/platform/organizations/{_org(acme)}", headers=bearer(staff)
    ).json()
    assert [m["role"] for m in detail["members"]] == ["owner"]
    assert detail["workspaces"] == ["Support"]
    # Still not a member: the tenant API stays closed to the platform admin.
    assert (
        client.get(
            f"{API}/organizations/{_org(acme)}", headers=bearer(staff)
        ).status_code
        == 404
    )


def test_platform_admin_suspends_reactivates_and_overrides_limits(
    client: TestClient,
) -> None:
    staff = signup(client)
    _make_platform_admin(staff)
    owner = signup(client)
    org = _org(owner)
    url = f"{API}/platform/organizations/{org}"

    r = client.patch(url, json={"status": "suspended"}, headers=bearer(staff))
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    r = client.get(f"{API}/organizations/{org}", headers=bearer(owner))
    assert r.json()["error"]["code"] == "organization_suspended"
    [(kind, action)] = run_sql(
        "SELECT actor_kind, action FROM audit_logs WHERE organization_id = :o "
        "AND action LIKE 'platform.%'",
        o=org,
    )
    assert (kind, action) == ("platform_admin", "platform.organization_updated")

    r = client.patch(
        url,
        json={"status": "active", "limits_override": {"agents": 50}},
        headers=bearer(staff),
    )
    assert r.status_code == 200
    assert r.json()["limits"]["agents"] == 50
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(owner)).json()[
            "limits"
        ]["agents"]
        == 50
    )

    assert (
        client.patch(
            url, json={"status": "pending_deletion"}, headers=bearer(staff)
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url, json={"limits_override": {"agents": -1}}, headers=bearer(staff)
        ).status_code
        == 422
    )


def test_platform_admins_grant_and_revoke_each_other(client: TestClient) -> None:
    staff = signup(client)
    _make_platform_admin(staff)
    colleague = signup(client)

    r = client.post(
        f"{API}/platform/admins",
        json={"email": colleague["_email"]},
        headers=bearer(staff),
    )
    assert r.status_code == 201, r.text
    assert colleague["_email"] in {a["email"] for a in r.json()}
    assert (
        client.get(f"{API}/platform/stats", headers=bearer(colleague)).status_code
        == 200
    )

    r = client.post(
        f"{API}/platform/admins",
        json={"email": "nobody-here@example.com"},
        headers=bearer(staff),
    )
    assert r.status_code == 404

    me_id = staff["user"]["id"]
    assert (
        client.delete(
            f"{API}/platform/admins/{me_id}", headers=bearer(staff)
        ).status_code
        == 422
    )
    r = client.delete(
        f"{API}/platform/admins/{colleague['user']['id']}", headers=bearer(staff)
    )
    assert r.status_code == 204
    assert (
        client.get(f"{API}/platform/stats", headers=bearer(colleague)).status_code
        == 404
    )


def test_platform_api_needs_mfa_outside_local(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = signup(client)
    _make_platform_admin(staff)
    monkeypatch.setattr(settings, "ENV", "production")
    r = client.get(f"{API}/platform/stats", headers=bearer(staff))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "mfa_required"
