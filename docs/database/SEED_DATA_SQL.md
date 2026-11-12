# Seed Data SQL

Two kinds of seed data:

| Part | Contents | Environments | How it runs |
|---|---|---|---|
| **A — reference data** | Permission catalog, subscription plans | All | Alembic migration `0002_reference_data` (idempotent upserts) |
| **B — demo tenant** | Demo users, organization, workspace, team, website inbox, labels, canned responses, custom attributes, an automation rule, one conversation with two messages | Development and CI only | `python -m converse.cli seed-demo` (runs the SQL as `converse_system`) |

The script below was run **twice in a row** against the tested schema without errors (every insert is `ON CONFLICT DO NOTHING` / upsert).
Fixed UUIDs make the demo data easy to reference in tests and API examples:

| Entity | UUID |
|---|---|
| Owner user `owner@demo.test` | `00000000-0000-0000-0000-00000000a001` |
| Agent user `agent@demo.test` | `00000000-0000-0000-0000-00000000a002` |
| Organization "Demo Org" | `00000000-0000-0000-0000-00000000b001` |
| Custom role "Support Lead" | `00000000-0000-0000-0000-00000000c001` |
| Workspace "Customer Support" | `00000000-0000-0000-0000-00000000e001` |
| Team "Tier 1" | `00000000-0000-0000-0000-00000000f001` |
| Website inbox | `00000000-0000-0000-0000-000000010001` (website token `demo_website_token_0001`) |
| Contact "Casey Customer" | `00000000-0000-0000-0000-000000020001` |
| Conversation #1 | `00000000-0000-0000-0000-000000030001` |

> The password hashes are placeholders. `seed-demo` replaces them with a real argon2id hash of `Password123!` before inserting.
> Never run Part B in production.

```sql
-- =====================================================================
-- Converse platform — seed data
-- Part A (reference data) runs in every environment.
-- Part B (demo tenant) runs in development only.
-- Run as the schema owner or converse_system (bypasses RLS).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Part A: permission catalog
-- ---------------------------------------------------------------------
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
        price_cents = EXCLUDED.price_cents, is_public = EXCLUDED.is_public;

-- ---------------------------------------------------------------------
-- Part B: development demo tenant (fixed UUIDs so tests can reference them)
-- Password for both users is "Password123!" (argon2id hash below is a
-- placeholder: regenerate with `python -m converse.cli hash-password`).
-- ---------------------------------------------------------------------
BEGIN;

INSERT INTO users (id, email, password_hash, name, email_verified_at) VALUES
    ('00000000-0000-0000-0000-00000000a001', 'owner@demo.test',
     '$argon2id$v=19$m=65536,t=3,p=4$REPLACE_ME$REPLACE_ME', 'Olivia Owner', NOW()),
    ('00000000-0000-0000-0000-00000000a002', 'agent@demo.test',
     '$argon2id$v=19$m=65536,t=3,p=4$REPLACE_ME$REPLACE_ME', 'Arjun Agent', NOW())
ON CONFLICT DO NOTHING;

INSERT INTO platform_admins (user_id) VALUES ('00000000-0000-0000-0000-00000000a001')
ON CONFLICT DO NOTHING;

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
    ('00000000-0000-0000-0000-00000000d002', '00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000a002', 'agent', '00000000-0000-0000-0000-00000000c001')
ON CONFLICT DO NOTHING;

INSERT INTO workspaces (id, organization_id, name, slug) VALUES
    ('00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000b001', 'Customer Support', 'support')
ON CONFLICT DO NOTHING;

INSERT INTO workspace_memberships (organization_id, workspace_id, membership_id) VALUES
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000d001'),
    ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-00000000e001', '00000000-0000-0000-0000-00000000d002')
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

COMMIT;
```
