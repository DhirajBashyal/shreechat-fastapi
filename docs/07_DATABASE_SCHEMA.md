# 07 — Database Schema

Every table in the Converse schema, grouped by domain, with its DDL and indexes. This file is **generated from the same SQL that was executed and tested** (see [database/INITIAL_SCHEMA_SQL.md](database/INITIAL_SCHEMA_SQL.md) for the single runnable script, including extensions, enum types and the `updated_at` trigger loop). Do not edit DDL here by hand; change the SQL and regenerate.

**66 tables.** Relationship diagrams: [08_DATABASE_RELATIONSHIPS.md](08_DATABASE_RELATIONSHIPS.md). Column-level notes and JSONB shapes: [database/TABLE_DOCUMENTATION.md](database/TABLE_DOCUMENTATION.md).

## Contents

- [1. Platform (global, NOT tenant-scoped)](#1-platform-global-not-tenant-scoped)
- [2. Tenancy: organizations, memberships, roles, workspaces](#2-tenancy-organizations-memberships-roles-workspaces)
- [3. Teams](#3-teams)
- [4. Inboxes & channel connections](#4-inboxes--channel-connections)
- [5. Files, contacts, conversations, messages](#5-files-contacts-conversations-messages)
- [6. Labels, custom attributes, canned responses, notes, macros](#6-labels-custom-attributes-canned-responses-notes-macros)
- [7. Events, webhooks, idempotency](#7-events-webhooks-idempotency)
- [8. Automation](#8-automation)
- [9. AI: providers, agents, knowledge bases, memory, runs](#9-ai-providers-agents-knowledge-bases-memory-runs)
- [10. Reporting, billing, audit](#10-reporting-billing-audit)
- [11. updated_at triggers](#11-updated_at-triggers)

## Shared types and helpers

Enum types, `set_updated_at()` and `current_org_id()` are defined once at the top of the script:

```sql
-- =====================================================================
-- Converse platform — initial PostgreSQL schema (PostgreSQL 16+)
-- Requires extensions: pgcrypto, citext, pg_trgm, vector (pgvector >= 0.7)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------
-- Shared helpers
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

-- Returns the tenant bound to the current transaction (set by the app with
-- SET LOCAL app.current_org_id = '<uuid>'). NULL when unset.
CREATE OR REPLACE FUNCTION current_org_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_org_id', true), '')::uuid;
$$;

-- ---------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------

CREATE TYPE org_status            AS ENUM ('active', 'suspended', 'pending_deletion');
CREATE TYPE membership_role       AS ENUM ('owner', 'administrator', 'agent');
CREATE TYPE availability_status   AS ENUM ('online', 'busy', 'offline');
CREATE TYPE channel_provider      AS ENUM ('web_widget', 'facebook_messenger', 'instagram', 'whatsapp_cloud', 'email', 'api');
CREATE TYPE connection_status     AS ENUM ('pending', 'active', 'reauthorization_required', 'disconnected', 'error');
CREATE TYPE conversation_status   AS ENUM ('open', 'pending', 'snoozed', 'resolved');
CREATE TYPE conversation_priority AS ENUM ('low', 'medium', 'high', 'urgent');
CREATE TYPE message_direction     AS ENUM ('incoming', 'outgoing', 'activity', 'template');
CREATE TYPE message_content_type  AS ENUM ('text', 'input_select', 'cards', 'form', 'article', 'interactive', 'template', 'location', 'contact_card', 'unsupported');
CREATE TYPE delivery_status       AS ENUM ('pending', 'sent', 'delivered', 'read', 'failed');
CREATE TYPE sender_kind           AS ENUM ('user', 'contact', 'ai_agent', 'system', 'automation');
CREATE TYPE attachment_kind       AS ENUM ('image', 'audio', 'video', 'file', 'location', 'sticker', 'fallback');
CREATE TYPE attribute_scope       AS ENUM ('conversation', 'contact');
CREATE TYPE attribute_value_type  AS ENUM ('text', 'number', 'currency', 'percent', 'link', 'date', 'list', 'checkbox');
CREATE TYPE webhook_delivery_state AS ENUM ('pending', 'succeeded', 'failed', 'dead');
CREATE TYPE inbound_event_state   AS ENUM ('received', 'processing', 'processed', 'ignored', 'failed', 'dead');
CREATE TYPE outbox_state          AS ENUM ('pending', 'dispatched', 'failed');
CREATE TYPE ai_run_status         AS ENUM ('running', 'replied', 'handed_off', 'discarded', 'failed');
CREATE TYPE kb_document_status    AS ENUM ('pending', 'processing', 'ready', 'failed');
CREATE TYPE subscription_state    AS ENUM ('trialing', 'active', 'past_due', 'canceled', 'incomplete');
CREATE TYPE billing_record_state  AS ENUM ('draft', 'open', 'paid', 'void', 'uncollectible');
```

## 1. Platform (global, NOT tenant-scoped)

### `users`

**Purpose:** Global person record. One user can belong to many organizations.  
**Chatwoot equivalent:** Chatwoot `users`. `realtime_token` replaces `pubsub_token`. No `tokens` blob: sessions live in `user_sessions`.  
**References:** —

```sql
CREATE TABLE users (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email               citext NOT NULL,
    password_hash       text,
    name                varchar(255) NOT NULL,
    display_name        varchar(255),
    avatar_file_id      uuid,
    email_verified_at   timestamptz,
    mfa_secret_ciphertext bytea,
    mfa_enabled         boolean NOT NULL DEFAULT false,
    mfa_backup_code_hashes text[] NOT NULL DEFAULT '{}',
    ui_settings         jsonb NOT NULL DEFAULT '{}'::jsonb,
    message_signature   text,
    realtime_token      varchar(64) NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
    last_login_at       timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    deleted_at          timestamptz,
    CONSTRAINT users_ui_settings_is_object CHECK (jsonb_typeof(ui_settings) = 'object')
);
CREATE UNIQUE INDEX users_email_uniq ON users (email) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX users_realtime_token_uniq ON users (realtime_token);
ALTER TABLE users ADD CONSTRAINT users_avatar_fk FOREIGN KEY (avatar_file_id) REFERENCES stored_files(id) ON DELETE SET NULL;
```

### `platform_admins`

**Purpose:** Users with platform (super admin) rights.  
**Chatwoot equivalent:** Chatwoot `super_admins` (STI on users).  
**References:** `users`

```sql
CREATE TABLE platform_admins (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    granted_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT platform_admins_user_uniq UNIQUE (user_id)
);
```

### `user_sessions`

**Purpose:** One row per refresh token. Rotation links tokens through `replaced_by_id`; `family_id` lets us revoke a whole chain on reuse.  
**Chatwoot equivalent:** Replaces devise_token_auth `users.tokens`.  
**References:** `users`

```sql
CREATE TABLE user_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_hash  bytea NOT NULL,
    family_id           uuid NOT NULL,
    user_agent          text,
    ip_address          inet,
    expires_at          timestamptz NOT NULL,
    revoked_at          timestamptz,
    replaced_by_id      uuid REFERENCES user_sessions(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    last_used_at        timestamptz
);
CREATE UNIQUE INDEX user_sessions_token_uniq ON user_sessions (refresh_token_hash);
CREATE INDEX user_sessions_user_active_idx ON user_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX user_sessions_family_idx ON user_sessions (family_id);
```

### `permissions`

**Purpose:** Catalog of permission codes checked by the API.  
**Chatwoot equivalent:** Chatwoot `CustomRole::PERMISSIONS` constant.  
**References:** —

```sql
CREATE TABLE permissions (
    code        varchar(64) PRIMARY KEY,
    description text NOT NULL
);
```

### `subscription_plans`

**Purpose:** Platform-defined plans with limits and feature list.  
**Chatwoot equivalent:** Chatwoot Enterprise plan config.  
**References:** —

```sql
CREATE TABLE subscription_plans (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code                varchar(64) NOT NULL,
    name                varchar(255) NOT NULL,
    billing_interval    varchar(16) NOT NULL,
    price_cents         integer NOT NULL,
    currency            char(3) NOT NULL DEFAULT 'USD',
    limits              jsonb NOT NULL DEFAULT '{}'::jsonb,
    features            text[] NOT NULL DEFAULT '{}',
    external_price_id   varchar(255),
    is_public           boolean NOT NULL DEFAULT true,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT subscription_plans_code_uniq UNIQUE (code),
    CONSTRAINT subscription_plans_interval_chk CHECK (billing_interval IN ('month', 'year')),
    CONSTRAINT subscription_plans_price_chk CHECK (price_cents >= 0),
    CONSTRAINT subscription_plans_limits_is_object CHECK (jsonb_typeof(limits) = 'object')
);
```

## 2. Tenancy: organizations, memberships, roles, workspaces

### `organizations`

**Purpose:** The tenant. RLS key for everything below it.  
**Chatwoot equivalent:** Chatwoot `accounts`.  
**References:** `users`

```sql
CREATE TABLE organizations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                varchar(255) NOT NULL,
    slug                varchar(64) NOT NULL,
    status              org_status NOT NULL DEFAULT 'active',
    locale              varchar(16) NOT NULL DEFAULT 'en',
    timezone            varchar(64) NOT NULL DEFAULT 'UTC',
    feature_flags       text[] NOT NULL DEFAULT '{}',
    settings            jsonb NOT NULL DEFAULT '{}'::jsonb,
    limits_override     jsonb NOT NULL DEFAULT '{}'::jsonb,
    external_customer_id varchar(255),
    deletion_scheduled_at timestamptz,  -- set while status = pending_deletion (docs/12 §5.1)
    created_by          uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    deleted_at          timestamptz,
    CONSTRAINT organizations_settings_is_object CHECK (jsonb_typeof(settings) = 'object')
);
CREATE UNIQUE INDEX organizations_slug_uniq ON organizations (slug) WHERE deleted_at IS NULL;
CREATE INDEX organizations_status_idx ON organizations (status);
```

### `roles`

**Purpose:** Custom roles defined by an organization.  
**Chatwoot equivalent:** Chatwoot Enterprise `custom_roles`.  
**References:** `organizations`

```sql
CREATE TABLE roles (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            varchar(100) NOT NULL,
    description     text,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT roles_name_per_org_uniq UNIQUE (organization_id, name),
    CONSTRAINT roles_org_id_uniq UNIQUE (organization_id, id)
);
```

### `role_permissions`

**Purpose:** Permissions granted by a custom role.  
**Chatwoot equivalent:** Chatwoot `custom_roles.permissions` array.  
**References:** `permissions`, `roles`

```sql
CREATE TABLE role_permissions (
    organization_id uuid NOT NULL,
    role_id         uuid NOT NULL,
    permission_code varchar(64) NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_code),
    FOREIGN KEY (organization_id, role_id) REFERENCES roles(organization_id, id) ON DELETE CASCADE
);
```

### `organization_memberships`

**Purpose:** User ↔ organization with base role, optional custom role, availability.  
**Chatwoot equivalent:** Chatwoot `account_users`.  
**References:** `organizations`, `roles`, `users`

```sql
CREATE TABLE organization_memberships (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            membership_role NOT NULL DEFAULT 'agent',
    custom_role_id  uuid,
    availability    availability_status NOT NULL DEFAULT 'offline',
    auto_offline    boolean NOT NULL DEFAULT true,
    accepts_conversations boolean NOT NULL DEFAULT true,
    invited_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    last_active_at  timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT org_memberships_user_uniq UNIQUE (organization_id, user_id),
    CONSTRAINT org_memberships_org_id_uniq UNIQUE (organization_id, id),
    CONSTRAINT org_memberships_custom_role_agent_chk CHECK (custom_role_id IS NULL OR role = 'agent'),
    FOREIGN KEY (organization_id, custom_role_id) REFERENCES roles(organization_id, id) ON DELETE SET NULL (custom_role_id)
);
CREATE INDEX org_memberships_user_idx ON organization_memberships (user_id);
CREATE UNIQUE INDEX org_memberships_one_owner_uniq ON organization_memberships (organization_id) WHERE role = 'owner';
```

### `invitations`

**Purpose:** Pending invitations, stored as a token hash.  
**Chatwoot equivalent:** Chatwoot creates the user immediately and emails a confirmation link.  
**References:** `organizations`, `roles`, `users`

```sql
CREATE TABLE invitations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           citext NOT NULL,
    role            membership_role NOT NULL DEFAULT 'agent',
    custom_role_id  uuid,
    token_hash      bytea NOT NULL,
    invited_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    expires_at      timestamptz NOT NULL,
    accepted_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    FOREIGN KEY (organization_id, custom_role_id) REFERENCES roles(organization_id, id) ON DELETE SET NULL (custom_role_id)
);
CREATE UNIQUE INDEX invitations_token_uniq ON invitations (token_hash);
CREATE UNIQUE INDEX invitations_open_email_uniq ON invitations (organization_id, email) WHERE accepted_at IS NULL;
```

### `api_access_tokens`

**Purpose:** Hashed API tokens with scopes, owned by an organization (and optionally a user).  
**Chatwoot equivalent:** Chatwoot `access_tokens` (plaintext).  
**References:** `organizations`, `users`

```sql
CREATE TABLE api_access_tokens (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id         uuid REFERENCES users(id) ON DELETE CASCADE,
    name            varchar(100) NOT NULL,
    token_prefix    varchar(12) NOT NULL,
    token_hash      bytea NOT NULL,
    scopes          text[] NOT NULL DEFAULT '{}',
    expires_at      timestamptz,
    last_used_at    timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX api_access_tokens_hash_uniq ON api_access_tokens (token_hash);
CREATE INDEX api_access_tokens_org_idx ON api_access_tokens (organization_id);
```

### `workspaces`

**Purpose:** Operational unit inside an organization: owns inboxes, contacts, conversations.  
**Chatwoot equivalent:** New in Converse.  
**References:** `organizations`

```sql
CREATE TABLE workspaces (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            varchar(255) NOT NULL,
    slug            varchar(64) NOT NULL,
    timezone        varchar(64) NOT NULL DEFAULT 'UTC',
    auto_resolve_after_minutes integer,
    settings        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT workspaces_org_id_uniq UNIQUE (organization_id, id),
    CONSTRAINT workspaces_auto_resolve_chk CHECK (auto_resolve_after_minutes IS NULL OR auto_resolve_after_minutes BETWEEN 10 AND 1439856)
);
CREATE UNIQUE INDEX workspaces_slug_per_org_uniq ON workspaces (organization_id, slug) WHERE deleted_at IS NULL;
```

### `workspace_counters`

**Purpose:** Per-workspace counters such as conversation display numbers.  
**Chatwoot equivalent:** Replaces Chatwoot's per-account sequences + triggers.  
**References:** `workspaces`

```sql
CREATE TABLE workspace_counters (
    workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    counter_name    varchar(32) NOT NULL,
    current_value   bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, counter_name)
);
```

### `workspace_memberships`

**Purpose:** Which organization members can work in which workspace.  
**Chatwoot equivalent:** New.  
**References:** `organization_memberships`, `workspaces`

```sql
CREATE TABLE workspace_memberships (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    membership_id   uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, membership_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, membership_id) REFERENCES organization_memberships(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX workspace_memberships_member_idx ON workspace_memberships (membership_id);
```

## 3. Teams

### `teams`

**Purpose:** Groups of agents for routing and visibility.  
**Chatwoot equivalent:** Chatwoot `teams`.  
**References:** `workspaces`

```sql
CREATE TABLE teams (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(100) NOT NULL,
    description     text,
    allow_auto_assign boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT teams_ws_id_uniq UNIQUE (workspace_id, id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX teams_name_per_ws_uniq ON teams (workspace_id, lower(name)) WHERE deleted_at IS NULL;
CREATE INDEX teams_org_idx ON teams (organization_id);
```

### `team_members`

**Purpose:** Users in a team.  
**Chatwoot equivalent:** Chatwoot `team_members`.  
**References:** `teams`, `users`, `workspaces`

```sql
CREATE TABLE team_members (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    team_id         uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, user_id),
    FOREIGN KEY (workspace_id, team_id) REFERENCES teams(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX team_members_user_idx ON team_members (user_id, workspace_id);
```

## 4. Inboxes & channel connections

### `inboxes`

**Purpose:** One connected channel instance and its behaviour settings.  
**Chatwoot equivalent:** Chatwoot `inboxes`.  
**References:** `workspaces`

```sql
CREATE TABLE inboxes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(255) NOT NULL,
    provider        channel_provider NOT NULL,
    enable_auto_assignment boolean NOT NULL DEFAULT true,
    auto_assignment_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    greeting_enabled boolean NOT NULL DEFAULT false,
    greeting_message text,
    working_hours_enabled boolean NOT NULL DEFAULT false,
    working_hours   jsonb NOT NULL DEFAULT '[]'::jsonb,
    out_of_office_message text,
    timezone        varchar(64) NOT NULL DEFAULT 'UTC',
    allow_messages_after_resolved boolean NOT NULL DEFAULT true,
    lock_to_single_conversation boolean NOT NULL DEFAULT false,
    settings        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT inboxes_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT inboxes_working_hours_is_array CHECK (jsonb_typeof(working_hours) = 'array'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX inboxes_ws_idx ON inboxes (workspace_id) WHERE deleted_at IS NULL;
CREATE INDEX inboxes_org_idx ON inboxes (organization_id);
```

### `inbox_members`

**Purpose:** Agents allowed to work an inbox.  
**Chatwoot equivalent:** Chatwoot `inbox_members`.  
**References:** `inboxes`, `users`, `workspaces`

```sql
CREATE TABLE inbox_members (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    inbox_id        uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (inbox_id, user_id),
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX inbox_members_user_idx ON inbox_members (user_id, workspace_id);
```

### `channel_connections`

**Purpose:** Provider-agnostic connection state and encrypted credentials, 1:1 with an inbox.  
**Chatwoot equivalent:** Chatwoot polymorphic `channel_*` rows.  
**References:** `inboxes`, `users`, `workspaces`

```sql
CREATE TABLE channel_connections (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    inbox_id            uuid NOT NULL,
    provider            channel_provider NOT NULL,
    status              connection_status NOT NULL DEFAULT 'pending',
    credentials_ciphertext bytea,
    credentials_key_version smallint,
    token_expires_at    timestamptz,
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error          text,
    last_error_at       timestamptz,
    connected_by        uuid REFERENCES users(id) ON DELETE SET NULL,
    connected_at        timestamptz,
    disconnected_at     timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT channel_connections_inbox_uniq UNIQUE (inbox_id),
    CONSTRAINT channel_connections_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT channel_connections_secret_key_chk CHECK (credentials_ciphertext IS NULL OR credentials_key_version IS NOT NULL),
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX channel_connections_token_expiry_idx ON channel_connections (token_expires_at) WHERE status = 'active';
```

### `channel_routes`

**Purpose:** Global map `(provider, external_id)` → tenant/inbox for webhook ingress before the tenant is known. Not under RLS; not readable by the app role.  
**Chatwoot equivalent:** Chatwoot finds channels by URL param or payload ids per controller.  
**References:** `channel_connections`, `organizations`, `workspaces`

```sql
CREATE TABLE channel_routes (
    provider            channel_provider NOT NULL,
    external_id         varchar(255) NOT NULL,
    organization_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id        uuid NOT NULL,
    inbox_id            uuid NOT NULL,
    channel_connection_id uuid NOT NULL REFERENCES channel_connections(id) ON DELETE CASCADE,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, external_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX channel_routes_connection_idx ON channel_routes (channel_connection_id);
```

### `facebook_pages`

**Purpose:** Messenger-specific details of a connected Facebook Page.  
**Chatwoot equivalent:** Chatwoot `channel_facebook_pages`.  
**References:** `channel_connections`, `workspaces`

```sql
CREATE TABLE facebook_pages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    channel_connection_id uuid NOT NULL,
    page_id             varchar(64) NOT NULL,
    page_name           varchar(255),
    linked_instagram_id varchar(64),
    subscribed_fields   text[] NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT facebook_pages_connection_uniq UNIQUE (channel_connection_id),
    CONSTRAINT facebook_pages_page_per_org_uniq UNIQUE (organization_id, page_id),
    FOREIGN KEY (workspace_id, channel_connection_id) REFERENCES channel_connections(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX facebook_pages_page_idx ON facebook_pages (page_id);
```

### `instagram_accounts`

**Purpose:** Instagram business account details.  
**Chatwoot equivalent:** Chatwoot `channel_instagram`.  
**References:** `channel_connections`, `workspaces`

```sql
CREATE TABLE instagram_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    channel_connection_id uuid NOT NULL,
    instagram_user_id   varchar(64) NOT NULL,
    username            varchar(255),
    connection_method   varchar(32) NOT NULL,
    facebook_page_id    varchar(64),
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT instagram_accounts_connection_uniq UNIQUE (channel_connection_id),
    CONSTRAINT instagram_accounts_ig_uniq UNIQUE (instagram_user_id),
    CONSTRAINT instagram_accounts_method_chk CHECK (connection_method IN ('instagram_login', 'facebook_page')),
    FOREIGN KEY (workspace_id, channel_connection_id) REFERENCES channel_connections(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

### `whatsapp_business_accounts`

**Purpose:** WABA: owns phone numbers and templates; may hold its own system-user token.  
**Chatwoot equivalent:** Chatwoot keeps WABA data in `channel_whatsapp.provider_config`.  
**References:** `organizations`

```sql
CREATE TABLE whatsapp_business_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    waba_id             varchar(64) NOT NULL,
    business_id         varchar(64),
    name                varchar(255),
    credentials_ciphertext bytea,
    credentials_key_version smallint,
    templates_synced_at timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT whatsapp_waba_per_org_uniq UNIQUE (organization_id, waba_id),
    CONSTRAINT whatsapp_waba_org_id_uniq UNIQUE (organization_id, id)
);
```

### `whatsapp_phone_numbers`

**Purpose:** A WhatsApp phone number connected as an inbox.  
**Chatwoot equivalent:** Chatwoot `channel_whatsapp`.  
**References:** `channel_connections`, `whatsapp_business_accounts`, `workspaces`

```sql
CREATE TABLE whatsapp_phone_numbers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    channel_connection_id uuid NOT NULL,
    waba_id             uuid NOT NULL,
    phone_number_id     varchar(64) NOT NULL,
    display_phone_number varchar(32) NOT NULL,
    verified_name       varchar(255),
    quality_rating      varchar(16),
    messaging_limit_tier varchar(32),
    health              jsonb NOT NULL DEFAULT '{}'::jsonb,
    health_checked_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT whatsapp_phone_connection_uniq UNIQUE (channel_connection_id),
    CONSTRAINT whatsapp_phone_number_id_uniq UNIQUE (phone_number_id),
    FOREIGN KEY (workspace_id, channel_connection_id) REFERENCES channel_connections(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, waba_id) REFERENCES whatsapp_business_accounts(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX whatsapp_phone_waba_idx ON whatsapp_phone_numbers (waba_id);
```

### `whatsapp_message_templates`

**Purpose:** Approved/pending templates synced from Meta.  
**Chatwoot equivalent:** Chatwoot `channel_whatsapp.message_templates` jsonb.  
**References:** `whatsapp_business_accounts`

```sql
CREATE TABLE whatsapp_message_templates (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    waba_id             uuid NOT NULL,
    external_template_id varchar(64) NOT NULL,
    name                varchar(512) NOT NULL,
    language            varchar(16) NOT NULL,
    category            varchar(32) NOT NULL,
    status              varchar(32) NOT NULL,
    components          jsonb NOT NULL DEFAULT '[]'::jsonb,
    parameter_format    varchar(16) NOT NULL DEFAULT 'positional',
    synced_at           timestamptz NOT NULL DEFAULT NOW(),
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT whatsapp_templates_uniq UNIQUE (waba_id, name, language),
    CONSTRAINT whatsapp_templates_components_is_array CHECK (jsonb_typeof(components) = 'array'),
    FOREIGN KEY (organization_id, waba_id) REFERENCES whatsapp_business_accounts(organization_id, id) ON DELETE CASCADE
);
```

### `web_widgets`

**Purpose:** Website widget configuration and identity-verification secret.  
**Chatwoot equivalent:** Chatwoot `channel_web_widgets`.  
**References:** `channel_connections`, `workspaces`

```sql
CREATE TABLE web_widgets (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    channel_connection_id uuid NOT NULL,
    website_token       varchar(64) NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
    website_url         text,
    allowed_origins     text[] NOT NULL DEFAULT '{}',
    widget_color        varchar(9) NOT NULL DEFAULT '#1f93ff',
    welcome_title       varchar(255),
    welcome_tagline     varchar(255),
    reply_time          varchar(32) NOT NULL DEFAULT 'in_a_few_minutes',
    pre_chat_form_enabled boolean NOT NULL DEFAULT false,
    pre_chat_form_options jsonb NOT NULL DEFAULT '{}'::jsonb,
    identity_hmac_secret_ciphertext bytea,
    identity_hmac_key_version smallint,
    identity_verification_required boolean NOT NULL DEFAULT false,
    feature_flags       text[] NOT NULL DEFAULT ARRAY['attachments', 'emoji_picker', 'end_conversation'],
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT web_widgets_connection_uniq UNIQUE (channel_connection_id),
    CONSTRAINT web_widgets_token_uniq UNIQUE (website_token),
    FOREIGN KEY (workspace_id, channel_connection_id) REFERENCES channel_connections(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

## 5. Files, contacts, conversations, messages

### `stored_files`

**Purpose:** Metadata for every stored binary (attachments, avatars, KB files).  
**Chatwoot equivalent:** ActiveStorage `active_storage_blobs`.  
**References:** `organizations`

```sql
CREATE TABLE stored_files (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    storage_backend varchar(16) NOT NULL,
    storage_key     text NOT NULL,
    filename        varchar(512) NOT NULL,
    content_type    varchar(255) NOT NULL,
    byte_size       bigint NOT NULL,
    checksum_sha256 char(64) NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    scan_status     varchar(16) NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT stored_files_key_uniq UNIQUE (storage_backend, storage_key),
    CONSTRAINT stored_files_org_id_uniq UNIQUE (organization_id, id),
    CONSTRAINT stored_files_size_chk CHECK (byte_size >= 0),
    CONSTRAINT stored_files_backend_chk CHECK (storage_backend IN ('local', 's3')),
    CONSTRAINT stored_files_scan_chk CHECK (scan_status IN ('pending', 'clean', 'infected', 'skipped'))
);
CREATE INDEX stored_files_org_created_idx ON stored_files (organization_id, created_at);
```

### `contacts`

**Purpose:** A customer within a workspace.  
**Chatwoot equivalent:** Chatwoot `contacts`.  
**References:** `stored_files`, `workspaces`

```sql
CREATE TABLE contacts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(255) NOT NULL DEFAULT '',
    email           citext,
    phone_number    varchar(32),
    identifier      varchar(255),
    avatar_file_id  uuid,
    contact_type    varchar(16) NOT NULL DEFAULT 'visitor',
    blocked         boolean NOT NULL DEFAULT false,
    country_code    varchar(2),
    location        varchar(255),
    additional_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    custom_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_activity_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT contacts_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT contacts_type_chk CHECK (contact_type IN ('visitor', 'lead', 'customer')),
    CONSTRAINT contacts_custom_attrs_is_object CHECK (jsonb_typeof(custom_attributes) = 'object'),
    CONSTRAINT contacts_additional_attrs_is_object CHECK (jsonb_typeof(additional_attributes) = 'object'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, avatar_file_id) REFERENCES stored_files(organization_id, id) ON DELETE SET NULL (avatar_file_id)
);
CREATE UNIQUE INDEX contacts_email_per_ws_uniq ON contacts (workspace_id, email) WHERE email IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX contacts_identifier_per_ws_uniq ON contacts (workspace_id, identifier) WHERE identifier IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX contacts_phone_idx ON contacts (workspace_id, phone_number) WHERE phone_number IS NOT NULL;
CREATE INDEX contacts_activity_idx ON contacts (workspace_id, last_activity_at DESC NULLS LAST) WHERE deleted_at IS NULL;
CREATE INDEX contacts_search_trgm_idx ON contacts USING gin ((coalesce(name, '') || ' ' || coalesce(email::text, '') || ' ' || coalesce(phone_number, '')) gin_trgm_ops);
CREATE INDEX contacts_org_idx ON contacts (organization_id);
```

### `contact_identities`

**Purpose:** A contact's identity on one inbox (`source_id`) plus their realtime token.  
**Chatwoot equivalent:** Chatwoot `contact_inboxes`.  
**References:** `contacts`, `inboxes`, `workspaces`

```sql
CREATE TABLE contact_identities (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    contact_id      uuid NOT NULL,
    inbox_id        uuid NOT NULL,
    source_id       text NOT NULL,
    realtime_token  varchar(64) NOT NULL DEFAULT encode(gen_random_bytes(24), 'hex'),
    identity_verified boolean NOT NULL DEFAULT false,
    profile         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT contact_identities_source_uniq UNIQUE (inbox_id, source_id),
    CONSTRAINT contact_identities_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT contact_identities_token_uniq UNIQUE (realtime_token),
    FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX contact_identities_contact_idx ON contact_identities (contact_id);
```

### `ai_agents`

**Purpose:** Tenant-configured AI agent: provider credential, models, prompt, tools, handoff policy.  
**Chatwoot equivalent:** Chatwoot Enterprise `captain_assistants`.  
**References:** `workspaces`

```sql
CREATE TABLE ai_agents (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    name                varchar(255) NOT NULL,
    description         text,
    provider_credential_id uuid,
    chat_model          varchar(128) NOT NULL,
    classifier_model    varchar(128),
    embedding_model     varchar(128) NOT NULL DEFAULT 'text-embedding-3-small',
    temperature         numeric(3,2) NOT NULL DEFAULT 0.2,
    system_prompt       text NOT NULL DEFAULT '',
    persona             jsonb NOT NULL DEFAULT '{}'::jsonb,
    guardrails          jsonb NOT NULL DEFAULT '{}'::jsonb,
    handoff_policy      jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled_tools       text[] NOT NULL DEFAULT ARRAY['knowledge_search'],
    max_turns_before_handoff integer NOT NULL DEFAULT 10,
    allowed_actions     text[] NOT NULL DEFAULT ARRAY['reply', 'handoff'],
    tool_settings       jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    deleted_at          timestamptz,
    CONSTRAINT ai_agents_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT ai_agents_temperature_chk CHECK (temperature BETWEEN 0 AND 2),
    CONSTRAINT ai_agents_actions_chk CHECK (allowed_actions <@ ARRAY['reply', 'handoff', 'add_label', 'set_priority', 'resolve', 'snooze', 'update_contact', 'add_private_note']::text[] AND 'handoff' = ANY(allowed_actions)),
    CONSTRAINT ai_agents_tool_settings_is_object CHECK (jsonb_typeof(tool_settings) = 'object'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
ALTER TABLE ai_agents
    ADD CONSTRAINT ai_agents_credential_fk FOREIGN KEY (organization_id, provider_credential_id)
    REFERENCES ai_provider_credentials(organization_id, id) ON DELETE SET NULL (provider_credential_id);
```

### `conversations`

**Purpose:** A thread between one contact identity and an inbox.  
**Chatwoot equivalent:** Chatwoot `conversations`.  
**References:** `ai_agents`, `contact_identities`, `contacts`, `inboxes`, `teams`, `users`, `workspaces`

```sql
CREATE TABLE conversations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    inbox_id            uuid NOT NULL,
    contact_id          uuid NOT NULL,
    contact_identity_id uuid NOT NULL,
    display_number      bigint NOT NULL,
    status              conversation_status NOT NULL DEFAULT 'open',
    priority            conversation_priority,
    assignee_id         uuid REFERENCES users(id) ON DELETE SET NULL,
    team_id             uuid,
    ai_agent_id         uuid,
    ai_active           boolean NOT NULL DEFAULT false,
    snoozed_until       timestamptz,
    waiting_since       timestamptz,
    first_reply_at      timestamptz,
    last_activity_at    timestamptz NOT NULL DEFAULT NOW(),
    last_incoming_at    timestamptz,
    contact_last_seen_at timestamptz,
    agent_last_seen_at  timestamptz,
    status_changed_at   timestamptz NOT NULL DEFAULT NOW(),
    external_thread_id  varchar(255),
    additional_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    custom_attributes   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT conversations_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT conversations_display_uniq UNIQUE (workspace_id, display_number),
    CONSTRAINT conversations_snooze_chk CHECK (status <> 'snoozed' OR snoozed_until IS NOT NULL),
    CONSTRAINT conversations_custom_attrs_is_object CHECK (jsonb_typeof(custom_attributes) = 'object'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, contact_identity_id) REFERENCES contact_identities(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, team_id) REFERENCES teams(workspace_id, id) ON DELETE SET NULL (team_id),
    FOREIGN KEY (workspace_id, ai_agent_id) REFERENCES ai_agents(workspace_id, id) ON DELETE SET NULL (ai_agent_id)
);
CREATE INDEX conversations_list_idx ON conversations (workspace_id, status, last_activity_at DESC);
CREATE INDEX conversations_inbox_status_idx ON conversations (workspace_id, inbox_id, status, assignee_id);
CREATE INDEX conversations_assignee_idx ON conversations (workspace_id, assignee_id, status) WHERE assignee_id IS NOT NULL;
CREATE INDEX conversations_team_idx ON conversations (workspace_id, team_id, status) WHERE team_id IS NOT NULL;
CREATE INDEX conversations_contact_idx ON conversations (contact_id, created_at DESC);
CREATE INDEX conversations_identity_open_idx ON conversations (contact_identity_id) WHERE status <> 'resolved';
CREATE INDEX conversations_snoozed_idx ON conversations (snoozed_until) WHERE status = 'snoozed';
CREATE INDEX conversations_waiting_idx ON conversations (workspace_id, waiting_since) WHERE waiting_since IS NOT NULL;
CREATE INDEX conversations_org_idx ON conversations (organization_id);
```

### `messages`

**Purpose:** Every incoming/outgoing message, private note and activity entry.  
**Chatwoot equivalent:** Chatwoot `messages`.  
**References:** `ai_agents`, `contacts`, `conversations`, `inboxes`, `users`, `workspaces`

```sql
CREATE TABLE messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL,
    workspace_id        uuid NOT NULL,
    inbox_id            uuid NOT NULL,
    conversation_id     uuid NOT NULL,
    direction           message_direction NOT NULL,
    content_type        message_content_type NOT NULL DEFAULT 'text',
    content             text,
    content_attributes  jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_private          boolean NOT NULL DEFAULT false,
    sender_kind         sender_kind NOT NULL,
    sender_user_id      uuid REFERENCES users(id) ON DELETE SET NULL,
    sender_contact_id   uuid,
    sender_ai_agent_id  uuid,
    delivery_status     delivery_status NOT NULL DEFAULT 'sent',
    delivery_error      text,
    external_id         varchar(255),
    in_reply_to_id      uuid,
    client_message_id   varchar(64),
    sequence_number     bigint GENERATED ALWAYS AS IDENTITY,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    deleted_at          timestamptz,
    CONSTRAINT messages_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT messages_content_len_chk CHECK (content IS NULL OR char_length(content) <= 150000),
    CONSTRAINT messages_private_outgoing_chk CHECK (NOT is_private OR direction = 'outgoing'),
    CONSTRAINT messages_sender_chk CHECK (
        (sender_kind = 'user'     AND sender_user_id IS NOT NULL) OR
        (sender_kind = 'contact'  AND sender_contact_id IS NOT NULL) OR
        (sender_kind = 'ai_agent' AND sender_ai_agent_id IS NOT NULL) OR
        (sender_kind IN ('system', 'automation'))
    ),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, sender_contact_id) REFERENCES contacts(workspace_id, id) ON DELETE SET NULL (sender_contact_id),
    FOREIGN KEY (workspace_id, sender_ai_agent_id) REFERENCES ai_agents(workspace_id, id) ON DELETE SET NULL (sender_ai_agent_id),
    FOREIGN KEY (workspace_id, in_reply_to_id) REFERENCES messages(workspace_id, id) ON DELETE SET NULL (in_reply_to_id)
);
CREATE UNIQUE INDEX messages_external_id_uniq ON messages (inbox_id, external_id) WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX messages_client_id_uniq ON messages (conversation_id, client_message_id) WHERE client_message_id IS NOT NULL;
CREATE INDEX messages_conversation_seq_idx ON messages (conversation_id, sequence_number);
CREATE INDEX messages_ws_created_idx ON messages (workspace_id, created_at);
CREATE INDEX messages_ws_direction_created_idx ON messages (workspace_id, direction, created_at);
CREATE INDEX messages_content_trgm_idx ON messages USING gin (content gin_trgm_ops) WHERE is_private = false;
CREATE INDEX messages_org_idx ON messages (organization_id);
```

### `attachments`

**Purpose:** Media/file/location attached to a message.  
**Chatwoot equivalent:** Chatwoot `attachments` + ActiveStorage.  
**References:** `messages`, `stored_files`, `workspaces`

```sql
CREATE TABLE attachments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    message_id      uuid NOT NULL,
    file_id         uuid,
    kind            attachment_kind NOT NULL,
    external_url    text,
    fallback_title  varchar(512),
    latitude        double precision,
    longitude       double precision,
    meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT attachments_source_chk CHECK (file_id IS NOT NULL OR external_url IS NOT NULL OR kind IN ('location', 'fallback')),
    FOREIGN KEY (workspace_id, message_id) REFERENCES messages(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, file_id) REFERENCES stored_files(organization_id, id) ON DELETE SET NULL (file_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX attachments_message_idx ON attachments (message_id);
CREATE INDEX attachments_ws_idx ON attachments (workspace_id, created_at);
```

### `conversation_assignments`

**Purpose:** History of assignee/team changes with reason.  
**Chatwoot equivalent:** Chatwoot has only the current assignee plus activity messages.  
**References:** `conversations`, `teams`, `users`, `workspaces`

```sql
CREATE TABLE conversation_assignments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    assignee_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    team_id         uuid,
    assigned_by_kind sender_kind NOT NULL,
    assigned_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    reason          varchar(32) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    ended_at        timestamptz,
    CONSTRAINT conversation_assignments_reason_chk CHECK (reason IN ('manual', 'auto_round_robin', 'auto_capacity', 'automation', 'ai_handoff', 'unassigned')),
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, team_id) REFERENCES teams(workspace_id, id) ON DELETE SET NULL (team_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX conversation_assignments_conv_idx ON conversation_assignments (conversation_id, created_at DESC);
CREATE INDEX conversation_assignments_assignee_idx ON conversation_assignments (workspace_id, assignee_id, created_at);
```

### `conversation_participants`

**Purpose:** Users watching a conversation (notified on updates).  
**Chatwoot equivalent:** Chatwoot `conversation_participants`.  
**References:** `conversations`, `users`, `workspaces`

```sql
CREATE TABLE conversation_participants (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (conversation_id, user_id),
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX conversation_participants_user_idx ON conversation_participants (user_id, workspace_id);
```

### `mentions`

**Purpose:** @mentions of users in private notes.  
**Chatwoot equivalent:** Chatwoot `mentions`.  
**References:** `conversations`, `messages`, `users`, `workspaces`

```sql
CREATE TABLE mentions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    message_id      uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT mentions_uniq UNIQUE (message_id, user_id),
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, message_id) REFERENCES messages(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX mentions_user_idx ON mentions (user_id, workspace_id, created_at DESC);
```

## 6. Labels, custom attributes, canned responses, notes, macros

### `labels`

**Purpose:** Workspace label catalog.  
**Chatwoot equivalent:** Chatwoot `labels`.  
**References:** `workspaces`

```sql
CREATE TABLE labels (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    title           varchar(100) NOT NULL,
    description     text,
    color           varchar(9) NOT NULL DEFAULT '#1f93ff',
    show_on_sidebar boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT labels_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT labels_title_format_chk CHECK (title ~ '^[a-z0-9_-]+$'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX labels_title_per_ws_uniq ON labels (workspace_id, title);
```

### `conversation_labels`

**Purpose:** Labels applied to conversations.  
**Chatwoot equivalent:** Chatwoot `taggings`.  
**References:** `conversations`, `labels`, `workspaces`

```sql
CREATE TABLE conversation_labels (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    label_id        uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (conversation_id, label_id),
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, label_id) REFERENCES labels(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX conversation_labels_label_idx ON conversation_labels (label_id);
```

### `contact_labels`

**Purpose:** Labels applied to contacts.  
**Chatwoot equivalent:** Chatwoot `taggings`.  
**References:** `contacts`, `labels`, `workspaces`

```sql
CREATE TABLE contact_labels (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    contact_id      uuid NOT NULL,
    label_id        uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id, label_id),
    FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, label_id) REFERENCES labels(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX contact_labels_label_idx ON contact_labels (label_id);
```

### `custom_attribute_definitions`

**Purpose:** Schema for custom attributes on contacts or conversations.  
**Chatwoot equivalent:** Chatwoot `custom_attribute_definitions`.  
**References:** `workspaces`

```sql
CREATE TABLE custom_attribute_definitions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    scope           attribute_scope NOT NULL,
    attribute_key   varchar(64) NOT NULL,
    display_name    varchar(255) NOT NULL,
    description     text,
    value_type      attribute_value_type NOT NULL,
    list_values     text[] NOT NULL DEFAULT '{}',
    regex_pattern   text,
    is_required     boolean NOT NULL DEFAULT false,
    ai_visible      boolean NOT NULL DEFAULT false,
    position        integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT custom_attr_key_uniq UNIQUE (workspace_id, scope, attribute_key),
    CONSTRAINT custom_attr_key_format_chk CHECK (attribute_key ~ '^[a-z][a-z0-9_]*$'),
    CONSTRAINT custom_attr_list_chk CHECK (value_type <> 'list' OR cardinality(list_values) > 0),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

### `canned_responses`

**Purpose:** Saved replies with a short code.  
**Chatwoot equivalent:** Chatwoot `canned_responses`.  
**References:** `users`, `workspaces`

```sql
CREATE TABLE canned_responses (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    short_code      varchar(64) NOT NULL,
    content         text NOT NULL,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT canned_responses_content_len_chk CHECK (char_length(content) <= 10000),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX canned_responses_code_uniq ON canned_responses (workspace_id, lower(short_code)) WHERE deleted_at IS NULL;
CREATE INDEX canned_responses_search_idx ON canned_responses USING gin ((short_code || ' ' || content) gin_trgm_ops);
```

### `notes`

**Purpose:** Notes about a contact (not part of a conversation).  
**Chatwoot equivalent:** Chatwoot `notes`.  
**References:** `contacts`, `users`, `workspaces`

```sql
CREATE TABLE notes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    contact_id      uuid NOT NULL,
    author_id       uuid REFERENCES users(id) ON DELETE SET NULL,
    content         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    FOREIGN KEY (workspace_id, contact_id) REFERENCES contacts(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX notes_contact_idx ON notes (contact_id, created_at DESC) WHERE deleted_at IS NULL;
```

### `macros`

**Purpose:** Saved action lists an agent runs on a conversation.  
**Chatwoot equivalent:** Chatwoot `macros`.  
**References:** `users`, `workspaces`

```sql
CREATE TABLE macros (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(255) NOT NULL,
    visibility      varchar(16) NOT NULL DEFAULT 'personal',
    actions         jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT macros_visibility_chk CHECK (visibility IN ('personal', 'global')),
    CONSTRAINT macros_actions_is_array CHECK (jsonb_typeof(actions) = 'array'),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX macros_ws_idx ON macros (workspace_id);
```

### `notifications`

**Purpose:** In-app notifications per user.  
**Chatwoot equivalent:** Chatwoot `notifications`.  
**References:** `conversations`, `messages`, `users`, `workspaces`

```sql
CREATE TABLE notifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type varchar(64) NOT NULL,
    conversation_id uuid,
    message_id      uuid,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    read_at         timestamptz,
    snoozed_until   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, message_id) REFERENCES messages(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX notifications_user_feed_idx ON notifications (user_id, workspace_id, created_at DESC);
CREATE INDEX notifications_user_unread_idx ON notifications (user_id, workspace_id) WHERE read_at IS NULL;
```

## 7. Events, webhooks, idempotency

### `event_outbox`

**Purpose:** Transactional outbox of domain events, relayed to workers.  
**Chatwoot equivalent:** Chatwoot dispatches in `after_commit`; no table.  
**References:** `organizations`

```sql
CREATE TABLE event_outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id    uuid,
    event_name      varchar(64) NOT NULL,
    aggregate_type  varchar(32) NOT NULL,
    aggregate_id    uuid NOT NULL,
    payload         jsonb NOT NULL,
    state           outbox_state NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    available_at    timestamptz NOT NULL DEFAULT NOW(),
    dispatched_at   timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX event_outbox_pending_idx ON event_outbox (available_at, id) WHERE state = 'pending';
CREATE INDEX event_outbox_aggregate_idx ON event_outbox (aggregate_type, aggregate_id);
```

### `inbound_webhook_events`

**Purpose:** Raw provider webhooks with a unique dedup key and processing state (dead-letter = `dead`).  
**Chatwoot equivalent:** Chatwoot uses Redis locks + `source_id` checks; no table.  
**References:** `organizations`

```sql
CREATE TABLE inbound_webhook_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        channel_provider NOT NULL,
    dedup_key       varchar(255) NOT NULL,
    organization_id uuid REFERENCES organizations(id) ON DELETE CASCADE,
    inbox_id        uuid,
    event_type      varchar(64) NOT NULL,
    payload         jsonb NOT NULL,
    signature_valid boolean NOT NULL,
    state           inbound_event_state NOT NULL DEFAULT 'received',
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    received_at     timestamptz NOT NULL DEFAULT NOW(),
    processed_at    timestamptz,
    CONSTRAINT inbound_webhook_events_dedup_uniq UNIQUE (provider, dedup_key)
);
CREATE INDEX inbound_webhook_events_state_idx ON inbound_webhook_events (state, received_at) WHERE state IN ('received', 'failed');
CREATE INDEX inbound_webhook_events_org_idx ON inbound_webhook_events (organization_id, received_at DESC);
```

### `webhook_subscriptions`

**Purpose:** Customer-configured outgoing webhooks.  
**Chatwoot equivalent:** Chatwoot `webhooks`.  
**References:** `inboxes`, `workspaces`

```sql
CREATE TABLE webhook_subscriptions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    inbox_id        uuid,
    name            varchar(255),
    url             text NOT NULL,
    secret_ciphertext bytea NOT NULL,
    secret_key_version smallint NOT NULL,
    events          text[] NOT NULL DEFAULT ARRAY['conversation.created', 'conversation.updated', 'conversation.status_changed', 'message.created', 'message.updated', 'contact.created', 'contact.updated'],
    is_active       boolean NOT NULL DEFAULT true,
    consecutive_failures integer NOT NULL DEFAULT 0,
    disabled_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT webhook_subscriptions_url_uniq UNIQUE (workspace_id, url),
    CONSTRAINT webhook_subscriptions_https_chk CHECK (url ~ '^https://'),
    CONSTRAINT webhook_subscriptions_ws_id_uniq UNIQUE (workspace_id, id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE
);
```

### `webhook_deliveries`

**Purpose:** Delivery log with retry state per subscription and event.  
**Chatwoot equivalent:** Not in Chatwoot.  
**References:** `event_outbox`, `webhook_subscriptions`, `workspaces`

```sql
CREATE TABLE webhook_deliveries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    subscription_id uuid NOT NULL,
    outbox_event_id bigint,
    event_name      varchar(64) NOT NULL,
    payload         jsonb NOT NULL,
    state           webhook_delivery_state NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    response_status integer,
    response_body_excerpt varchar(2000),
    next_attempt_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    completed_at    timestamptz,
    CONSTRAINT webhook_deliveries_event_uniq UNIQUE (subscription_id, outbox_event_id),
    FOREIGN KEY (workspace_id, subscription_id) REFERENCES webhook_subscriptions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (outbox_event_id) REFERENCES event_outbox(id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX webhook_deliveries_retry_idx ON webhook_deliveries (next_attempt_at) WHERE state IN ('pending', 'failed');
CREATE INDEX webhook_deliveries_sub_idx ON webhook_deliveries (subscription_id, created_at DESC);
```

### `idempotency_keys`

**Purpose:** Stored responses for `Idempotency-Key` requests (24 h).  
**Chatwoot equivalent:** Not in Chatwoot.  
**References:** `organizations`

```sql
CREATE TABLE idempotency_keys (
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    idempotency_key varchar(255) NOT NULL,
    request_fingerprint char(64) NOT NULL,
    response_status integer,
    response_body   jsonb,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    expires_at      timestamptz NOT NULL,
    PRIMARY KEY (organization_id, idempotency_key)
);
CREATE INDEX idempotency_keys_expiry_idx ON idempotency_keys (expires_at);
```

## 8. Automation

### `automation_rules`

**Purpose:** Event-triggered rules with conditions and actions.  
**Chatwoot equivalent:** Chatwoot `automation_rules`.  
**References:** `users`, `workspaces`

```sql
CREATE TABLE automation_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(255) NOT NULL,
    description     text,
    event_name      varchar(64) NOT NULL,
    conditions      jsonb NOT NULL DEFAULT '[]'::jsonb,
    actions         jsonb NOT NULL DEFAULT '[]'::jsonb,
    is_active       boolean NOT NULL DEFAULT true,
    position        integer NOT NULL DEFAULT 0,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT automation_rules_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT automation_rules_event_chk CHECK (event_name IN ('conversation.created', 'conversation.updated', 'conversation.opened', 'conversation.resolved', 'message.created')),
    CONSTRAINT automation_rules_conditions_is_array CHECK (jsonb_typeof(conditions) = 'array'),
    CONSTRAINT automation_rules_actions_is_array CHECK (jsonb_typeof(actions) = 'array' AND jsonb_array_length(actions) > 0),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX automation_rules_lookup_idx ON automation_rules (workspace_id, event_name, position) WHERE is_active;
```

### `automation_rule_executions`

**Purpose:** Execution log; unique per (rule, outbox event) so a rule fires at most once per event.  
**Chatwoot equivalent:** Not in Chatwoot.  
**References:** `automation_rules`, `conversations`, `workspaces`

```sql
CREATE TABLE automation_rule_executions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    rule_id         uuid NOT NULL,
    conversation_id uuid,
    outbox_event_id bigint,
    matched         boolean NOT NULL,
    actions_result  jsonb NOT NULL DEFAULT '[]'::jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT automation_executions_once_uniq UNIQUE (rule_id, outbox_event_id),
    FOREIGN KEY (workspace_id, rule_id) REFERENCES automation_rules(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX automation_executions_rule_idx ON automation_rule_executions (rule_id, created_at DESC);
```

## 9. AI: providers, agents, knowledge bases, memory, runs

### `ai_provider_credentials`

**Purpose:** Tenant's own LLM API keys (bring-your-own-key).  
**Chatwoot equivalent:** Chatwoot uses installation-level keys.  
**References:** `organizations`, `users`

```sql
CREATE TABLE ai_provider_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider        varchar(32) NOT NULL,
    label           varchar(100) NOT NULL,
    base_url        text,
    api_key_ciphertext bytea NOT NULL,
    api_key_key_version smallint NOT NULL,
    api_key_last4   varchar(4) NOT NULL,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_provider_credentials_label_uniq UNIQUE (organization_id, label),
    CONSTRAINT ai_provider_credentials_org_id_uniq UNIQUE (organization_id, id),
    CONSTRAINT ai_provider_credentials_provider_chk CHECK (provider IN ('openai', 'anthropic', 'azure_openai', 'gemini', 'openai_compatible'))
);
```

### `ai_agent_inboxes`

**Purpose:** Which inbox an AI agent serves and in which mode (one agent per inbox).  
**Chatwoot equivalent:** Chatwoot `captain_inboxes`.  
**References:** `ai_agents`, `inboxes`, `workspaces`

```sql
CREATE TABLE ai_agent_inboxes (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    ai_agent_id     uuid NOT NULL,
    inbox_id        uuid NOT NULL,
    mode            varchar(16) NOT NULL DEFAULT 'autonomous',
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (inbox_id),
    CONSTRAINT ai_agent_inboxes_mode_chk CHECK (mode IN ('autonomous', 'assist_only')),
    FOREIGN KEY (workspace_id, ai_agent_id) REFERENCES ai_agents(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, inbox_id) REFERENCES inboxes(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX ai_agent_inboxes_agent_idx ON ai_agent_inboxes (ai_agent_id);
```

### `knowledge_bases`

**Purpose:** Collections of documents used for retrieval.  
**Chatwoot equivalent:** Chatwoot Captain assistant documents.  
**References:** `workspaces`

```sql
CREATE TABLE knowledge_bases (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    name            varchar(255) NOT NULL,
    description     text,
    embedding_model varchar(128) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimensions integer NOT NULL DEFAULT 1536,
    chunk_size_tokens integer NOT NULL DEFAULT 500,
    chunk_overlap_tokens integer NOT NULL DEFAULT 50,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT knowledge_bases_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT knowledge_bases_dims_chk CHECK (embedding_dimensions = 1536),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

### `ai_agent_knowledge_bases`

**Purpose:** Knowledge bases an AI agent may search.  
**Chatwoot equivalent:** —  
**References:** `ai_agents`, `knowledge_bases`, `workspaces`

```sql
CREATE TABLE ai_agent_knowledge_bases (
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    ai_agent_id     uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    PRIMARY KEY (ai_agent_id, knowledge_base_id),
    FOREIGN KEY (workspace_id, ai_agent_id) REFERENCES ai_agents(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, knowledge_base_id) REFERENCES knowledge_bases(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

### `kb_documents`

**Purpose:** A source document (URL, file, text, FAQ) and its ingestion status.  
**Chatwoot equivalent:** Chatwoot `captain_documents`.  
**References:** `knowledge_bases`, `stored_files`, `users`, `workspaces`

```sql
CREATE TABLE kb_documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    title           varchar(512) NOT NULL,
    source_type     varchar(16) NOT NULL,
    source_url      text,
    file_id         uuid,
    content_hash    char(64),
    status          kb_document_status NOT NULL DEFAULT 'pending',
    error           text,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_synced_at  timestamptz,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    deleted_at      timestamptz,
    CONSTRAINT kb_documents_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT kb_documents_source_chk CHECK (
        (source_type = 'url'  AND source_url IS NOT NULL) OR
        (source_type = 'file' AND file_id IS NOT NULL) OR
        (source_type IN ('text', 'faq'))
    ),
    FOREIGN KEY (workspace_id, knowledge_base_id) REFERENCES knowledge_bases(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, file_id) REFERENCES stored_files(organization_id, id) ON DELETE SET NULL (file_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX kb_documents_kb_idx ON kb_documents (knowledge_base_id, status) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX kb_documents_url_uniq ON kb_documents (knowledge_base_id, source_url) WHERE source_url IS NOT NULL AND deleted_at IS NULL;
```

### `kb_chunks`

**Purpose:** Embedded chunks with HNSW vector index and full-text vector for hybrid search.  
**Chatwoot equivalent:** Chatwoot `captain_assistant_responses` (FAQ-level embeddings).  
**References:** `kb_documents`, `knowledge_bases`, `workspaces`

```sql
CREATE TABLE kb_chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id     uuid NOT NULL,
    chunk_index     integer NOT NULL,
    content         text NOT NULL,
    token_count     integer NOT NULL,
    embedding       vector(1536) NOT NULL,
    embedding_model varchar(128) NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_vector   tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT kb_chunks_position_uniq UNIQUE (document_id, chunk_index),
    FOREIGN KEY (workspace_id, document_id) REFERENCES kb_documents(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, knowledge_base_id) REFERENCES knowledge_bases(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX kb_chunks_embedding_hnsw_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX kb_chunks_kb_idx ON kb_chunks (knowledge_base_id);
CREATE INDEX kb_chunks_fts_idx ON kb_chunks USING gin (search_vector);
```

### `ai_conversation_sessions`

**Purpose:** AI memory per conversation: summary, facts, LangGraph thread id, status.  
**Chatwoot equivalent:** —  
**References:** `ai_agents`, `conversations`, `workspaces`

```sql
CREATE TABLE ai_conversation_sessions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    ai_agent_id     uuid NOT NULL,
    thread_id       varchar(128) NOT NULL,
    summary         text NOT NULL DEFAULT '',
    summarized_through_seq bigint NOT NULL DEFAULT 0,
    facts           jsonb NOT NULL DEFAULT '{}'::jsonb,
    turn_count      integer NOT NULL DEFAULT 0,
    status          varchar(16) NOT NULL DEFAULT 'active',
    handed_off_at   timestamptz,
    customer_verification_level varchar(16) NOT NULL DEFAULT 'none',
    customer_verified_until timestamptz,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT ai_sessions_conversation_uniq UNIQUE (conversation_id, ai_agent_id),
    CONSTRAINT ai_sessions_thread_uniq UNIQUE (thread_id),
    CONSTRAINT ai_sessions_ws_id_uniq UNIQUE (workspace_id, id),
    CONSTRAINT ai_sessions_status_chk CHECK (status IN ('active', 'handed_off', 'closed')),
    CONSTRAINT ai_sessions_verification_chk CHECK (customer_verification_level IN ('none', 'channel', 'verified')),
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, ai_agent_id) REFERENCES ai_agents(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
```

### `ai_runs`

**Purpose:** One graph execution: outcome, tokens, latency, retrieved chunks, guardrail flags.  
**Chatwoot equivalent:** Chatwoot Captain stores traces differently.  
**References:** `ai_conversation_sessions`, `conversations`, `messages`, `workspaces`

```sql
CREATE TABLE ai_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    workspace_id    uuid NOT NULL,
    session_id      uuid NOT NULL,
    conversation_id uuid NOT NULL,
    trigger_message_id uuid,
    reply_message_id uuid,
    status          ai_run_status NOT NULL DEFAULT 'running',
    intent          varchar(64),
    route           varchar(64),
    handoff_reason  varchar(64),
    model           varchar(128) NOT NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    latency_ms      integer,
    retrieved_chunk_ids uuid[] NOT NULL DEFAULT '{}',
    guardrail_flags text[] NOT NULL DEFAULT '{}',
    error           text,
    started_at      timestamptz NOT NULL DEFAULT NOW(),
    finished_at     timestamptz,
    CONSTRAINT ai_runs_trigger_uniq UNIQUE (session_id, trigger_message_id),
    FOREIGN KEY (workspace_id, session_id) REFERENCES ai_conversation_sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, trigger_message_id) REFERENCES messages(workspace_id, id) ON DELETE SET NULL (trigger_message_id),
    FOREIGN KEY (workspace_id, reply_message_id) REFERENCES messages(workspace_id, id) ON DELETE SET NULL (reply_message_id),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX ai_runs_conversation_idx ON ai_runs (conversation_id, started_at DESC);
CREATE INDEX ai_runs_ws_started_idx ON ai_runs (workspace_id, started_at);
```

### `ai_tool_calls`

**Purpose:** Tool calls made during a run.  
**Chatwoot equivalent:** Chatwoot `copilot_tool_calls` (copilot only).  
**References:** `ai_runs`

```sql
CREATE TABLE ai_tool_calls (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    run_id          uuid NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
    tool_name       varchar(64) NOT NULL,
    arguments       jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_excerpt  text,
    policy_decision varchar(24) NOT NULL DEFAULT 'allowed',
    denial_reason   varchar(64),
    succeeded       boolean NOT NULL,
    latency_ms      integer,
    created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX ai_tool_calls_run_idx ON ai_tool_calls (run_id);
ALTER TABLE ai_tool_calls ADD CONSTRAINT ai_tool_calls_decision_chk
    CHECK (policy_decision IN ('allowed', 'denied', 'needs_verification', 'needs_approval'));
```

## 10. Reporting, billing, audit

### `reporting_events`

**Purpose:** Pre-computed metric events (first response, resolution, handoff).  
**Chatwoot equivalent:** Chatwoot `reporting_events`.  
**References:** `organizations`, `workspaces`

```sql
CREATE TABLE reporting_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id    uuid NOT NULL,
    name            varchar(64) NOT NULL,
    conversation_id uuid,
    inbox_id        uuid,
    user_id         uuid,
    team_id         uuid,
    value_seconds   double precision NOT NULL,
    value_in_business_hours_seconds double precision,
    event_start_at  timestamptz NOT NULL,
    event_end_at    timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT reporting_events_name_chk CHECK (name IN ('first_response', 'reply_time', 'conversation_resolved', 'conversation_opened', 'conversation_bot_handoff', 'conversation_bot_resolved')),
    FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX reporting_events_ws_name_time_idx ON reporting_events (workspace_id, name, created_at);
CREATE INDEX reporting_events_ws_inbox_idx ON reporting_events (workspace_id, inbox_id, created_at);
CREATE INDEX reporting_events_ws_user_idx ON reporting_events (workspace_id, user_id, created_at);
CREATE INDEX reporting_events_conversation_idx ON reporting_events (conversation_id);
```

### `subscriptions`

**Purpose:** An organization's plan subscription; at most one live per org.  
**Chatwoot equivalent:** Chatwoot Enterprise billing state on `accounts`.  
**References:** `organizations`, `subscription_plans`

```sql
CREATE TABLE subscriptions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    plan_id             uuid NOT NULL REFERENCES subscription_plans(id) ON DELETE RESTRICT,
    state               subscription_state NOT NULL,
    seats               integer NOT NULL DEFAULT 1,
    external_subscription_id varchar(255),
    trial_ends_at       timestamptz,
    current_period_start timestamptz NOT NULL,
    current_period_end  timestamptz NOT NULL,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    canceled_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT subscriptions_seats_chk CHECK (seats >= 1),
    CONSTRAINT subscriptions_period_chk CHECK (current_period_end > current_period_start)
);
CREATE UNIQUE INDEX subscriptions_one_live_per_org ON subscriptions (organization_id) WHERE state IN ('trialing', 'active', 'past_due', 'incomplete');
CREATE UNIQUE INDEX subscriptions_external_uniq ON subscriptions (external_subscription_id) WHERE external_subscription_id IS NOT NULL;
```

### `billing_records`

**Purpose:** Invoices mirrored from the billing provider.  
**Chatwoot equivalent:** —  
**References:** `organizations`, `subscriptions`

```sql
CREATE TABLE billing_records (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    subscription_id     uuid REFERENCES subscriptions(id) ON DELETE SET NULL,
    external_invoice_id varchar(255),
    state               billing_record_state NOT NULL,
    amount_due_cents    integer NOT NULL,
    amount_paid_cents   integer NOT NULL DEFAULT 0,
    currency            char(3) NOT NULL,
    period_start        timestamptz,
    period_end          timestamptz,
    hosted_invoice_url  text,
    line_items          jsonb NOT NULL DEFAULT '[]'::jsonb,
    issued_at           timestamptz,
    paid_at             timestamptz,
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT billing_records_amount_chk CHECK (amount_due_cents >= 0 AND amount_paid_cents >= 0)
);
CREATE UNIQUE INDEX billing_records_external_uniq ON billing_records (external_invoice_id) WHERE external_invoice_id IS NOT NULL;
CREATE INDEX billing_records_org_idx ON billing_records (organization_id, created_at DESC);
```

### `usage_records`

**Purpose:** Metered usage (AI responses, tokens) with a unique source id for idempotent counting.  
**Chatwoot equivalent:** Chatwoot Captain usage counters.  
**References:** `organizations`

```sql
CREATE TABLE usage_records (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    metric          varchar(32) NOT NULL,
    quantity        bigint NOT NULL,
    period_start    date NOT NULL,
    source_id       varchar(255) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT usage_records_metric_chk CHECK (metric IN ('ai_responses', 'ai_tokens', 'conversations', 'storage_bytes')),
    CONSTRAINT usage_records_source_uniq UNIQUE (organization_id, metric, source_id)
);
CREATE INDEX usage_records_period_idx ON usage_records (organization_id, metric, period_start);
```

### `billing_webhook_events`

**Purpose:** Stripe events by event id, for dedup.  
**Chatwoot equivalent:** —  
**References:** —

```sql
CREATE TABLE billing_webhook_events (
    id              varchar(255) PRIMARY KEY,
    event_type      varchar(128) NOT NULL,
    payload         jsonb NOT NULL,
    processed_at    timestamptz,
    received_at     timestamptz NOT NULL DEFAULT NOW()
);
```

### `audit_logs`

**Purpose:** Append-only security and configuration audit trail.  
**Chatwoot equivalent:** Chatwoot `audits` (audited gem).  
**References:** `organizations`

```sql
CREATE TABLE audit_logs (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id    uuid,
    actor_kind      varchar(16) NOT NULL,
    actor_id        uuid,
    action          varchar(64) NOT NULL,
    resource_type   varchar(64) NOT NULL,
    resource_id     uuid,
    changes         jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip_address      inet,
    user_agent      text,
    request_id      varchar(64),
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT audit_logs_actor_chk CHECK (actor_kind IN ('user', 'platform_admin', 'api_token', 'system', 'ai_agent'))
);
CREATE INDEX audit_logs_org_time_idx ON audit_logs (organization_id, created_at DESC);
CREATE INDEX audit_logs_resource_idx ON audit_logs (resource_type, resource_id);
```

## 11. updated_at triggers

```sql
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables tb
          ON tb.table_name = c.table_name AND tb.table_schema = c.table_schema
        WHERE c.table_schema = current_schema()
          AND c.column_name = 'updated_at'
          AND tb.table_type = 'BASE TABLE'
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t || '_set_updated_at', t);
    END LOOP;
END;
$$;
```
