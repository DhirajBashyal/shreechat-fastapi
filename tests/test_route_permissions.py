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
    ("POST", "/invitations/accept"): (True, None, None),
    ("GET", "/organizations/{organization_id}"): (True, "O", None),
    ("PATCH", "/organizations/{organization_id}"): (True, "O", "organization.manage"),
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
    ("GET", "/workspaces/{workspace_id}"): (True, "W", None),
    ("GET", "/workspaces/{workspace_id}/members"): (True, "W", None),
}


def _inspect(dep: Dependant, found: dict[str, Any]) -> None:
    call = dep.call
    if call is deps.get_current_user:
        found["auth"] = True
    if call is deps.get_org_context:
        found["tenant"] = "O"
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
        assert perm is None or perm in PERMISSIONS, f"{key}: unknown permission {perm}"
