"""Route walker (docs/implementation/PHASE_02_AUTHENTICATION.md, step 14).

Fails when an API route doesn't enforce the authentication, tenant check and
permission the API design requires. New routes must be added to EXPECTED, so
none can ship without a deliberate access decision. No database needed.
"""

from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api import deps
from app.core.permissions import PERMISSIONS
from app.main import app

# (method, path) → (requires login, tenant scope "O"/"W"/None, permission or None)
# Source: docs/backend/API_DESIGN.md §2–4. None permission + tenant = any member.
# "owner" = owner-only action (role check, not a permission code; docs/12 §4).
# "platform_admin" = our staff only (/platform/*, docs/12 §7).
EXPECTED: dict[tuple[str, str], tuple[bool, str | None, str | None]] = {
    ("GET", "/health"): (False, None, None),
    ("POST", "/auth/signup"): (False, None, None),
    ("POST", "/auth/login"): (False, None, None),
    ("POST", "/auth/refresh"): (False, None, None),
    ("POST", "/auth/logout"): (True, None, None),
    ("POST", "/auth/logout-all"): (True, None, None),
    ("POST", "/auth/password/forgot"): (False, None, None),
    ("POST", "/auth/password/reset"): (False, None, None),
    ("GET", "/auth/me"): (True, None, None),
    ("GET", "/permissions"): (True, None, None),
    ("GET", "/organizations"): (True, None, None),
    ("GET", "/ai-models"): (True, None, None),
    # Website widget inboxes (docs/integrations/WEBSITE_CHAT.md §7)
    ("POST", "/workspaces/{workspace_id}/channels/web-widget"): (
        True,
        "W",
        "inbox.manage",
    ),
    ("GET", "/workspaces/{workspace_id}/inboxes/{inbox_id}/web-widget"): (
        True,
        "W",
        "inbox.manage",
    ),
    ("PATCH", "/workspaces/{workspace_id}/inboxes/{inbox_id}/web-widget"): (
        True,
        "W",
        "inbox.manage",
    ),
    ("DELETE", "/workspaces/{workspace_id}/inboxes/{inbox_id}/web-widget"): (
        True,
        "W",
        "inbox.manage",
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/inboxes/{inbox_id}/web-widget/identity-secret/rotate",
    ): (True, "W", "inbox.manage"),
    # Website widget visitor API: no user login; website_token + visitor token
    # (docs/integrations/WEBSITE_CHAT.md §3)
    ("GET", "/widget/config"): (False, None, None),
    ("POST", "/widget/sessions"): (False, None, None),
    ("DELETE", "/widget/sessions"): (False, None, None),
    ("POST", "/widget/identify"): (False, None, None),
    ("PATCH", "/widget/contact"): (False, None, None),
    ("DELETE", "/widget/contact/custom-attributes/{key}"): (False, None, None),
    ("GET", "/widget/conversations"): (False, None, None),
    ("POST", "/widget/conversations"): (False, None, None),
    ("PATCH", "/widget/conversations/{conversation_id}"): (False, None, None),
    ("GET", "/widget/conversations/{conversation_id}/messages"): (False, None, None),
    ("POST", "/widget/conversations/{conversation_id}/messages"): (False, None, None),
    ("POST", "/widget/conversations/{conversation_id}/typing"): (False, None, None),
    ("POST", "/widget/conversations/{conversation_id}/seen"): (False, None, None),
    ("POST", "/widget/conversations/{conversation_id}/resolve"): (False, None, None),
    ("POST", "/widget/conversations/{conversation_id}/labels"): (False, None, None),
    ("DELETE", "/widget/conversations/{conversation_id}/labels/{label}"): (
        False,
        None,
        None,
    ),
    ("POST", "/widget/uploads"): (False, None, None),
    ("POST", "/widget/events"): (False, None, None),
    ("POST", "/invitations/accept"): (True, None, None),
    ("GET", "/organizations/{organization_id}"): (True, "O", None),
    ("PATCH", "/organizations/{organization_id}"): (True, "O", "organization.manage"),
    ("DELETE", "/organizations/{organization_id}"): (True, "O", "owner"),
    ("POST", "/organizations/{organization_id}/restore"): (True, "O", "owner"),
    ("POST", "/organizations/{organization_id}/transfer-ownership"): (
        True,
        "O",
        "owner",
    ),
    ("POST", "/organizations/{organization_id}/leave"): (True, "O", None),
    ("GET", "/organizations/{organization_id}/workspaces"): (True, "O", None),
    ("POST", "/organizations/{organization_id}/workspaces"): (
        True,
        "O",
        "organization.manage",
    ),
    ("GET", "/organizations/{organization_id}/members"): (True, "O", None),
    ("PATCH", "/organizations/{organization_id}/members/{membership_id}"): (
        True,
        "O",
        "member.manage",
    ),
    ("DELETE", "/organizations/{organization_id}/members/{membership_id}"): (
        True,
        "O",
        "member.manage",
    ),
    ("POST", "/organizations/{organization_id}/invitations"): (
        True,
        "O",
        "member.manage",
    ),
    ("GET", "/organizations/{organization_id}/invitations"): (
        True,
        "O",
        "member.manage",
    ),
    ("DELETE", "/organizations/{organization_id}/invitations/{invitation_id}"): (
        True,
        "O",
        "member.manage",
    ),
    ("GET", "/organizations/{organization_id}/audit-logs"): (
        True,
        "O",
        "audit_log.view",
    ),
    ("GET", "/platform/stats"): (True, None, "platform_admin"),
    ("GET", "/platform/organizations"): (True, None, "platform_admin"),
    ("GET", "/platform/organizations/{organization_id}"): (
        True,
        None,
        "platform_admin",
    ),
    ("PATCH", "/platform/organizations/{organization_id}"): (
        True,
        None,
        "platform_admin",
    ),
    ("GET", "/platform/admins"): (True, None, "platform_admin"),
    ("POST", "/platform/admins"): (True, None, "platform_admin"),
    ("DELETE", "/platform/admins/{user_id}"): (True, None, "platform_admin"),
    ("GET", "/workspaces/{workspace_id}/knowledge-bases"): (True, "W", None),
    ("POST", "/workspaces/{workspace_id}/knowledge-bases"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    ("GET", "/workspaces/{workspace_id}/knowledge-bases/{kb_id}"): (True, "W", None),
    ("PATCH", "/workspaces/{workspace_id}/knowledge-bases/{kb_id}"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    ("DELETE", "/workspaces/{workspace_id}/knowledge-bases/{kb_id}"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    ("GET", "/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    ("POST", "/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents/{doc_id}/resync",
    ): (True, "W", "ai.knowledge.manage"),
    (
        "DELETE",
        "/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents/{doc_id}",
    ): (True, "W", "ai.knowledge.manage"),
    (
        "GET",
        "/workspaces/{workspace_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
    ): (True, "W", "ai.knowledge.manage"),
    ("POST", "/workspaces/{workspace_id}/knowledge-search"): (
        True,
        "W",
        "ai.knowledge.manage",
    ),
    ("GET", "/organizations/{organization_id}/ai-credentials"): (
        True,
        "O",
        "ai.credentials.manage",
    ),
    ("POST", "/organizations/{organization_id}/ai-credentials"): (
        True,
        "O",
        "ai.credentials.manage",
    ),
    ("DELETE", "/organizations/{organization_id}/ai-credentials/{credential_id}"): (
        True,
        "O",
        "ai.credentials.manage",
    ),
    ("GET", "/workspaces/{workspace_id}/ai-agents"): (True, "W", None),
    ("POST", "/workspaces/{workspace_id}/ai-agents"): (True, "W", "ai.agent.manage"),
    ("GET", "/workspaces/{workspace_id}/ai-agents/{agent_id}"): (True, "W", None),
    ("PATCH", "/workspaces/{workspace_id}/ai-agents/{agent_id}"): (
        True,
        "W",
        "ai.agent.manage",
    ),
    ("DELETE", "/workspaces/{workspace_id}/ai-agents/{agent_id}"): (
        True,
        "W",
        "ai.agent.manage",
    ),
    ("PUT", "/workspaces/{workspace_id}/ai-agents/{agent_id}/inboxes"): (
        True,
        "W",
        "ai.agent.manage",
    ),
    ("PUT", "/workspaces/{workspace_id}/ai-agents/{agent_id}/knowledge-bases"): (
        True,
        "W",
        "ai.agent.manage",
    ),
    ("POST", "/workspaces/{workspace_id}/ai-agents/{agent_id}/playground"): (
        True,
        "W",
        "ai.playground.use",
    ),
    ("GET", "/workspaces/{workspace_id}/conversations/{conversation_id}/ai-runs"): (
        True,
        "W",
        "ai.activity.view",
    ),
    ("POST", "/workspaces/{workspace_id}/conversations/{conversation_id}/ai/pause"): (
        True,
        "W",
        "conversation.ai_control",
    ),
    ("POST", "/workspaces/{workspace_id}/conversations/{conversation_id}/ai/resume"): (
        True,
        "W",
        "conversation.ai_control",
    ),
    ("GET", "/workspaces/{workspace_id}"): (True, "W", None),
    ("GET", "/workspaces/{workspace_id}/members"): (True, "W", None),
}


def _inspect(dep: Dependant, found: dict[str, Any]) -> None:
    call = dep.call
    if call is deps.get_current_user:
        found["auth"] = True
    if call in (deps.get_org_context, deps.get_org_context_for_restore):
        found["tenant"] = "O"
    if call is deps.require_platform_admin:
        found["perm"] = "platform_admin"
    if call is deps.require_owner_for_restore or (
        getattr(call, "__qualname__", "") == "require_owner.<locals>.checker"
    ):
        found["perm"] = "owner"
    if call is deps.get_workspace_context:
        found["tenant"] = "W"
    closure = getattr(call, "__closure__", None) or ()
    if getattr(call, "__name__", "") in ("org_checker", "ws_checker"):
        for cell in closure:
            if isinstance(cell.cell_contents, str):
                found["perm"] = cell.cell_contents
    for sub in dep.dependencies:
        _inspect(sub, found)


def _actual() -> dict[tuple[str, str], tuple[bool, str | None, str | None]]:
    result = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        found: dict[str, Any] = {"auth": False, "tenant": None, "perm": None}
        _inspect(route.dependant, found)
        for method in route.methods:
            result[(method, route.path.removeprefix("/api/v1"))] = (
                found["auth"],
                found["tenant"],
                found["perm"],
            )
    return result


def test_every_route_has_an_access_decision() -> None:
    actual = _actual()
    undeclared = sorted(set(actual) - set(EXPECTED))
    assert not undeclared, (
        f"Add these routes to EXPECTED with their access rule: {undeclared}"
    )
    missing = sorted(set(EXPECTED) - set(actual))
    assert not missing, f"Expected routes not found: {missing}"


def test_routes_enforce_documented_access() -> None:
    actual = _actual()
    wrong = {k: (v, EXPECTED[k]) for k, v in actual.items() if EXPECTED.get(k) != v}
    assert not wrong, f"(actual, expected): {wrong}"


def test_tenant_routes_require_login() -> None:
    for key, (auth, tenant, _) in _actual().items():
        if tenant:
            assert auth, f"{key} is tenant-scoped but doesn't require login"


def test_required_permissions_exist_in_catalog() -> None:
    for key, (_, _, perm) in EXPECTED.items():
        assert perm in (None, "owner", "platform_admin") or perm in PERMISSIONS, (
            f"{key}: unknown permission {perm}"
        )
