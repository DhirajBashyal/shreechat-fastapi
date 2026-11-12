# 06 — Database Architecture

This document explains the **decisions** behind the schema. The executable SQL is in [database/INITIAL_SCHEMA_SQL.md](database/INITIAL_SCHEMA_SQL.md), per-table DDL with explanations is in [07_DATABASE_SCHEMA.md](07_DATABASE_SCHEMA.md), and relationships are in [08_DATABASE_RELATIONSHIPS.md](08_DATABASE_RELATIONSHIPS.md).

## 1. Overview

- PostgreSQL 16+ (the schema uses `ON DELETE SET NULL (column)`, which needs PostgreSQL 15+). Validated on 17.6.
- Extensions: `pgcrypto` (`gen_random_uuid`, `gen_random_bytes`), `citext` (case-insensitive emails), `pg_trgm` (fuzzy search), `vector` (pgvector ≥ 0.7 for HNSW).
- One schema (`public`), **66 tables** in 11 domains:

| Domain | Tables |
|---|---|
| Platform | `users`, `platform_admins`, `user_sessions`, `permissions`, `subscription_plans` |
| Tenancy | `organizations`, `roles`, `role_permissions`, `organization_memberships`, `invitations`, `api_access_tokens`, `workspaces`, `workspace_counters`, `workspace_memberships` |
| Teams | `teams`, `team_members` |
| Inboxes & channels | `inboxes`, `inbox_members`, `channel_connections`, `channel_routes`, `facebook_pages`, `instagram_accounts`, `whatsapp_business_accounts`, `whatsapp_phone_numbers`, `whatsapp_message_templates`, `web_widgets` |
| Files | `stored_files` |
| Contacts | `contacts`, `contact_identities`, `notes`, `contact_labels` |
| Conversations | `conversations`, `messages`, `attachments`, `conversation_assignments`, `conversation_participants`, `mentions`, `conversation_labels` |
| Productivity | `labels`, `custom_attribute_definitions`, `canned_responses`, `macros`, `notifications` |
| Events & webhooks | `event_outbox`, `inbound_webhook_events`, `webhook_subscriptions`, `webhook_deliveries`, `idempotency_keys` |
| Automation | `automation_rules`, `automation_rule_executions` |
| AI | `ai_provider_credentials`, `ai_agents`, `ai_agent_inboxes`, `knowledge_bases`, `ai_agent_knowledge_bases`, `kb_documents`, `kb_chunks`, `ai_conversation_sessions`, `ai_runs`, `ai_tool_calls` |
| Reporting, billing, audit | `reporting_events`, `subscriptions`, `billing_records`, `usage_records`, `billing_webhook_events`, `audit_logs` |

## 2. Primary keys

| Choice | Where | Why |
|---|---|---|
| `uuid DEFAULT gen_random_uuid()` | All entity tables | Not guessable in URLs; can be generated in the app before insert (useful for idempotency and for building outbox payloads before flush); safe to merge data across environments |
| `bigint GENERATED ALWAYS AS IDENTITY` | Append-only logs: `event_outbox`, `reporting_events`, `usage_records`, `audit_logs` | Smaller, naturally ordered, never exposed in URLs |
| Composite natural PKs | Join tables (`team_members`, `inbox_members`, `conversation_labels`, …) | No surrogate needed |
| Text PK | `permissions.code`, `billing_webhook_events.id` (Stripe event id) | Natural identifiers |

**UUID v4 vs v7.** Random v4 UUIDs spread inserts across the B-tree, which costs some write performance on very large tables. When PostgreSQL 18 (`uuidv7()`) is the minimum version, or if the app generates v7 UUIDs (`uuid6` package), switch defaults for `messages`, `conversations` and `attachments`. No schema change is needed beyond the default.

**Human-friendly numbers.** `conversations.display_number` comes from `next_workspace_counter(workspace_id, 'conversation')`, an `INSERT … ON CONFLICT DO UPDATE … RETURNING` on `workspace_counters`. This replaces Chatwoot's design of one PostgreSQL sequence per account, created by a trigger. Trade-off: concurrent conversation creation in one workspace serializes on one row for the rest of the transaction. Conversation creation is short and rare compared to messages, so this is acceptable. If a workspace ever creates more than ~200 conversations per second, allocate numbers in a separate short transaction.

## 3. Tenant columns

Every tenant-owned row carries `organization_id`. Every workspace-owned row also carries `workspace_id`, **including deep children** such as `messages`, `attachments` and `kb_chunks`.

Why denormalise:
1. RLS policies are a simple column comparison, with no joins.
2. Every hot index can lead with `workspace_id` (e.g. `conversations_list_idx (workspace_id, status, last_activity_at DESC)`).
3. Composite foreign keys can enforce "child and parent belong to the same workspace".

### Composite tenant foreign keys

Parents expose `UNIQUE (workspace_id, id)`. Children reference `(workspace_id, parent_id)`:

```sql
FOREIGN KEY (workspace_id, conversation_id) REFERENCES conversations(workspace_id, id) ON DELETE CASCADE
```

A message therefore **cannot** be stored against a conversation in a different workspace, even if application code passes the wrong ids. Additionally, every workspace-scoped table has:

```sql
FOREIGN KEY (organization_id, workspace_id) REFERENCES workspaces(organization_id, id) ON DELETE CASCADE
```

so `organization_id` and `workspace_id` can never disagree. RLS's `WITH CHECK` then guarantees `organization_id` is the tenant of the current transaction. Together these make cross-tenant writes impossible at the database level (tested, see [database/TENANT_ISOLATION.md](database/TENANT_ISOLATION.md)).

`users` are global (one person can join many organizations), so `user_id` columns reference `users(id)` directly. Membership is checked by the application.

## 4. JSONB usage

JSONB is used for **data whose shape varies by provider or tenant, and which is read as a whole**. It is not used for data we filter or join on regularly.

| Column | Contents | Validated by |
|---|---|---|
| `organizations.settings`, `workspaces.settings`, `inboxes.settings` | UI and behaviour toggles | Pydantic `*Settings` model on write; `CHECK jsonb_typeof = 'object'` |
| `organizations.limits_override` | Per-tenant plan limit overrides | `PlanLimits` model |
| `inboxes.auto_assignment_config` | `{ "max_assignment_limit": 10, "strategy": "round_robin" }` | `AutoAssignmentConfig` |
| `inboxes.working_hours` | `[{ "day": 1, "open": "09:00", "close": "17:00", "closed": false }]` | `WorkingHours`; `CHECK jsonb_typeof = 'array'` |
| `channel_connections.config` | Non-secret provider config (API version, subscribed fields, webhook URL) | Per-adapter `ConnectionConfig` model |
| `contacts.custom_attributes`, `conversations.custom_attributes` | Tenant-defined fields | Validated against `custom_attribute_definitions` |
| `contacts.additional_attributes` | Browser, city, social handles, company | `ContactAdditionalAttributes` |
| `conversations.additional_attributes` | Referrer URL, browser language, initiated_at, Meta referral/ad data | `ConversationAdditionalAttributes` |
| `messages.content_attributes` | Interactive payloads (buttons/list), template name+params, reply-to external id, reaction, story reply, deleted flag, translation | Discriminated union per `content_type` |
| `attachments.meta` | width/height/duration/waveform, provider media id | `AttachmentMeta` |
| `automation_rules.conditions` / `actions` | Rule definition | `Condition` / `Action` discriminated unions |
| `ai_agents.persona`, `guardrails`, `handoff_policy` | AI configuration | `AIAgentConfig` |
| `ai_conversation_sessions.facts` | Structured memory (`{"order_id": "1234", "language": "es"}`) | `SessionFacts` |
| `event_outbox.payload`, `inbound_webhook_events.payload`, `webhook_deliveries.payload` | Event bodies | Event schemas |
| `audit_logs.changes` | `{ "field": [old, new] }` | — |

Rules:
1. Every JSONB column has a Pydantic model; the repository layer validates before writing.
2. If a JSONB key starts being filtered in list views, add an expression index (`CREATE INDEX … ((custom_attributes->>'plan'))`) or promote it to a column.
3. `NOT NULL DEFAULT '{}'` so code never handles `NULL` vs `{}`.
4. Size: keep each document small (< 64 KB). Large blobs go to `stored_files`.

## 5. Secrets and credentials

Third-party secrets (Meta page tokens, WhatsApp system-user tokens, Instagram tokens, AI provider keys, webhook signing secrets, widget HMAC secrets, MFA secrets) are stored **encrypted**, in `bytea` columns named `*_ciphertext` plus a `*_key_version smallint`.

**Envelope encryption scheme**

```text
master keys (KMS or env: CONVERSE_ENCRYPTION_KEYS = {"1": "<base64 32 bytes>", "2": "..."})
   │
   ▼
ciphertext = version_byte || nonce(12) || AES-256-GCM(key[version], plaintext, aad = "<table>:<column>:<row id>")
```

- **AAD binds the ciphertext to its row**, so copying a ciphertext into another tenant's row fails to decrypt.
- **Key rotation:** add key `n+1`, set it as the active write key, and run the `reencrypt_secrets` task, which decrypts with the old version and re-encrypts with the new. Remove the old key after the task reports zero rows on that version.
- In production, master keys come from a KMS (AWS KMS, GCP KMS, Vault Transit). The `Encryptor` interface supports a local-key implementation for dev and a KMS implementation for prod.
- Decrypted secrets are never logged, never returned by the API (only `api_key_last4`), and are held in memory only for the duration of a call.
- Credentials that need lookups (none currently) would need deterministic encryption or a separate hash column. API tokens and refresh tokens are **hashed** (SHA-256 of a high-entropy random value), not encrypted, because we never need the plaintext back.

## 6. Soft deletion

| Table | Soft delete? | Reason |
|---|---|---|
| `users`, `organizations`, `workspaces` | Yes (`deleted_at`) | Grace period for restore; hard purge by a scheduled job after 30 days |
| `inboxes`, `teams`, `contacts`, `canned_responses`, `ai_agents`, `knowledge_bases`, `kb_documents`, `notes` | Yes | Accidental deletes are common; references from history remain readable |
| `messages` | Yes (`deleted_at`), content cleared | "Message deleted" must still show in the timeline; Meta unsend events |
| `conversations` | **No** | Deleting a conversation is rare and intentional (admin + permission); hard delete via background job (`CASCADE` children) |
| Join tables, logs, events | No | No meaning to restore |

Partial unique indexes (`WHERE deleted_at IS NULL`) let a soft-deleted name be reused (e.g. `teams_name_per_ws_uniq`). Repositories add `deleted_at IS NULL` by default. Admin "trash" views opt out explicitly.

**GDPR erasure** is different from soft delete: `ContactErasureService` hard-deletes the contact, their identities, and message content/attachments in the contact's conversations, then writes an audit log entry without PII.

## 7. Growth and partitioning

| Table | Expected growth | Plan |
|---|---|---|
| `messages` | Largest (10–100× conversations) | Start unpartitioned. Above ~200M rows, migrate to range partitioning by `created_at` (monthly). The unique index on `(inbox_id, external_id)` then has to include `created_at` or move to a separate `message_external_ids` table. Decide at that point using real query plans |
| `event_outbox` | High churn | Delete `dispatched` rows older than 7 days (beat task, batched by id range) |
| `inbound_webhook_events` | High | Keep 14 days of `processed`/`ignored` rows; `dead` rows kept until resolved |
| `webhook_deliveries` | High | Keep 30 days |
| `reporting_events` | Medium | Monthly partitioning when large; rollups for dashboards |
| `kb_chunks` | Medium | HNSW index memory: ~(dims × 4 bytes + overhead) per row; 1M chunks × 1536 dims ≈ 6–8 GB index. Watch `maintenance_work_mem` during builds |

## 8. Row-level security

Summary (full SQL and tests in [database/TENANT_ISOLATION.md](database/TENANT_ISOLATION.md)):

- `converse_app` role: RLS `ENABLE` + `FORCE` on every table with `organization_id` (and `organizations` by `id`). Policy: `organization_id = current_org_id()` for both `USING` and `WITH CHECK`.
- `current_org_id()` reads `current_setting('app.current_org_id', true)`. The app sets it with `SET LOCAL` at the start of each transaction, so it **cannot leak** across pooled connections. Unset → `NULL` → no rows.
- `converse_system` role: `BYPASSRLS`. Used by webhook routing (`channel_routes`), the outbox relay, cross-tenant scheduled scans, login (finding a user's memberships across organizations) and platform admin. Code paths using it are confined to `converse/core/db/system.py` and reviewed.
- Global tables (`users`, `user_sessions`, `permissions`, `subscription_plans`, `platform_admins`) have no `organization_id` and no RLS; the API exposes them only through auth/platform modules.
- `workspace_counters` has no `organization_id`; it is only reachable through `next_workspace_counter()` called by `ConversationService` with an already-authorized workspace id.

**Connection pooling:** with PgBouncer in transaction mode, `SET LOCAL` is safe (scoped to the transaction). Never use session-level `SET`.

## 9. Migration strategy

1. **Baseline:** Alembic revision `0001_initial` executes the three SQL files from [database/INITIAL_SCHEMA_SQL.md](database/INITIAL_SCHEMA_SQL.md) (schema, functions + RLS) with `op.execute()`. Reference seed data (permissions, plans) runs in `0002_reference_data`. Demo data is **not** a migration; it's `python -m converse.cli seed-demo`.
2. **SQLAlchemy models** mirror the schema. A CI job runs `alembic upgrade head` on an empty database and then `alembic check` (autogenerate diff must be empty) to catch drift between models and migrations.
3. **Every later change is an Alembic migration** written with `op.*` APIs and reviewed by hand. Generate with `alembic revision -m "<name>"`. Never edit an applied migration.
4. **Zero-downtime rules** (expand → migrate → contract):
   - Add columns as nullable or with a constant default; backfill in batches in a Celery task, not in the migration.
   - Create indexes with `op.create_index(..., postgresql_concurrently=True)` inside an `autocommit_block()`.
   - Add constraints as `NOT VALID`, then `VALIDATE CONSTRAINT` in a later migration.
   - Rename = add new column, dual-write, backfill, switch reads, drop old (across releases).
   - Set `lock_timeout = '5s'` and `statement_timeout` in migration sessions so a blocked migration fails instead of queueing all traffic behind it.
5. **New tenant tables** must: include `organization_id` (+ `workspace_id` if workspace-scoped), the composite FKs, and an RLS policy in the same migration. A CI test lists all tables with `organization_id` and fails if any lacks RLS.
6. **Rollback:** each migration has a `downgrade()` for development. In production we roll forward; destructive downgrades are not run.
