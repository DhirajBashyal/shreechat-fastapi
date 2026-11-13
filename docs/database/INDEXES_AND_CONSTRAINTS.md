# Indexes and Constraints

Exported from the tested database (`pg_indexes`, `pg_constraint`). The reasoning behind each index group comes first; the full list follows.

## 1. Index design rules

1. **Lead with the tenant.** Hot list indexes start with `workspace_id` (e.g. `conversations_list_idx (workspace_id, status, last_activity_at DESC)`), so a query for one workspace reads one contiguous index range.
2. **Match the sort.** The conversation list sorts by `last_activity_at DESC`; the index stores that order so Postgres can stop after `LIMIT` rows. Use keyset pagination: `WHERE (last_activity_at, id) < (:cursor_ts, :cursor_id)`.
3. **Partial indexes for sparse states.** `conversations_snoozed_idx … WHERE status = 'snoozed'`, `event_outbox_pending_idx … WHERE state = 'pending'`, `notifications_user_unread_idx … WHERE read_at IS NULL`. Small and hot.
4. **Uniqueness that encodes business rules:**
   - `messages_external_id_uniq (inbox_id, external_id)`: a provider message is stored at most once (database-level webhook dedup).
   - `messages_client_id_uniq (conversation_id, client_message_id)`: a client retry can't double-send.
   - `contact_identities_source_uniq (inbox_id, source_id)`: one identity per PSID/IGSID/wa_id per inbox; create races resolve with `ON CONFLICT`.
   - `inbound_webhook_events_dedup_uniq (provider, dedup_key)`: first-line dedup of Meta retries.
   - `automation_executions_once_uniq (rule_id, outbox_event_id)` and `webhook_deliveries_event_uniq (subscription_id, outbox_event_id)`: handlers stay idempotent under at-least-once delivery.
   - `subscriptions_one_live_per_org`: at most one live subscription per organization.
   - `usage_records_source_uniq`: metered usage counted once per source event.
   - Soft-delete-aware uniques (`… WHERE deleted_at IS NULL`) so names can be reused after deletion.
5. **Search.** `pg_trgm` GIN indexes on contacts (name/email/phone expression) and on non-private message content; `tsvector` GIN on `kb_chunks` for the keyword half of hybrid search. The query must use the **same expression** as the index to hit it.
6. **Vectors.** `kb_chunks_embedding_hnsw_idx USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`. Set `SET LOCAL hnsw.ef_search = 100` in retrieval transactions. Every query also filters by `knowledge_base_id` (and RLS by tenant), so use pgvector ≥ 0.8 **iterative index scans** (`SET LOCAL hnsw.iterative_scan = relaxed_order`) so filtering doesn't starve results.
7. **FK columns are indexed** where the parent is deleted or joined often (e.g. `attachments_message_idx`, `conversation_labels_label_idx`). Composite FK columns are covered by composite uniques or leading index columns.
8. **Verify with real plans.** Before release, run `EXPLAIN (ANALYZE, BUFFERS)` for the queries in [../backend/API_DESIGN.md](../backend/API_DESIGN.md) on a dataset with at least one large tenant (10M messages) and many small ones. A small fixture database is not performance evidence.

## 2. Constraint design rules

- `NOT NULL` on every column the app always sets; `DEFAULT '{}'`/`'[]'` on JSONB.
- `CHECK (jsonb_typeof(...))` keeps JSONB documents the right shape (object vs array).
- Cross-column invariants as `CHECK`: snoozed requires `snoozed_until`; private notes must be outgoing; `sender_kind` matches the populated sender column; KB document source matches `source_type`; HTTPS-only webhook URLs.
- Stable value sets are PostgreSQL `ENUM` types (statuses, directions). Value sets that will grow (assignment reasons, AI providers, usage metrics) are `CHECK (… IN (…))`, which is easier to change in a migration.
- Rules that depend on other rows or on configuration (plan limits, custom attribute validation, assignment eligibility) are enforced in services, not the database.

## 3. All indexes (210, including primary keys and unique constraints)

### `ai_agent_inboxes`

| Index | Definition |
|---|---|
| `ai_agent_inboxes_agent_idx` | `USING btree (ai_agent_id)` |
| `ai_agent_inboxes_pkey` | `UNIQUE USING btree (inbox_id)` |

### `ai_agent_knowledge_bases`

| Index | Definition |
|---|---|
| `ai_agent_knowledge_bases_pkey` | `UNIQUE USING btree (ai_agent_id, knowledge_base_id)` |

### `ai_agents`

| Index | Definition |
|---|---|
| `ai_agents_pkey` | `UNIQUE USING btree (id)` |
| `ai_agents_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `ai_conversation_sessions`

| Index | Definition |
|---|---|
| `ai_conversation_sessions_pkey` | `UNIQUE USING btree (id)` |
| `ai_sessions_conversation_uniq` | `UNIQUE USING btree (conversation_id, ai_agent_id)` |
| `ai_sessions_thread_uniq` | `UNIQUE USING btree (thread_id)` |
| `ai_sessions_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `ai_provider_credentials`

| Index | Definition |
|---|---|
| `ai_provider_credentials_label_uniq` | `UNIQUE USING btree (organization_id, label)` |
| `ai_provider_credentials_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `ai_provider_credentials_pkey` | `UNIQUE USING btree (id)` |

### `ai_runs`

| Index | Definition |
|---|---|
| `ai_runs_conversation_idx` | `USING btree (conversation_id, started_at DESC)` |
| `ai_runs_pkey` | `UNIQUE USING btree (id)` |
| `ai_runs_trigger_uniq` | `UNIQUE USING btree (session_id, trigger_message_id)` |
| `ai_runs_ws_started_idx` | `USING btree (workspace_id, started_at)` |

### `ai_tool_calls`

| Index | Definition |
|---|---|
| `ai_tool_calls_pkey` | `UNIQUE USING btree (id)` |
| `ai_tool_calls_run_idx` | `USING btree (run_id)` |

### `api_access_tokens`

| Index | Definition |
|---|---|
| `api_access_tokens_hash_uniq` | `UNIQUE USING btree (token_hash)` |
| `api_access_tokens_org_idx` | `USING btree (organization_id)` |
| `api_access_tokens_pkey` | `UNIQUE USING btree (id)` |

### `attachments`

| Index | Definition |
|---|---|
| `attachments_message_idx` | `USING btree (message_id)` |
| `attachments_pkey` | `UNIQUE USING btree (id)` |
| `attachments_ws_idx` | `USING btree (workspace_id, created_at)` |

### `audit_logs`

| Index | Definition |
|---|---|
| `audit_logs_org_time_idx` | `USING btree (organization_id, created_at DESC)` |
| `audit_logs_pkey` | `UNIQUE USING btree (id)` |
| `audit_logs_resource_idx` | `USING btree (resource_type, resource_id)` |

### `automation_rule_executions`

| Index | Definition |
|---|---|
| `automation_executions_once_uniq` | `UNIQUE USING btree (rule_id, outbox_event_id)` |
| `automation_executions_rule_idx` | `USING btree (rule_id, created_at DESC)` |
| `automation_rule_executions_pkey` | `UNIQUE USING btree (id)` |

### `automation_rules`

| Index | Definition |
|---|---|
| `automation_rules_lookup_idx` | `USING btree (workspace_id, event_name, "position") WHERE is_active` |
| `automation_rules_pkey` | `UNIQUE USING btree (id)` |
| `automation_rules_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `billing_records`

| Index | Definition |
|---|---|
| `billing_records_external_uniq` | `UNIQUE USING btree (external_invoice_id) WHERE (external_invoice_id IS NOT NULL)` |
| `billing_records_org_idx` | `USING btree (organization_id, created_at DESC)` |
| `billing_records_pkey` | `UNIQUE USING btree (id)` |

### `billing_webhook_events`

| Index | Definition |
|---|---|
| `billing_webhook_events_pkey` | `UNIQUE USING btree (id)` |

### `canned_responses`

| Index | Definition |
|---|---|
| `canned_responses_code_uniq` | `UNIQUE USING btree (workspace_id, lower((short_code)::text)) WHERE (deleted_at IS NULL)` |
| `canned_responses_pkey` | `UNIQUE USING btree (id)` |
| `canned_responses_search_idx` | `USING gin (((((short_code)::text \|\| ' '::text) \|\| content)) gin_trgm_ops)` |

### `channel_connections`

| Index | Definition |
|---|---|
| `channel_connections_inbox_uniq` | `UNIQUE USING btree (inbox_id)` |
| `channel_connections_pkey` | `UNIQUE USING btree (id)` |
| `channel_connections_token_expiry_idx` | `USING btree (token_expires_at) WHERE (status = 'active'::connection_status)` |
| `channel_connections_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `channel_routes`

| Index | Definition |
|---|---|
| `channel_routes_connection_idx` | `USING btree (channel_connection_id)` |
| `channel_routes_pkey` | `UNIQUE USING btree (provider, external_id)` |

### `contact_identities`

| Index | Definition |
|---|---|
| `contact_identities_contact_idx` | `USING btree (contact_id)` |
| `contact_identities_pkey` | `UNIQUE USING btree (id)` |
| `contact_identities_source_uniq` | `UNIQUE USING btree (inbox_id, source_id)` |
| `contact_identities_token_uniq` | `UNIQUE USING btree (realtime_token)` |
| `contact_identities_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `contact_labels`

| Index | Definition |
|---|---|
| `contact_labels_label_idx` | `USING btree (label_id)` |
| `contact_labels_pkey` | `UNIQUE USING btree (contact_id, label_id)` |

### `contacts`

| Index | Definition |
|---|---|
| `contacts_activity_idx` | `USING btree (workspace_id, last_activity_at DESC NULLS LAST) WHERE (deleted_at IS NULL)` |
| `contacts_email_per_ws_uniq` | `UNIQUE USING btree (workspace_id, email) WHERE ((email IS NOT NULL) AND (deleted_at IS NULL))` |
| `contacts_identifier_per_ws_uniq` | `UNIQUE USING btree (workspace_id, identifier) WHERE ((identifier IS NOT NULL) AND (deleted_at IS NULL))` |
| `contacts_org_idx` | `USING btree (organization_id)` |
| `contacts_phone_idx` | `USING btree (workspace_id, phone_number) WHERE (phone_number IS NOT NULL)` |
| `contacts_pkey` | `UNIQUE USING btree (id)` |
| `contacts_search_trgm_idx` | `USING gin (((((((COALESCE(name, ''::character varying))::text \|\| ' '::text) \|\| COALESCE((email)::text, ''::text)) \|\| ' '::text) \|\| (COALESCE(phone_number, ''::character varying))::text)) gin_trgm_ops)` |
| `contacts_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `conversation_assignments`

| Index | Definition |
|---|---|
| `conversation_assignments_assignee_idx` | `USING btree (workspace_id, assignee_id, created_at)` |
| `conversation_assignments_conv_idx` | `USING btree (conversation_id, created_at DESC)` |
| `conversation_assignments_pkey` | `UNIQUE USING btree (id)` |

### `conversation_labels`

| Index | Definition |
|---|---|
| `conversation_labels_label_idx` | `USING btree (label_id)` |
| `conversation_labels_pkey` | `UNIQUE USING btree (conversation_id, label_id)` |

### `conversation_participants`

| Index | Definition |
|---|---|
| `conversation_participants_pkey` | `UNIQUE USING btree (conversation_id, user_id)` |
| `conversation_participants_user_idx` | `USING btree (user_id, workspace_id)` |

### `conversations`

| Index | Definition |
|---|---|
| `conversations_assignee_idx` | `USING btree (workspace_id, assignee_id, status) WHERE (assignee_id IS NOT NULL)` |
| `conversations_contact_idx` | `USING btree (contact_id, created_at DESC)` |
| `conversations_display_uniq` | `UNIQUE USING btree (workspace_id, display_number)` |
| `conversations_identity_open_idx` | `USING btree (contact_identity_id) WHERE (status <> 'resolved'::conversation_status)` |
| `conversations_inbox_status_idx` | `USING btree (workspace_id, inbox_id, status, assignee_id)` |
| `conversations_list_idx` | `USING btree (workspace_id, status, last_activity_at DESC)` |
| `conversations_org_idx` | `USING btree (organization_id)` |
| `conversations_pkey` | `UNIQUE USING btree (id)` |
| `conversations_snoozed_idx` | `USING btree (snoozed_until) WHERE (status = 'snoozed'::conversation_status)` |
| `conversations_team_idx` | `USING btree (workspace_id, team_id, status) WHERE (team_id IS NOT NULL)` |
| `conversations_waiting_idx` | `USING btree (workspace_id, waiting_since) WHERE (waiting_since IS NOT NULL)` |
| `conversations_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `custom_attribute_definitions`

| Index | Definition |
|---|---|
| `custom_attr_key_uniq` | `UNIQUE USING btree (workspace_id, scope, attribute_key)` |
| `custom_attribute_definitions_pkey` | `UNIQUE USING btree (id)` |

### `event_outbox`

| Index | Definition |
|---|---|
| `event_outbox_aggregate_idx` | `USING btree (aggregate_type, aggregate_id)` |
| `event_outbox_pending_idx` | `USING btree (available_at, id) WHERE (state = 'pending'::outbox_state)` |
| `event_outbox_pkey` | `UNIQUE USING btree (id)` |

### `facebook_pages`

| Index | Definition |
|---|---|
| `facebook_pages_connection_uniq` | `UNIQUE USING btree (channel_connection_id)` |
| `facebook_pages_page_idx` | `USING btree (page_id)` |
| `facebook_pages_page_per_org_uniq` | `UNIQUE USING btree (organization_id, page_id)` |
| `facebook_pages_pkey` | `UNIQUE USING btree (id)` |

### `idempotency_keys`

| Index | Definition |
|---|---|
| `idempotency_keys_expiry_idx` | `USING btree (expires_at)` |
| `idempotency_keys_pkey` | `UNIQUE USING btree (organization_id, idempotency_key)` |

### `inbound_webhook_events`

| Index | Definition |
|---|---|
| `inbound_webhook_events_dedup_uniq` | `UNIQUE USING btree (provider, dedup_key)` |
| `inbound_webhook_events_org_idx` | `USING btree (organization_id, received_at DESC)` |
| `inbound_webhook_events_pkey` | `UNIQUE USING btree (id)` |
| `inbound_webhook_events_state_idx` | `USING btree (state, received_at) WHERE (state = ANY (ARRAY['received'::inbound_event_state, 'failed'::inbound_event_state]))` |

### `inbox_members`

| Index | Definition |
|---|---|
| `inbox_members_pkey` | `UNIQUE USING btree (inbox_id, user_id)` |
| `inbox_members_user_idx` | `USING btree (user_id, workspace_id)` |

### `inboxes`

| Index | Definition |
|---|---|
| `inboxes_org_idx` | `USING btree (organization_id)` |
| `inboxes_pkey` | `UNIQUE USING btree (id)` |
| `inboxes_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |
| `inboxes_ws_idx` | `USING btree (workspace_id) WHERE (deleted_at IS NULL)` |

### `instagram_accounts`

| Index | Definition |
|---|---|
| `instagram_accounts_connection_uniq` | `UNIQUE USING btree (channel_connection_id)` |
| `instagram_accounts_ig_uniq` | `UNIQUE USING btree (instagram_user_id)` |
| `instagram_accounts_pkey` | `UNIQUE USING btree (id)` |

### `invitations`

| Index | Definition |
|---|---|
| `invitations_open_email_uniq` | `UNIQUE USING btree (organization_id, email) WHERE (accepted_at IS NULL)` |
| `invitations_pkey` | `UNIQUE USING btree (id)` |
| `invitations_token_uniq` | `UNIQUE USING btree (token_hash)` |

### `kb_chunks`

| Index | Definition |
|---|---|
| `kb_chunks_embedding_hnsw_idx` | `USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')` |
| `kb_chunks_fts_idx` | `USING gin (search_vector)` |
| `kb_chunks_kb_idx` | `USING btree (knowledge_base_id)` |
| `kb_chunks_pkey` | `UNIQUE USING btree (id)` |
| `kb_chunks_position_uniq` | `UNIQUE USING btree (document_id, chunk_index)` |

### `kb_documents`

| Index | Definition |
|---|---|
| `kb_documents_kb_idx` | `USING btree (knowledge_base_id, status) WHERE (deleted_at IS NULL)` |
| `kb_documents_pkey` | `UNIQUE USING btree (id)` |
| `kb_documents_url_uniq` | `UNIQUE USING btree (knowledge_base_id, source_url) WHERE ((source_url IS NOT NULL) AND (deleted_at IS NULL))` |
| `kb_documents_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `knowledge_bases`

| Index | Definition |
|---|---|
| `knowledge_bases_pkey` | `UNIQUE USING btree (id)` |
| `knowledge_bases_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `labels`

| Index | Definition |
|---|---|
| `labels_pkey` | `UNIQUE USING btree (id)` |
| `labels_title_per_ws_uniq` | `UNIQUE USING btree (workspace_id, title)` |
| `labels_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `macros`

| Index | Definition |
|---|---|
| `macros_pkey` | `UNIQUE USING btree (id)` |
| `macros_ws_idx` | `USING btree (workspace_id)` |

### `mentions`

| Index | Definition |
|---|---|
| `mentions_pkey` | `UNIQUE USING btree (id)` |
| `mentions_uniq` | `UNIQUE USING btree (message_id, user_id)` |
| `mentions_user_idx` | `USING btree (user_id, workspace_id, created_at DESC)` |

### `messages`

| Index | Definition |
|---|---|
| `messages_client_id_uniq` | `UNIQUE USING btree (conversation_id, client_message_id) WHERE (client_message_id IS NOT NULL)` |
| `messages_content_trgm_idx` | `USING gin (content gin_trgm_ops) WHERE (is_private = false)` |
| `messages_conversation_seq_idx` | `USING btree (conversation_id, sequence_number)` |
| `messages_external_id_uniq` | `UNIQUE USING btree (inbox_id, external_id) WHERE (external_id IS NOT NULL)` |
| `messages_org_idx` | `USING btree (organization_id)` |
| `messages_pkey` | `UNIQUE USING btree (id)` |
| `messages_ws_created_idx` | `USING btree (workspace_id, created_at)` |
| `messages_ws_direction_created_idx` | `USING btree (workspace_id, direction, created_at)` |
| `messages_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `notes`

| Index | Definition |
|---|---|
| `notes_contact_idx` | `USING btree (contact_id, created_at DESC) WHERE (deleted_at IS NULL)` |
| `notes_pkey` | `UNIQUE USING btree (id)` |

### `notifications`

| Index | Definition |
|---|---|
| `notifications_pkey` | `UNIQUE USING btree (id)` |
| `notifications_user_feed_idx` | `USING btree (user_id, workspace_id, created_at DESC)` |
| `notifications_user_unread_idx` | `USING btree (user_id, workspace_id) WHERE (read_at IS NULL)` |

### `organization_memberships`

| Index | Definition |
|---|---|
| `org_memberships_one_owner_uniq` | `UNIQUE USING btree (organization_id) WHERE (role = 'owner'::membership_role)` |
| `org_memberships_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `org_memberships_user_idx` | `USING btree (user_id)` |
| `org_memberships_user_uniq` | `UNIQUE USING btree (organization_id, user_id)` |
| `organization_memberships_pkey` | `UNIQUE USING btree (id)` |

### `organizations`

| Index | Definition |
|---|---|
| `organizations_pkey` | `UNIQUE USING btree (id)` |
| `organizations_slug_uniq` | `UNIQUE USING btree (slug) WHERE (deleted_at IS NULL)` |
| `organizations_status_idx` | `USING btree (status)` |

### `permissions`

| Index | Definition |
|---|---|
| `permissions_pkey` | `UNIQUE USING btree (code)` |

### `platform_admins`

| Index | Definition |
|---|---|
| `platform_admins_pkey` | `UNIQUE USING btree (id)` |
| `platform_admins_user_uniq` | `UNIQUE USING btree (user_id)` |

### `reporting_events`

| Index | Definition |
|---|---|
| `reporting_events_conversation_idx` | `USING btree (conversation_id)` |
| `reporting_events_pkey` | `UNIQUE USING btree (id)` |
| `reporting_events_ws_inbox_idx` | `USING btree (workspace_id, inbox_id, created_at)` |
| `reporting_events_ws_name_time_idx` | `USING btree (workspace_id, name, created_at)` |
| `reporting_events_ws_user_idx` | `USING btree (workspace_id, user_id, created_at)` |

### `role_permissions`

| Index | Definition |
|---|---|
| `role_permissions_pkey` | `UNIQUE USING btree (role_id, permission_code)` |

### `roles`

| Index | Definition |
|---|---|
| `roles_name_per_org_uniq` | `UNIQUE USING btree (organization_id, name)` |
| `roles_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `roles_pkey` | `UNIQUE USING btree (id)` |

### `stored_files`

| Index | Definition |
|---|---|
| `stored_files_key_uniq` | `UNIQUE USING btree (storage_backend, storage_key)` |
| `stored_files_org_created_idx` | `USING btree (organization_id, created_at)` |
| `stored_files_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `stored_files_pkey` | `UNIQUE USING btree (id)` |

### `subscription_plans`

| Index | Definition |
|---|---|
| `subscription_plans_code_uniq` | `UNIQUE USING btree (code)` |
| `subscription_plans_pkey` | `UNIQUE USING btree (id)` |

### `subscriptions`

| Index | Definition |
|---|---|
| `subscriptions_external_uniq` | `UNIQUE USING btree (external_subscription_id) WHERE (external_subscription_id IS NOT NULL)` |
| `subscriptions_one_live_per_org` | `UNIQUE USING btree (organization_id) WHERE (state = ANY (ARRAY['trialing'::subscription_state, 'active'::subscription_state, 'past_due'::subscription_state, 'incomplete'::subscription_state]))` |
| `subscriptions_pkey` | `UNIQUE USING btree (id)` |

### `team_members`

| Index | Definition |
|---|---|
| `team_members_pkey` | `UNIQUE USING btree (team_id, user_id)` |
| `team_members_user_idx` | `USING btree (user_id, workspace_id)` |

### `teams`

| Index | Definition |
|---|---|
| `teams_name_per_ws_uniq` | `UNIQUE USING btree (workspace_id, lower((name)::text)) WHERE (deleted_at IS NULL)` |
| `teams_org_idx` | `USING btree (organization_id)` |
| `teams_pkey` | `UNIQUE USING btree (id)` |
| `teams_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `usage_records`

| Index | Definition |
|---|---|
| `usage_records_period_idx` | `USING btree (organization_id, metric, period_start)` |
| `usage_records_pkey` | `UNIQUE USING btree (id)` |
| `usage_records_source_uniq` | `UNIQUE USING btree (organization_id, metric, source_id)` |

### `user_sessions`

| Index | Definition |
|---|---|
| `user_sessions_family_idx` | `USING btree (family_id)` |
| `user_sessions_pkey` | `UNIQUE USING btree (id)` |
| `user_sessions_token_uniq` | `UNIQUE USING btree (refresh_token_hash)` |
| `user_sessions_user_active_idx` | `USING btree (user_id) WHERE (revoked_at IS NULL)` |

### `users`

| Index | Definition |
|---|---|
| `users_email_uniq` | `UNIQUE USING btree (email) WHERE (deleted_at IS NULL)` |
| `users_pkey` | `UNIQUE USING btree (id)` |
| `users_realtime_token_uniq` | `UNIQUE USING btree (realtime_token)` |

### `web_widgets`

| Index | Definition |
|---|---|
| `web_widgets_connection_uniq` | `UNIQUE USING btree (channel_connection_id)` |
| `web_widgets_pkey` | `UNIQUE USING btree (id)` |
| `web_widgets_token_uniq` | `UNIQUE USING btree (website_token)` |

### `webhook_deliveries`

| Index | Definition |
|---|---|
| `webhook_deliveries_event_uniq` | `UNIQUE USING btree (subscription_id, outbox_event_id)` |
| `webhook_deliveries_pkey` | `UNIQUE USING btree (id)` |
| `webhook_deliveries_retry_idx` | `USING btree (next_attempt_at) WHERE (state = ANY (ARRAY['pending'::webhook_delivery_state, 'failed'::webhook_delivery_state]))` |
| `webhook_deliveries_sub_idx` | `USING btree (subscription_id, created_at DESC)` |

### `webhook_subscriptions`

| Index | Definition |
|---|---|
| `webhook_subscriptions_pkey` | `UNIQUE USING btree (id)` |
| `webhook_subscriptions_url_uniq` | `UNIQUE USING btree (workspace_id, url)` |
| `webhook_subscriptions_ws_id_uniq` | `UNIQUE USING btree (workspace_id, id)` |

### `whatsapp_business_accounts`

| Index | Definition |
|---|---|
| `whatsapp_business_accounts_pkey` | `UNIQUE USING btree (id)` |
| `whatsapp_waba_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `whatsapp_waba_per_org_uniq` | `UNIQUE USING btree (organization_id, waba_id)` |

### `whatsapp_message_templates`

| Index | Definition |
|---|---|
| `whatsapp_message_templates_pkey` | `UNIQUE USING btree (id)` |
| `whatsapp_templates_uniq` | `UNIQUE USING btree (waba_id, name, language)` |

### `whatsapp_phone_numbers`

| Index | Definition |
|---|---|
| `whatsapp_phone_connection_uniq` | `UNIQUE USING btree (channel_connection_id)` |
| `whatsapp_phone_number_id_uniq` | `UNIQUE USING btree (phone_number_id)` |
| `whatsapp_phone_numbers_pkey` | `UNIQUE USING btree (id)` |
| `whatsapp_phone_waba_idx` | `USING btree (waba_id)` |

### `workspace_counters`

| Index | Definition |
|---|---|
| `workspace_counters_pkey` | `UNIQUE USING btree (workspace_id, counter_name)` |

### `workspace_memberships`

| Index | Definition |
|---|---|
| `workspace_memberships_member_idx` | `USING btree (membership_id)` |
| `workspace_memberships_pkey` | `UNIQUE USING btree (workspace_id, membership_id)` |

### `workspaces`

| Index | Definition |
|---|---|
| `workspaces_org_id_uniq` | `UNIQUE USING btree (organization_id, id)` |
| `workspaces_pkey` | `UNIQUE USING btree (id)` |
| `workspaces_slug_per_org_uniq` | `UNIQUE USING btree (organization_id, slug) WHERE (deleted_at IS NULL)` |

## 4. All CHECK constraints (50)

| Table | Constraint | Definition |
|---|---|---|
| `ai_agent_inboxes` | `ai_agent_inboxes_mode_chk` | `CHECK (((mode)::text = ANY ((ARRAY['autonomous'::character varying, 'assist_only'::character varying])::text[])))` |
| `ai_agents` | `ai_agents_actions_chk` | `CHECK (((allowed_actions <@ ARRAY['reply'::text, 'handoff'::text, 'add_label'::text, 'set_priority'::text, 'resolve'::text, 'snooze'::text, 'update_contact'::text, 'add_private_note'::text]) AND ('handoff'::text = ANY (allowed_actions))))` |
| `ai_agents` | `ai_agents_temperature_chk` | `CHECK (((temperature >= (0)::numeric) AND (temperature <= (2)::numeric)))` |
| `ai_agents` | `ai_agents_tool_settings_is_object` | `CHECK ((jsonb_typeof(tool_settings) = 'object'::text))` |
| `ai_provider_credentials` | `ai_provider_credentials_provider_chk` | `CHECK (((provider)::text = ANY ((ARRAY['openai'::character varying, 'anthropic'::character varying, 'azure_openai'::character varying, 'gemini'::character varying, 'openai_compatible'::character varying])::text[])))` |
| `ai_conversation_sessions` | `ai_sessions_status_chk` | `CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'handed_off'::character varying, 'closed'::character varying])::text[])))` |
| `ai_conversation_sessions` | `ai_sessions_verification_chk` | `CHECK (((customer_verification_level)::text = ANY ((ARRAY['none'::character varying, 'channel'::character varying, 'verified'::character varying])::text[])))` |
| `ai_tool_calls` | `ai_tool_calls_decision_chk` | `CHECK (((policy_decision)::text = ANY ((ARRAY['allowed'::character varying, 'denied'::character varying, 'needs_verification'::character varying, 'needs_approval'::character varying])::text[])))` |
| `attachments` | `attachments_source_chk` | `CHECK (((file_id IS NOT NULL) OR (external_url IS NOT NULL) OR (kind = ANY (ARRAY['location'::attachment_kind, 'fallback'::attachment_kind]))))` |
| `audit_logs` | `audit_logs_actor_chk` | `CHECK (((actor_kind)::text = ANY ((ARRAY['user'::character varying, 'platform_admin'::character varying, 'api_token'::character varying, 'system'::character varying, 'ai_agent'::character varying])::text[])))` |
| `automation_rules` | `automation_rules_actions_is_array` | `CHECK (((jsonb_typeof(actions) = 'array'::text) AND (jsonb_array_length(actions) > 0)))` |
| `automation_rules` | `automation_rules_conditions_is_array` | `CHECK ((jsonb_typeof(conditions) = 'array'::text))` |
| `automation_rules` | `automation_rules_event_chk` | `CHECK (((event_name)::text = ANY ((ARRAY['conversation.created'::character varying, 'conversation.updated'::character varying, 'conversation.opened'::character varying, 'conversation.resolved'::character varying, 'message.created'::character varying])::text[])))` |
| `billing_records` | `billing_records_amount_chk` | `CHECK (((amount_due_cents >= 0) AND (amount_paid_cents >= 0)))` |
| `canned_responses` | `canned_responses_content_len_chk` | `CHECK ((char_length(content) <= 10000))` |
| `channel_connections` | `channel_connections_secret_key_chk` | `CHECK (((credentials_ciphertext IS NULL) OR (credentials_key_version IS NOT NULL)))` |
| `contacts` | `contacts_additional_attrs_is_object` | `CHECK ((jsonb_typeof(additional_attributes) = 'object'::text))` |
| `contacts` | `contacts_custom_attrs_is_object` | `CHECK ((jsonb_typeof(custom_attributes) = 'object'::text))` |
| `contacts` | `contacts_type_chk` | `CHECK (((contact_type)::text = ANY ((ARRAY['visitor'::character varying, 'lead'::character varying, 'customer'::character varying])::text[])))` |
| `conversation_assignments` | `conversation_assignments_reason_chk` | `CHECK (((reason)::text = ANY ((ARRAY['manual'::character varying, 'auto_round_robin'::character varying, 'auto_capacity'::character varying, 'automation'::character varying, 'ai_handoff'::character varying, 'unassigned'::character varying])::text[])))` |
| `conversations` | `conversations_custom_attrs_is_object` | `CHECK ((jsonb_typeof(custom_attributes) = 'object'::text))` |
| `conversations` | `conversations_snooze_chk` | `CHECK (((status <> 'snoozed'::conversation_status) OR (snoozed_until IS NOT NULL)))` |
| `custom_attribute_definitions` | `custom_attr_key_format_chk` | `CHECK (((attribute_key)::text ~ '^[a-z][a-z0-9_]*$'::text))` |
| `custom_attribute_definitions` | `custom_attr_list_chk` | `CHECK (((value_type <> 'list'::attribute_value_type) OR (cardinality(list_values) > 0)))` |
| `inboxes` | `inboxes_working_hours_is_array` | `CHECK ((jsonb_typeof(working_hours) = 'array'::text))` |
| `instagram_accounts` | `instagram_accounts_method_chk` | `CHECK (((connection_method)::text = ANY ((ARRAY['instagram_login'::character varying, 'facebook_page'::character varying])::text[])))` |
| `kb_documents` | `kb_documents_source_chk` | `CHECK (((((source_type)::text = 'url'::text) AND (source_url IS NOT NULL)) OR (((source_type)::text = 'file'::text) AND (file_id IS NOT NULL)) OR ((source_type)::text = ANY ((ARRAY['text'::character varying, 'faq'::character varying])::text[]))))` |
| `knowledge_bases` | `knowledge_bases_dims_chk` | `CHECK ((embedding_dimensions = 1536))` |
| `labels` | `labels_title_format_chk` | `CHECK (((title)::text ~ '^[a-z0-9_-]+$'::text))` |
| `macros` | `macros_actions_is_array` | `CHECK ((jsonb_typeof(actions) = 'array'::text))` |
| `macros` | `macros_visibility_chk` | `CHECK (((visibility)::text = ANY ((ARRAY['personal'::character varying, 'global'::character varying])::text[])))` |
| `messages` | `messages_content_len_chk` | `CHECK (((content IS NULL) OR (char_length(content) <= 150000)))` |
| `messages` | `messages_private_outgoing_chk` | `CHECK (((NOT is_private) OR (direction = 'outgoing'::message_direction)))` |
| `messages` | `messages_sender_chk` | `CHECK ((((sender_kind = 'user'::sender_kind) AND (sender_user_id IS NOT NULL)) OR ((sender_kind = 'contact'::sender_kind) AND (sender_contact_id IS NOT NULL)) OR ((sender_kind = 'ai_agent'::sender_kind) AND (sender_ai_agent_id IS NOT NULL)) OR (sender_kind = ANY (ARRAY['system'::sender_kind, 'automation'::sender_kind]))))` |
| `organization_memberships` | `org_memberships_custom_role_agent_chk` | `CHECK (((custom_role_id IS NULL) OR (role = 'agent'::membership_role)))` |
| `organizations` | `organizations_settings_is_object` | `CHECK ((jsonb_typeof(settings) = 'object'::text))` |
| `reporting_events` | `reporting_events_name_chk` | `CHECK (((name)::text = ANY ((ARRAY['first_response'::character varying, 'reply_time'::character varying, 'conversation_resolved'::character varying, 'conversation_opened'::character varying, 'conversation_bot_handoff'::character varying, 'conversation_bot_resolved'::character varying])::text[])))` |
| `stored_files` | `stored_files_backend_chk` | `CHECK (((storage_backend)::text = ANY ((ARRAY['local'::character varying, 's3'::character varying])::text[])))` |
| `stored_files` | `stored_files_scan_chk` | `CHECK (((scan_status)::text = ANY ((ARRAY['pending'::character varying, 'clean'::character varying, 'infected'::character varying, 'skipped'::character varying])::text[])))` |
| `stored_files` | `stored_files_size_chk` | `CHECK ((byte_size >= 0))` |
| `subscription_plans` | `subscription_plans_interval_chk` | `CHECK (((billing_interval)::text = ANY ((ARRAY['month'::character varying, 'year'::character varying])::text[])))` |
| `subscription_plans` | `subscription_plans_limits_is_object` | `CHECK ((jsonb_typeof(limits) = 'object'::text))` |
| `subscription_plans` | `subscription_plans_price_chk` | `CHECK ((price_cents >= 0))` |
| `subscriptions` | `subscriptions_period_chk` | `CHECK ((current_period_end > current_period_start))` |
| `subscriptions` | `subscriptions_seats_chk` | `CHECK ((seats >= 1))` |
| `usage_records` | `usage_records_metric_chk` | `CHECK (((metric)::text = ANY ((ARRAY['ai_responses'::character varying, 'ai_tokens'::character varying, 'conversations'::character varying, 'storage_bytes'::character varying])::text[])))` |
| `users` | `users_ui_settings_is_object` | `CHECK ((jsonb_typeof(ui_settings) = 'object'::text))` |
| `webhook_subscriptions` | `webhook_subscriptions_https_chk` | `CHECK ((url ~ '^https://'::text))` |
| `whatsapp_message_templates` | `whatsapp_templates_components_is_array` | `CHECK ((jsonb_typeof(components) = 'array'::text))` |
| `workspaces` | `workspaces_auto_resolve_chk` | `CHECK (((auto_resolve_after_minutes IS NULL) OR ((auto_resolve_after_minutes >= 10) AND (auto_resolve_after_minutes <= 1439856))))` |
