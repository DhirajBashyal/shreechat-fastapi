"""reference data: permission catalog and subscription plans

Source: docs/database/SEED_DATA_SQL.md (Part A). Idempotent upserts.

Revision ID: 3b9d1c2e7a41
Revises: 68c4a56bbb96
Create Date: 2026-11-12 17:00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b9d1c2e7a41"
down_revision: Union[str, Sequence[str], None] = "68c4a56bbb96"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REFERENCE_DATA_STATEMENTS = [
    """
INSERT INTO permissions (code, description) VALUES
    ('conversation.view_all',          'View every conversation in the workspace'),
    ('conversation.view_unassigned',   'View unassigned conversations in member inboxes'),
    ('conversation.view_participating','View conversations the user is assigned to or participating in'),
    ('conversation.manage',            'Reply, assign, label, change status of visible conversations'),
    ('conversation.delete',            'Delete conversations'),
    ('contact.manage',                 'Create, update, merge and delete contacts'),
    ('contact.export',                 'Export contacts'),
    ('inbox.manage',                   'Create inboxes and connect channels'),
    ('team.manage',                    'Manage teams and team members'),
    ('member.manage',                  'Invite, remove and change roles of members'),
    ('label.manage',                   'Manage labels'),
    ('canned_response.manage',         'Manage canned responses'),
    ('custom_attribute.manage',        'Manage custom attribute definitions'),
    ('automation.manage',              'Manage automation rules and macros'),
    ('webhook.manage',                 'Manage outgoing webhooks and API tokens'),
    ('ai.agent.manage',                'Create and configure AI agents: prompts, models, tools, actions, handoff rules, inboxes'),
    ('ai.credentials.manage',          'Add and remove AI provider API keys for the organization'),
    ('ai.knowledge.manage',            'Manage knowledge bases and their documents'),
    ('ai.playground.use',              'Test AI agents in the playground (no customer-facing effects)'),
    ('ai.activity.view',               'View AI runs, sources and tool calls on visible conversations'),
    ('conversation.ai_control',        'Pause the AI on a visible conversation or hand it back to the AI'),
    ('report.view',                    'View analytics and reports'),
    ('billing.manage',                 'Manage subscription and billing'),
    ('audit_log.view',                 'View audit logs'),
    ('organization.manage',            'Change organization settings and workspaces')
ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description;
""",
    """
-- ---------------------------------------------------------------------
-- Part A: subscription plans
-- limits: max values enforced by the application (null key = unlimited)
-- ---------------------------------------------------------------------
INSERT INTO subscription_plans (code, name, billing_interval, price_cents, currency, limits, features, is_public) VALUES
    ('free', 'Free', 'month', 0, 'USD',
        '{"agents": 2, "inboxes": 2, "workspaces": 1, "ai_responses_per_month": 100, "storage_gb": 1}'::jsonb,
        ARRAY['web_widget', 'facebook_messenger', 'instagram', 'whatsapp_cloud'], true),
    ('starter_monthly', 'Starter', 'month', 1900, 'USD',
        '{"agents": 5, "inboxes": 10, "workspaces": 1, "ai_responses_per_month": 1000, "storage_gb": 10}'::jsonb,
        ARRAY['web_widget', 'facebook_messenger', 'instagram', 'whatsapp_cloud', 'automation', 'canned_responses', 'webhooks'], true),
    ('business_monthly', 'Business', 'month', 4900, 'USD',
        '{"agents": 25, "inboxes": 50, "workspaces": 5, "ai_responses_per_month": 10000, "storage_gb": 100}'::jsonb,
        ARRAY['web_widget', 'facebook_messenger', 'instagram', 'whatsapp_cloud', 'automation', 'canned_responses', 'webhooks', 'ai_agent', 'custom_roles', 'reports_advanced', 'audit_logs'], true),
    ('enterprise_custom', 'Enterprise', 'year', 0, 'USD',
        '{}'::jsonb,
        ARRAY['web_widget', 'facebook_messenger', 'instagram', 'whatsapp_cloud', 'automation', 'canned_responses', 'webhooks', 'ai_agent', 'custom_roles', 'reports_advanced', 'audit_logs', 'sso', 'byo_llm_keys'], false)
ON CONFLICT (code) DO UPDATE
    SET name = EXCLUDED.name, limits = EXCLUDED.limits, features = EXCLUDED.features,
        price_cents = EXCLUDED.price_cents, is_public = EXCLUDED.is_public;;
""",
]


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for statement in REFERENCE_DATA_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        "DELETE FROM subscription_plans WHERE code IN "
        "('free', 'starter_monthly', 'business_monthly', 'enterprise_custom')"
    )
    op.execute("DELETE FROM permissions")
