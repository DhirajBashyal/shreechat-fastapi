# 08 — Database Relationships

Entity-relationship diagrams by domain. The complete foreign-key list with delete behaviour (149 FKs, exported from the tested database) is in [database/RELATIONSHIPS.md](database/RELATIONSHIPS.md).

Notation: `||--o{` one-to-many, `||--||` one-to-one, `}o--o{` many-to-many (through a join table shown explicitly).

## 1. Tenancy and access

```mermaid
erDiagram
    users ||--o{ organization_memberships : "joins"
    users ||--o| platform_admins : "may be"
    users ||--o{ user_sessions : "has"
    organizations ||--o{ organization_memberships : "has"
    organizations ||--o{ roles : "defines"
    roles ||--o{ role_permissions : "grants"
    permissions ||--o{ role_permissions : "granted by"
    roles ||--o{ organization_memberships : "custom role of"
    organizations ||--o{ invitations : "sends"
    organizations ||--o{ api_access_tokens : "issues"
    organizations ||--o{ workspaces : "contains"
    workspaces ||--o{ workspace_memberships : "has"
    organization_memberships ||--o{ workspace_memberships : "works in"
    workspaces ||--o{ workspace_counters : "numbers"
    workspaces ||--o{ teams : "has"
    teams ||--o{ team_members : "has"
    users ||--o{ team_members : "is"

    organizations {
        uuid id PK
        varchar slug UK
        org_status status
        text_array feature_flags
    }
    organization_memberships {
        uuid id PK
        uuid organization_id FK
        uuid user_id FK
        membership_role role
        uuid custom_role_id FK
    }
    workspaces {
        uuid id PK
        uuid organization_id FK
        varchar slug
    }
```

**Rules**
- A user is global; a membership attaches them to one organization with one base role (`owner`, `administrator`, `agent`) and optionally one custom role.
- A member works in a workspace only if a `workspace_memberships` row exists. Owners and administrators implicitly have access to all workspaces (checked in code, so new workspaces don't need backfilled rows).
- Teams belong to one workspace.

## 2. Inboxes and channels

```mermaid
erDiagram
    workspaces ||--o{ inboxes : "has"
    inboxes ||--o{ inbox_members : "has"
    users ||--o{ inbox_members : "is"
    inboxes ||--|| channel_connections : "connected by"
    channel_connections ||--o{ channel_routes : "routed by"
    channel_connections ||--o| facebook_pages : "details"
    channel_connections ||--o| instagram_accounts : "details"
    channel_connections ||--o| whatsapp_phone_numbers : "details"
    channel_connections ||--o| web_widgets : "details"
    organizations ||--o{ whatsapp_business_accounts : "owns"
    whatsapp_business_accounts ||--o{ whatsapp_phone_numbers : "has"
    whatsapp_business_accounts ||--o{ whatsapp_message_templates : "has"

    channel_connections {
        uuid id PK
        uuid inbox_id FK,UK
        channel_provider provider
        connection_status status
        bytea credentials_ciphertext
        timestamptz token_expires_at
    }
    channel_routes {
        channel_provider provider PK
        varchar external_id PK
        uuid organization_id
        uuid inbox_id
    }
```

**Rules**
- Exactly one `channel_connections` row per inbox, and exactly one provider detail row per connection (the detail table matching `provider`; enforced in `InboxService`).
- `channel_routes.external_id` per provider: Facebook → Page id, Instagram → IG user id, WhatsApp → `phone_number_id`, web widget → `website_token`.
- A WABA can serve several phone numbers (each its own inbox); templates are per WABA.

## 3. Contacts, conversations, messages

```mermaid
erDiagram
    workspaces ||--o{ contacts : "has"
    contacts ||--o{ contact_identities : "has"
    inboxes ||--o{ contact_identities : "scopes"
    contact_identities ||--o{ conversations : "starts"
    contacts ||--o{ conversations : "participates"
    inboxes ||--o{ conversations : "receives"
    users ||--o{ conversations : "assigned"
    teams ||--o{ conversations : "assigned"
    ai_agents ||--o{ conversations : "handles"
    conversations ||--o{ messages : "contains"
    messages ||--o{ attachments : "has"
    stored_files ||--o{ attachments : "stores"
    messages ||--o{ messages : "replies to"
    conversations ||--o{ conversation_assignments : "history"
    conversations ||--o{ conversation_participants : "watched by"
    conversations ||--o{ mentions : "has"
    messages ||--o{ mentions : "contains"
    conversations ||--o{ conversation_labels : "tagged"
    labels ||--o{ conversation_labels : "applied"
    contacts ||--o{ contact_labels : "tagged"
    labels ||--o{ contact_labels : "applied"
    contacts ||--o{ notes : "has"

    contact_identities {
        uuid id PK
        uuid contact_id FK
        uuid inbox_id FK
        text source_id "UK with inbox_id"
        varchar realtime_token UK
    }
    conversations {
        uuid id PK
        bigint display_number "UK with workspace_id"
        conversation_status status
        uuid assignee_id FK
        uuid team_id FK
        boolean ai_active
        timestamptz last_activity_at
    }
    messages {
        uuid id PK
        uuid conversation_id FK
        message_direction direction
        boolean is_private
        sender_kind sender_kind
        varchar external_id "UK with inbox_id"
        bigint sequence_number
    }
```

**Rules**
- A contact can have many identities (one per inbox they've used). A conversation belongs to exactly one identity, therefore to one inbox and one contact.
- At most one **non-resolved** conversation per identity at a time (enforced by `ConversationResolver` under the per-identity ordering lock, not by a constraint, because `lock_to_single_conversation = false` inboxes may still create a new conversation after resolution).
- A message's sender is exactly one of user / contact / AI agent / system / automation (`messages_sender_chk`).

## 4. Automation, webhooks, events

```mermaid
erDiagram
    workspaces ||--o{ automation_rules : "has"
    automation_rules ||--o{ automation_rule_executions : "logs"
    event_outbox ||--o{ automation_rule_executions : "triggered"
    workspaces ||--o{ webhook_subscriptions : "has"
    inboxes ||--o{ webhook_subscriptions : "optionally scopes"
    webhook_subscriptions ||--o{ webhook_deliveries : "logs"
    event_outbox ||--o{ webhook_deliveries : "delivered as"
    organizations ||--o{ event_outbox : "emits"
    organizations ||--o{ inbound_webhook_events : "receives"
```

## 5. AI and knowledge

```mermaid
erDiagram
    organizations ||--o{ ai_provider_credentials : "owns"
    ai_provider_credentials ||--o{ ai_agents : "used by"
    workspaces ||--o{ ai_agents : "has"
    ai_agents ||--o{ ai_agent_inboxes : "serves"
    inboxes ||--o| ai_agent_inboxes : "served by"
    workspaces ||--o{ knowledge_bases : "has"
    ai_agents ||--o{ ai_agent_knowledge_bases : "searches"
    knowledge_bases ||--o{ ai_agent_knowledge_bases : "searched by"
    knowledge_bases ||--o{ kb_documents : "contains"
    kb_documents ||--o{ kb_chunks : "split into"
    stored_files ||--o{ kb_documents : "source file"
    conversations ||--o{ ai_conversation_sessions : "has"
    ai_agents ||--o{ ai_conversation_sessions : "runs"
    ai_conversation_sessions ||--o{ ai_runs : "executes"
    ai_runs ||--o{ ai_tool_calls : "makes"
    messages ||--o{ ai_runs : "triggers / replies"

    kb_chunks {
        uuid id PK
        uuid knowledge_base_id FK
        uuid document_id FK
        vector embedding "1536, HNSW cosine"
        tsvector search_vector "GIN"
    }
```

**Rules**
- One AI agent per inbox (`ai_agent_inboxes` PK is `inbox_id`); one agent may serve many inboxes.
- An AI agent searches only the knowledge bases linked to it. All are in the same workspace (composite FKs).
- `ai_conversation_sessions` is unique per `(conversation_id, ai_agent_id)`; `ai_runs` is unique per `(session_id, trigger_message_id)`, so one customer message triggers at most one run even if the event is delivered twice.

## 6. Billing, reporting, audit

```mermaid
erDiagram
    subscription_plans ||--o{ subscriptions : "priced by"
    organizations ||--o{ subscriptions : "has"
    subscriptions ||--o{ billing_records : "invoices"
    organizations ||--o{ billing_records : "billed"
    organizations ||--o{ usage_records : "meters"
    organizations ||--o{ reporting_events : "measures"
    organizations ||--o{ audit_logs : "audits"
```

- At most one live (`trialing`/`active`/`past_due`/`incomplete`) subscription per organization (`subscriptions_one_live_per_org`).
- `reporting_events` keeps loose references (no FK to conversations/users) so metrics survive deletions; it is still tenant-bound through the workspace FK.
- `audit_logs` keeps loose references on purpose: the audit trail must outlive the resources it describes.
