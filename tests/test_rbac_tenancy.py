"""Role-based access and multi-tenant isolation."""

import re
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import API, PASSWORD, bearer, signup, unique_email


def _org(tokens: dict[str, Any]) -> str:
    return str(tokens["memberships"][0]["organization_id"])


def _invite(
    client: TestClient,
    owner: dict[str, Any],
    sent_emails: list[dict[str, str]],
    role: str = "agent",
) -> tuple[str, str]:
    email = unique_email(role)
    r = client.post(
        f"{API}/organizations/{_org(owner)}/invitations",
        json={"email": email, "role": role},
        headers=bearer(owner),
    )
    assert r.status_code == 201, r.text
    match = re.search(r"token=([\w\-]+)", sent_emails[-1]["body"])
    assert match is not None
    return email, match.group(1)


def _member_join(
    client: TestClient,
    owner: dict[str, Any],
    sent_emails: list[dict[str, str]],
    role: str = "agent",
) -> dict[str, Any]:
    email, token = _invite(client, owner, sent_emails, role)
    return signup(client, email=email, invitation_token=token)


def test_invited_agent_joins_with_agent_role(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client, org="Rbac Co")
    agent = _member_join(client, owner, sent_emails)
    [m] = agent["memberships"]
    assert m["organization_id"] == _org(owner)
    assert m["role"] == "agent"
    assert "member.manage" not in m["permissions"]
    assert "conversation.manage" in m["permissions"]
    # Agents only see workspaces they were added to: none yet.
    assert m["workspaces"] == []


def test_agent_is_forbidden_from_admin_actions(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    agent = _member_join(client, owner, sent_emails)
    org = _org(owner)

    # Any member can read the org and its members...
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(agent)).status_code
        == 200
    )
    assert (
        client.get(
            f"{API}/organizations/{org}/members", headers=bearer(agent)
        ).status_code
        == 200
    )
    # ...but not invite, change settings, create workspaces or read the audit log.
    for method, path, body in [
        ("post", f"/organizations/{org}/invitations", {"email": unique_email()}),
        ("patch", f"/organizations/{org}", {"name": "Hacked"}),
        ("post", f"/organizations/{org}/workspaces", {"name": "Mine"}),
        ("get", f"/organizations/{org}/audit-logs", None),
    ]:
        r = client.request(method, f"{API}{path}", json=body, headers=bearer(agent))
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "permission_denied"


def test_agent_sees_only_assigned_workspaces(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    agent = _member_join(client, owner, sent_emails)
    agent_membership = agent["memberships"][0]["membership_id"]

    ws = client.post(
        f"{API}/organizations/{org}/workspaces",
        json={"name": "EU Sales", "slug": "eu-sales"},
        headers=bearer(owner),
    )
    assert ws.status_code == 201, ws.text
    ws_id = ws.json()["id"]

    # Not assigned yet → 404 (not 403), and not listed.
    assert (
        client.get(f"{API}/workspaces/{ws_id}", headers=bearer(agent)).status_code
        == 404
    )
    listed = client.get(f"{API}/organizations/{org}/workspaces", headers=bearer(agent))
    assert listed.json() == []

    assign = client.patch(
        f"{API}/organizations/{org}/members/{agent_membership}",
        json={"workspace_ids": [ws_id]},
        headers=bearer(owner),
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["workspace_ids"] == [ws_id]

    assert (
        client.get(f"{API}/workspaces/{ws_id}", headers=bearer(agent)).status_code
        == 200
    )
    listed = client.get(f"{API}/organizations/{org}/workspaces", headers=bearer(agent))
    assert [w["id"] for w in listed.json()] == [ws_id]


def test_only_owner_manages_administrators(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    admin = _member_join(client, owner, sent_emails, role="administrator")
    assert admin["memberships"][0]["role"] == "administrator"
    assert "member.manage" in admin["memberships"][0]["permissions"]
    assert "billing.manage" not in admin["memberships"][0]["permissions"]

    # Administrator can invite agents...
    r = client.post(
        f"{API}/organizations/{org}/invitations",
        json={"email": unique_email(), "role": "agent"},
        headers=bearer(admin),
    )
    assert r.status_code == 201
    # ...but not other administrators.
    r = client.post(
        f"{API}/organizations/{org}/invitations",
        json={"email": unique_email(), "role": "administrator"},
        headers=bearer(admin),
    )
    assert r.status_code == 403

    # Nobody can change or remove the owner through member endpoints.
    owner_membership = owner["memberships"][0]["membership_id"]
    r = client.delete(
        f"{API}/organizations/{org}/members/{owner_membership}", headers=bearer(admin)
    )
    assert r.status_code == 403


def test_role_change_applies_immediately(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    org = _org(owner)
    agent = _member_join(client, owner, sent_emails)
    membership_id = agent["memberships"][0]["membership_id"]

    r = client.patch(
        f"{API}/organizations/{org}/members/{membership_id}",
        json={"role": "administrator"},
        headers=bearer(owner),
    )
    assert r.status_code == 200
    # Same access token, new permissions (they are resolved per request).
    r = client.get(f"{API}/organizations/{org}/audit-logs", headers=bearer(agent))
    assert r.status_code == 200
    actions = {row["action"] for row in r.json()}
    assert {"member.updated", "invitation.created"} <= actions

    # Removed member loses access at once.
    assert (
        client.delete(
            f"{API}/organizations/{org}/members/{membership_id}", headers=bearer(owner)
        ).status_code
        == 204
    )
    assert (
        client.get(f"{API}/organizations/{org}", headers=bearer(agent)).status_code
        == 404
    )


def test_cross_tenant_access_is_404(client: TestClient) -> None:
    org_a = signup(client, org="Tenant A")
    org_b = signup(client, org="Tenant B")
    a_org = _org(org_a)
    a_ws = org_a["memberships"][0]["workspaces"][0]["id"]

    for path in [
        f"/organizations/{a_org}",
        f"/organizations/{a_org}/members",
        f"/organizations/{a_org}/workspaces",
        f"/organizations/{a_org}/invitations",
        f"/workspaces/{a_ws}",
        f"/workspaces/{a_ws}/members",
    ]:
        r = client.get(f"{API}{path}", headers=bearer(org_b))
        assert r.status_code == 404, (path, r.status_code)

    # Org B's user only ever sees their own organization.
    orgs = client.get(f"{API}/organizations", headers=bearer(org_b)).json()
    assert [o["id"] for o in orgs] == [_org(org_b)]


def test_logged_in_user_accepts_invitation(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    inviter = signup(client, org="Inviter")
    existing = signup(client, org="Own Org")
    r = client.post(
        f"{API}/organizations/{_org(inviter)}/invitations",
        json={"email": existing["_email"], "role": "agent"},
        headers=bearer(inviter),
    )
    assert r.status_code == 201
    match = re.search(r"token=([\w\-]+)", sent_emails[-1]["body"])
    assert match is not None

    r = client.post(
        f"{API}/invitations/accept",
        json={"token": match.group(1)},
        headers=bearer(existing),
    )
    assert r.status_code == 200, r.text
    roles = {m["organization_name"]: m["role"] for m in r.json()["memberships"]}
    assert roles == {"Own Org": "owner", "Inviter": "agent"}

    # The token works once.
    again = client.post(
        f"{API}/invitations/accept",
        json={"token": match.group(1)},
        headers=bearer(existing),
    )
    assert again.status_code == 404


def test_login_lists_all_memberships(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    email, token = _invite(client, owner, sent_emails)
    signup(client, email=email, invitation_token=token)
    r = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    assert [m["role"] for m in r.json()["memberships"]] == ["agent"]
