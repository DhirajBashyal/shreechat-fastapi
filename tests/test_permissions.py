"""Permission resolution (docs/12 §4, §5.4). No database needed."""

from app.core.permissions import (
    ALL_PERMISSIONS,
    BASE_ROLE_PERMISSIONS,
    NEVER_IN_CUSTOM_ROLES,
    effective_permissions,
)
from app.models.enums import MembershipRole


def test_catalog_has_the_25_documented_codes() -> None:
    assert len(ALL_PERMISSIONS) == 25


def test_base_roles() -> None:
    assert BASE_ROLE_PERMISSIONS[MembershipRole.OWNER] == ALL_PERMISSIONS
    assert BASE_ROLE_PERMISSIONS[MembershipRole.ADMINISTRATOR] == ALL_PERMISSIONS - {
        "billing.manage"
    }
    assert len(BASE_ROLE_PERMISSIONS[MembershipRole.AGENT]) == 6


def test_custom_role_replaces_agent_defaults_and_never_grants_reserved_codes() -> None:
    custom = frozenset({"report.view", "label.manage"}) | NEVER_IN_CUSTOM_ROLES
    perms = effective_permissions(MembershipRole.AGENT, custom)
    assert perms == {"report.view", "label.manage"}
    assert "conversation.manage" not in perms  # replaced, not added


def test_custom_roles_do_not_apply_to_owners_or_administrators() -> None:
    for role in (MembershipRole.OWNER, MembershipRole.ADMINISTRATOR):
        assert effective_permissions(role, frozenset()) == BASE_ROLE_PERMISSIONS[role]
