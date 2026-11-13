"""Organization lifecycle and member rules from
docs/12_ORGANIZATION_LEVELS_AND_PERMISSIONS.md §4, §5 and §10."""

import asyncio
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from tests.conftest import API, PASSWORD, bearer, signup, unique_email
from tests.conftest import run_sql as _sql


def _org(tokens: dict[str, Any]) -> str:
    return str(tokens["memberships"][0]["organization_id"])


def _join(
    client: TestClient,
    owner: dict[str, Any],
    sent_emails: list[dict[str, str]],
    role: str = "agent",
) -> dict[str, Any]:
    email = unique_email(role)
    r = client.post(
        f"{API}/organizations/{_org(owner)}/invitations",
        json={"email": email, "role": role},
        headers=bearer(owner),
    )
    assert r.status_code == 201, r.text
    match = re.search(r"token=([\w\-]+)", sent_emails[-1]["body"])
    assert match is not None
    return signup(client, email=email, invitation_token=match.group(1))


def _me(client: TestClient, tokens: dict[str, Any]) -> dict[str, Any]:
    r = client.get(f"{API}/auth/me", headers=bearer(tokens))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


# --- Transfer ownership ---------------------------------------------------------


def test_transfer_ownership_to_an_administrator(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    admin = _join(client, owner, sent_emails, role="administrator")
    agent = _join(client, owner, sent_emails)
    url = f"{API}/organizations/{org}/transfer-ownership"
    admin_id = admin["memberships"][0]["membership_id"]
    agent_id = agent["memberships"][0]["membership_id"]

    # Only the owner, only to an administrator, only with the right password.
    r = client.post(
        url,
        json={"membership_id": agent_id, "password": PASSWORD},
        headers=bearer(admin),
    )
    assert r.status_code == 403
    r = client.post(
        url,
        json={"membership_id": agent_id, "password": PASSWORD},
        headers=bearer(owner),
    )
    assert r.status_code == 422
    r = client.post(
        url,
        json={"membership_id": admin_id, "password": "wrong"},
        headers=bearer(owner),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "password_incorrect"

    r = client.post(
        url,
        json={"membership_id": admin_id, "password": PASSWORD},
        headers=bearer(owner),
    )
    assert r.status_code == 204, r.text
    assert _me(client, owner)["memberships"][0]["role"] == "administrator"
    assert _me(client, admin)["memberships"][0]["role"] == "owner"
    roles = [
        m["role"]
        for m in client.get(
            f"{API}/organizations/{org}/members", headers=bearer(admin)
        ).json()
    ]
    assert roles.count("owner") == 1


# --- Leave -----------------------------------------------------------------------


def test_members_can_leave_but_the_owner_cannot(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    agent = _join(client, owner, sent_emails)

    r = client.post(f"{API}/organizations/{org}/leave", headers=bearer(owner))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "owner_cannot_leave"

    r = client.post(f"{API}/organizations/{org}/leave", headers=bearer(agent))
    assert r.status_code == 204
    assert _me(client, agent)["memberships"] == []
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(agent)).status_code
        == 404
    )


# --- Removal side effects ---------------------------------------------------------


def test_removed_member_conversations_are_unassigned(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    ws = owner["memberships"][0]["workspaces"][0]["id"]
    agent = _join(client, owner, sent_emails)
    agent_user = agent["user"]["id"]
    ids = {
        k: str(uuid.uuid4()) for k in ("inbox", "contact", "identity", "open", "done")
    }
    _sql(
        "INSERT INTO inboxes (id, organization_id, workspace_id, name, provider) "
        "VALUES (:inbox, :org, :ws, 'Website', 'web_widget')",
        org=org,
        ws=ws,
        **ids,
    )
    _sql(
        "INSERT INTO inbox_members (organization_id, workspace_id, inbox_id, user_id) "
        "VALUES (:org, :ws, :inbox, :user)",
        org=org,
        ws=ws,
        inbox=ids["inbox"],
        user=agent_user,
    )
    _sql(
        "INSERT INTO contacts (id, organization_id, workspace_id) VALUES (:contact, :org, :ws)",
        org=org,
        ws=ws,
        contact=ids["contact"],
    )
    _sql(
        "INSERT INTO contact_identities (id, organization_id, workspace_id, contact_id, "
        "inbox_id, source_id) VALUES (:identity, :org, :ws, :contact, :inbox, 'v1')",
        org=org,
        ws=ws,
        **{k: ids[k] for k in ("identity", "contact", "inbox")},
    )
    for n, (key, status) in enumerate((("open", "open"), ("done", "resolved")), 1):
        _sql(
            "INSERT INTO conversations (id, organization_id, workspace_id, inbox_id, "
            "contact_id, contact_identity_id, display_number, status, assignee_id) "
            "VALUES (:id, :org, :ws, :inbox, :contact, :identity, :n, "
            "CAST(:status AS conversation_status), :user)",
            id=ids[key],
            org=org,
            ws=ws,
            n=n,
            status=status,
            user=agent_user,
            **{k: ids[k] for k in ("inbox", "contact", "identity")},
        )

    membership_id = agent["memberships"][0]["membership_id"]
    r = client.delete(
        f"{API}/organizations/{org}/members/{membership_id}", headers=bearer(owner)
    )
    assert r.status_code == 204

    rows = dict(
        _sql(
            "SELECT id::text, assignee_id FROM conversations WHERE organization_id = :org",
            org=org,
        )
    )
    assert rows[ids["open"]] is None  # back to the unassigned queue
    assert str(rows[ids["done"]]) == agent_user  # resolved history keeps assignee
    assert (
        _sql("SELECT 1 FROM inbox_members WHERE user_id = :user", user=agent_user) == []
    )


# --- Delete and restore -----------------------------------------------------------


def test_delete_hides_org_from_members_and_owner_can_restore(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    admin = _join(client, owner, sent_emails, role="administrator")

    r = client.request(
        "DELETE",
        f"{API}/organizations/{org}",
        json={"password": PASSWORD},
        headers=bearer(admin),
    )
    assert r.status_code == 403  # owner only
    r = client.request(
        "DELETE",
        f"{API}/organizations/{org}",
        json={"password": "nope"},
        headers=bearer(owner),
    )
    assert r.status_code == 403

    r = client.request(
        "DELETE",
        f"{API}/organizations/{org}",
        json={"password": PASSWORD},
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_deletion"
    assert r.json()["deletion_scheduled_at"]

    # Owner still sees it (to restore), but the API is closed.
    [m] = _me(client, owner)["memberships"]
    assert m["organization_status"] == "pending_deletion"
    r = client.get(f"{API}/organizations/{org}/members", headers=bearer(owner))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "organization_pending_deletion"
    # Everyone else: gone.
    assert _me(client, admin)["memberships"] == []
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(admin)).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/organizations/{org}/restore", headers=bearer(admin)
        ).status_code
        == 404
    )

    r = client.post(f"{API}/organizations/{org}/restore", headers=bearer(owner))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert _me(client, admin)["memberships"][0]["organization_id"] == org


def test_purge_closes_organizations_after_the_grace_period(
    client: TestClient,
) -> None:
    owner = signup(client)
    org = _org(owner)
    r = client.request(
        "DELETE",
        f"{API}/organizations/{org}",
        json={"password": PASSWORD},
        headers=bearer(owner),
    )
    assert r.status_code == 200
    _sql(
        "UPDATE organizations SET deletion_scheduled_at = NOW() - interval '1 minute' "
        "WHERE id = :org",
        org=org,
    )
    # Past the grace period: no restore.
    r = client.post(f"{API}/organizations/{org}/restore", headers=bearer(owner))
    assert r.status_code == 404

    from app.jobs.purge_organizations import main

    asyncio.run(main())
    assert _me(client, owner)["memberships"] == []
    [(deleted_at,)] = _sql(
        "SELECT deleted_at FROM organizations WHERE id = :org", org=org
    )
    assert deleted_at is not None


# --- Suspension -------------------------------------------------------------------


def test_suspended_organization_blocks_the_api_but_not_login(
    client: TestClient,
) -> None:
    owner = signup(client)
    org = _org(owner)
    _sql("UPDATE organizations SET status = 'suspended' WHERE id = :org", org=org)
    [m] = _me(client, owner)["memberships"]
    assert m["organization_status"] == "suspended"
    r = client.get(f"{API}/organizations/{org}", headers=bearer(owner))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "organization_suspended"
    # Outsiders still get 404, never a hint that it exists.
    stranger = signup(client)
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(stranger)).status_code
        == 404
    )


# --- Plan limits ------------------------------------------------------------------


def test_inviting_beyond_the_agent_limit_returns_402(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    _sql(
        "UPDATE organizations SET limits_override = '{\"agents\": 2}' WHERE id = :org",
        org=org,
    )
    r = client.post(
        f"{API}/organizations/{org}/invitations",
        json={"email": unique_email(), "role": "agent"},
        headers=bearer(owner),
    )
    assert r.status_code == 201  # owner + 1 pending = 2 seats
    r = client.post(
        f"{API}/organizations/{org}/invitations",
        json={"email": unique_email(), "role": "agent"},
        headers=bearer(owner),
    )
    assert r.status_code == 402
    assert r.json()["error"] == {
        **r.json()["error"],
        "code": "plan_limit_reached",
        "limit": "agents",
        "max": 2,
    }


# --- Database guarantees (docs/12 §2) ---------------------------------------------


def test_database_allows_one_owner_and_custom_roles_on_agents_only(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    admin = _join(client, owner, sent_emails, role="administrator")
    admin_id = admin["memberships"][0]["membership_id"]
    [(role_id,)] = _sql(
        "INSERT INTO roles (organization_id, name) VALUES (:org, 'Lead') RETURNING id",
        org=org,
    )

    with pytest.raises(IntegrityError, match="org_memberships_one_owner_uniq"):
        _sql(
            "UPDATE organization_memberships SET role = 'owner' WHERE id = :m",
            m=admin_id,
        )
    with pytest.raises(IntegrityError, match="org_memberships_custom_role_agent_chk"):
        _sql(
            "UPDATE organization_memberships SET custom_role_id = :r WHERE id = :m",
            r=role_id,
            m=admin_id,
        )
