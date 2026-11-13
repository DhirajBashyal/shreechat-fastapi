"""Development seed data: a demo tenant to log in with.

Source: docs/database/SEED_DATA_SQL.md, Part B (demo tenant). Reference data
(permission catalog, subscription plans) is Part A and lives in the Alembic
migration 3b9d1c2e7a41_reference_data, so run migrations first.

    uv run alembic upgrade head
    uv run python seeder.py            # or: make seed

Idempotent: every insert is ON CONFLICT DO NOTHING, so running it twice is
safe. Refuses to run unless ENV is local/test (use --force to override).
Never run it in production.

Accounts created (password for all: Password123!):

    owner@demo.example.com     Olivia Owner     owner (also platform admin)
    admin@demo.example.com     Adam Admin       administrator   (addition, not in docs)
    agent@demo.example.com     Arjun Agent      agent with custom role "Support Lead"
    platform@demo.example.com  Pat Platform     platform admin only, no organization
                                                (addition: staff login, docs/12 §7.1)

Website widget: website_token demo_website_token_0001, identity secret
demo_widget_identity_secret_0001, allowed origin http://localhost:5173.
"""

import argparse
import sys

import psycopg
from psycopg import sql

from app.core.config import settings
from app.core.crypto import KEY_VERSION, SecretKeyMissingError, encrypt_secret
from app.core.security import hash_password

DEMO_PASSWORD = "Password123!"
# Website widget (docs/integrations/WEBSITE_CHAT.md): identifier_hash =
# hex(HMAC_SHA256(DEMO_WIDGET_IDENTITY_SECRET, identifier)). Demo only.
DEMO_WEBSITE_TOKEN = "demo_website_token_0001"
DEMO_WIDGET_IDENTITY_SECRET = "demo_widget_identity_secret_0001"

# Fixed UUIDs (docs/database/SEED_DATA_SQL.md) so tests and API examples can
# reference them.
OWNER_ID = "00000000-0000-0000-0000-00000000a001"
AGENT_ID = "00000000-0000-0000-0000-00000000a002"
ADMIN_ID = "00000000-0000-0000-0000-00000000a003"  # addition
PLATFORM_ID = "00000000-0000-0000-0000-00000000a004"  # addition: staff account
ORG_ID = "00000000-0000-0000-0000-00000000b001"
WORKSPACE_ID = "00000000-0000-0000-0000-00000000e001"

USERS = [
    (
        OWNER_ID,
        "owner@demo.example.com",
        "Olivia Owner",
        "owner of Demo Org + platform admin",
    ),
    (
        AGENT_ID,
        "agent@demo.example.com",
        "Arjun Agent",
        'agent, custom role "Support Lead"',
    ),
    (ADMIN_ID, "admin@demo.example.com", "Adam Admin", "administrator of Demo Org"),
    (
        PLATFORM_ID,
        "platform@demo.example.com",
        "Pat Platform",
        "platform admin, no organization",
    ),
]

# Platform admins (looked up by email, so an existing account with the same
# email is reused instead of breaking the foreign key).
PLATFORM_ADMIN_EMAILS = ("owner@demo.example.com", "platform@demo.example.com")

# Everything after the users, from SEED_DATA_SQL.md Part B (plus the admin
# membership marked below).
DEMO_TENANT_SQL = """

INSERT INTO organizations (id, name, slug, created_by) VALUES
    ('00000000-0000-0000-0000-00000000b001', 'Demo Org', 'demo', '00000000-0000-0000-0000-00000000a001')
ON CONFLICT DO NOTHING;

INSERT INTO subscriptions (organization_id, plan_id, state, current_period_start, current_period_end, trial_ends_at)
SELECT '00000000-0000-0000-0000-00000000b001', id, 'trialing', NOW(), NOW() + INTERVAL '14 days', NOW() + INTERVAL '14 days'
FROM subscription_plans WHERE code = 'business_monthly'
ON CONFLICT DO NOTHING;

INSERT INTO roles (id, organization_id, name, description) VALUES
    ('00000000-0000-0000-0000-00000000c001', '00000000-0000-0000-0000-00000000b001', 'Support Lead',
     'Agent who can also see reports and manage canned responses')
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (organization_id, role_id, permission_code) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'conversation.view_all'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'conversation.manage'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'contact.manage'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'canned_response.manage'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'report.view'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'ai.activity.view'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000c001', 'conversation.ai_control')
ON CONFLICT DO NOTHING;

INSERT INTO organization_memberships (id, organization_id, user_id, role, custom_role_id) VALUES
    ('00000000-0000-0000-0000-00000000d001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000a001', 'owner', NULL),
    ('00000000-0000-0000-0000-00000000d002', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000a002', 'agent', '00000000-0000-0000-0000-00000000c001'),
    -- addition: administrator, so every base role can be tested
    ('00000000-0000-0000-0000-00000000d003', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000a003', 'administrator', NULL)
ON CONFLICT DO NOTHING;

INSERT INTO workspaces (id, organization_id, name, slug) VALUES
    ('00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000b001', 'Customer Support', 'support')
ON CONFLICT DO NOTHING;

INSERT INTO workspace_memberships (organization_id, workspace_id, membership_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000d001'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000d002'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000d003')
ON CONFLICT DO NOTHING;

INSERT INTO teams (id, organization_id, workspace_id, name, description) VALUES
    ('00000000-0000-0000-0000-00000000f001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'Tier 1', 'First-line support')
ON CONFLICT DO NOTHING;

INSERT INTO team_members (organization_id, workspace_id, team_id, user_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000f001', '00000000-0000-0000-0000-00000000a002')
ON CONFLICT DO NOTHING;

-- Website chat inbox
INSERT INTO inboxes (id, organization_id, workspace_id, name, provider, greeting_enabled, greeting_message) VALUES
    ('00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     'Website', 'web_widget', true, 'Hi! How can we help you today?')
ON CONFLICT DO NOTHING;

INSERT INTO channel_connections (id, organization_id, workspace_id, inbox_id, provider, status, connected_at) VALUES
    ('00000000-0000-0000-0000-000000011001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     '00000000-0000-0000-0000-000000010001', 'web_widget', 'active', NOW())
ON CONFLICT DO NOTHING;

INSERT INTO web_widgets (organization_id, workspace_id, channel_connection_id, website_token, website_url, allowed_origins, welcome_title) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-000000011001',
     'demo_website_token_0001', 'http://localhost:5173', ARRAY['http://localhost:5173'], 'Welcome to Demo')
ON CONFLICT DO NOTHING;

-- Labels the website widget may set (converse.setLabel)
INSERT INTO labels (organization_id, workspace_id, title, description, color, widget_settable) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'sales', 'Sales enquiry (set by the website widget)', '#10b981', true)
ON CONFLICT DO NOTHING;

INSERT INTO channel_routes (provider, external_id, organization_id, workspace_id, inbox_id, channel_connection_id) VALUES
    ('web_widget', 'demo_website_token_0001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     '00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-000000011001')
ON CONFLICT DO NOTHING;

INSERT INTO inbox_members (organization_id, workspace_id, inbox_id, user_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-00000000a001'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-00000000a002')
ON CONFLICT DO NOTHING;

INSERT INTO labels (organization_id, workspace_id, title, description, color) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'billing', 'Billing questions', '#f59e0b'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'bug', 'Product bugs', '#ef4444'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'vip', 'High value customer', '#8b5cf6')
ON CONFLICT DO NOTHING;

INSERT INTO canned_responses (organization_id, workspace_id, short_code, content, created_by) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'thanks',
     'Thanks for reaching out, {{contact.first_name}}! Let me look into this for you.', '00000000-0000-0000-0000-00000000a001'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'refund',
     'Refunds are processed within 5-7 business days to the original payment method.', '00000000-0000-0000-0000-00000000a001')
ON CONFLICT DO NOTHING;

INSERT INTO custom_attribute_definitions (organization_id, workspace_id, scope, attribute_key, display_name, value_type, list_values) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'contact', 'plan', 'Plan', 'list', ARRAY['free', 'pro', 'enterprise']),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', 'conversation', 'order_id', 'Order ID', 'text', '{}')
ON CONFLICT DO NOTHING;

INSERT INTO automation_rules (organization_id, workspace_id, name, event_name, conditions, actions, created_by) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     'Label refund requests', 'message.created',
     '[{"attribute": "message.content", "operator": "contains", "values": ["refund"], "query_operator": null}]'::jsonb,
     '[{"action": "add_label", "params": {"labels": ["billing"]}}, {"action": "assign_team", "params": {"team_id": "00000000-0000-0000-0000-00000000f001"}}]'::jsonb,
     '00000000-0000-0000-0000-00000000a001')
ON CONFLICT DO NOTHING;

-- A sample conversation with two messages
INSERT INTO contacts (id, organization_id, workspace_id, name, email, contact_type) VALUES
    ('00000000-0000-0000-0000-000000020001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     'Casey Customer', 'casey@example.com', 'lead')
ON CONFLICT DO NOTHING;

INSERT INTO contact_identities (id, organization_id, workspace_id, contact_id, inbox_id, source_id) VALUES
    ('00000000-0000-0000-0000-000000021001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
     '00000000-0000-0000-0000-000000020001', '00000000-0000-0000-0000-000000010001', 'visitor-7d1c2c1e')
ON CONFLICT DO NOTHING;

INSERT INTO conversations (id, organization_id, workspace_id, inbox_id, contact_id, contact_identity_id, display_number, status, last_incoming_at, waiting_since)
SELECT '00000000-0000-0000-0000-000000030001', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001',
       '00000000-0000-0000-0000-000000010001', '00000000-0000-0000-0000-000000020001', '00000000-0000-0000-0000-000000021001',
       next_workspace_counter('00000000-0000-0000-0000-00000000e001', 'conversation'), 'open', NOW(), NOW()
WHERE NOT EXISTS (SELECT 1 FROM conversations WHERE id = '00000000-0000-0000-0000-000000030001');

INSERT INTO messages (organization_id, workspace_id, inbox_id, conversation_id, direction, content, sender_kind, sender_contact_id, client_message_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-000000010001',
     '00000000-0000-0000-0000-000000030001', 'incoming', 'Hi, I would like a refund for order #1234', 'contact', '00000000-0000-0000-0000-000000020001', 'seed-msg-1')
ON CONFLICT DO NOTHING;

INSERT INTO messages (organization_id, workspace_id, inbox_id, conversation_id, direction, content, is_private, sender_kind, sender_user_id, client_message_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-000000010001',
     '00000000-0000-0000-0000-000000030001', 'outgoing', 'Checking order #1234 in Stripe before replying.', true, 'user', '00000000-0000-0000-0000-00000000a002', 'seed-msg-2')
ON CONFLICT DO NOTHING;
"""


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _sync_dsn(url: str) -> str:
    """postgresql+asyncpg://... → postgresql://... for psycopg."""
    scheme, rest = url.split("://", 1)
    return f"{scheme.split('+', 1)[0]}://{rest}"


def _count(conn: psycopg.Connection, table: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE organization_id = %s").format(
        sql.Identifier(table)
    )
    row = conn.execute(query, (ORG_ID,)).fetchone()
    return int(row[0]) if row else 0


def seed() -> None:
    password_hash = hash_password(DEMO_PASSWORD)
    with psycopg.connect(_sync_dsn(settings.DB_URL)) as conn:
        # One transaction: either everything is seeded or nothing.
        with conn.transaction():
            plans = conn.execute("SELECT count(*) FROM subscription_plans").fetchone()
            if not plans or plans[0] == 0:
                sys.exit(
                    "Reference data missing (no subscription plans). "
                    "Run `uv run alembic upgrade head` first."
                )
            # Users first, parameterised (the password hash is generated here).
            for user_id, email, name, _ in USERS:
                conn.execute(
                    """
                    INSERT INTO users (id, email, password_hash, name, email_verified_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, email, password_hash, name),
                )
            for email in PLATFORM_ADMIN_EMAILS:
                conn.execute(
                    """
                    INSERT INTO platform_admins (user_id)
                    SELECT id FROM users WHERE email = %s AND deleted_at IS NULL
                    ON CONFLICT DO NOTHING
                    """,
                    (email,),
                )
            # The rest has no parameters, so it runs as one script.
            conn.execute(DEMO_TENANT_SQL)  # type: ignore[call-overload]
            try:
                ciphertext = encrypt_secret(DEMO_WIDGET_IDENTITY_SECRET)
            except SecretKeyMissingError:
                ciphertext = None
            if ciphertext is not None:
                conn.execute(
                    """
                    UPDATE web_widgets
                    SET identity_hmac_secret_ciphertext = %s, identity_hmac_key_version = %s
                    WHERE website_token = %s AND identity_hmac_secret_ciphertext IS NULL
                    """,
                    (ciphertext, KEY_VERSION, DEMO_WEBSITE_TOKEN),
                )

        counts = {
            table: _count(conn, table)
            for table in (
                "organization_memberships",
                "workspaces",
                "inboxes",
                "conversations",
                "messages",
            )
        }

    _out("Demo tenant seeded (Demo Org).")
    _out(f"  rows: {counts}")
    _out(f"  Password for all accounts: {DEMO_PASSWORD}")
    for _, email, name, what in USERS:
        _out(f"    {email:<28} {name:<14} {what}")
    _out("  Platform console: log in as platform@demo.example.com (local/test only).")
    _out(
        f"  Website widget: website_token={DEMO_WEBSITE_TOKEN}, "
        f"identity secret={DEMO_WIDGET_IDENTITY_SECRET} (origin http://localhost:5173)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the development demo tenant.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when ENV is not local/test (never use in production).",
    )
    args = parser.parse_args()
    if not settings.is_local and not args.force:
        sys.exit(f"Refusing to seed demo data in ENV={settings.ENV!r}. Use --force.")
    seed()


if __name__ == "__main__":
    main()
