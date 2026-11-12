"""Permission catalog and base-role resolution (docs/10_AUTHENTICATION_AND_RBAC.md §4)."""

from typing import Final

from app.models.enums import MembershipRole

PERMISSIONS: Final[dict[str, str]] = {
    "conversation.view_all": "View every conversation in the workspace",
    "conversation.view_unassigned": "View unassigned conversations in member inboxes",
    "conversation.view_participating": "View conversations the user is assigned to or participating in",
    "conversation.manage": "Reply, assign, label, change status of visible conversations",
    "conversation.delete": "Delete conversations",
    "contact.manage": "Create, update, merge and delete contacts",
    "contact.export": "Export contacts",
    "inbox.manage": "Create inboxes and connect channels",
    "team.manage": "Manage teams and team members",
    "member.manage": "Invite, remove and change roles of members",
    "label.manage": "Manage labels",
    "canned_response.manage": "Manage canned responses",
    "custom_attribute.manage": "Manage custom attribute definitions",
    "automation.manage": "Manage automation rules and macros",
    "webhook.manage": "Manage outgoing webhooks and API tokens",
    "ai.agent.manage": "Create and configure AI agents: prompts, models, tools, actions, handoff rules, inboxes",
    "ai.credentials.manage": "Add and remove AI provider API keys for the organization",
    "ai.knowledge.manage": "Manage knowledge bases and their documents",
    "ai.playground.use": "Test AI agents in the playground (no customer-facing effects)",
    "ai.activity.view": "View AI runs, sources and tool calls on visible conversations",
    "conversation.ai_control": "Pause the AI on a visible conversation or hand it back to the AI",
    "report.view": "View analytics and reports",
    "billing.manage": "Manage subscription and billing",
    "audit_log.view": "View audit logs",
    "organization.manage": "Change organization settings and workspaces",
}

ALL_PERMISSIONS: Final[frozenset[str]] = frozenset(PERMISSIONS)

BASE_ROLE_PERMISSIONS: Final[dict[MembershipRole, frozenset[str]]] = {
    MembershipRole.OWNER: ALL_PERMISSIONS,
    MembershipRole.ADMINISTRATOR: ALL_PERMISSIONS - {"billing.manage"},
    MembershipRole.AGENT: frozenset(
        {
            "conversation.view_participating",
            "conversation.view_unassigned",
            "conversation.manage",
            "contact.manage",
            "ai.activity.view",
            "conversation.ai_control",
        }
    ),
}

# Custom roles may never grant these (only owners/administrators have them).
NEVER_IN_CUSTOM_ROLES: Final[frozenset[str]] = frozenset(
    {"ai.credentials.manage", "billing.manage", "organization.manage", "member.manage"}
)


def effective_permissions(
    role: MembershipRole, custom_role_permissions: frozenset[str] | None
) -> frozenset[str]:
    """A custom role REPLACES the agent defaults. Custom roles only apply to
    agents; owners and administrators always have their full set."""
    if role == MembershipRole.AGENT and custom_role_permissions is not None:
        return custom_role_permissions - NEVER_IN_CUSTOM_ROLES
    return BASE_ROLE_PERMISSIONS[role]
