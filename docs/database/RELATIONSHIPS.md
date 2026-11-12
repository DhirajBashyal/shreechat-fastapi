# Relationships and Cascade Rules

Every foreign key in the schema, **exported from the tested database** (`pg_constraint`), with its delete behaviour. Diagrams: [../08_DATABASE_RELATIONSHIPS.md](../08_DATABASE_RELATIONSHIPS.md).

## Cascade policy

| Rule | Used when | Examples |
|---|---|---|
| `CASCADE` | The child has no meaning without the parent | `messages` → `conversations`; `kb_chunks` → `kb_documents`; everything → `workspaces` / `organizations` |
| `SET NULL` | History must survive the parent's removal | `conversations.assignee_id` → `users`; `messages.sender_user_id`; `created_by` columns |
| `SET NULL (column)` | Composite tenant FK where only the id column should be cleared (keeps `workspace_id`) | `conversations (workspace_id, team_id)` → `teams`; `messages (workspace_id, in_reply_to_id)` |
| `RESTRICT` | Deleting the parent would corrupt billing | `subscriptions.plan_id` → `subscription_plans` |

**Deleting tenants.** `ON DELETE CASCADE` from `organizations` and `workspaces` is a safety net, not the deletion mechanism. A single `DELETE FROM organizations` on a large tenant would lock and cascade millions of rows in one transaction. The `purge_organization` task deletes children in batches (messages by id range → conversations → contacts → …), then the organization row. Soft delete (`deleted_at`) hides the tenant immediately; the purge runs 30 days later.

**Users.** Users are global. Deleting a user cascades only their memberships, sessions and personal join rows; authored content keeps a `NULL` author. Users are normally soft-deleted and anonymised rather than hard-deleted.

## All foreign keys (149)

| Child table | Columns | Parent table | On delete |
|---|---|---|---|
| `ai_agent_inboxes` | `workspace_id, ai_agent_id` | `ai_agents` | CASCADE |
| `ai_agent_inboxes` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `ai_agent_inboxes` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `ai_agent_knowledge_bases` | `workspace_id, ai_agent_id` | `ai_agents` | CASCADE |
| `ai_agent_knowledge_bases` | `workspace_id, knowledge_base_id` | `knowledge_bases` | CASCADE |
| `ai_agent_knowledge_bases` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `ai_agents` | `organization_id, provider_credential_id` | `ai_provider_credentials` | SET NULL (provider_credential_id) |
| `ai_agents` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `ai_conversation_sessions` | `workspace_id, ai_agent_id` | `ai_agents` | CASCADE |
| `ai_conversation_sessions` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `ai_conversation_sessions` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `ai_provider_credentials` | `organization_id` | `organizations` | CASCADE |
| `ai_provider_credentials` | `created_by` | `users` | SET NULL |
| `ai_runs` | `workspace_id, session_id` | `ai_conversation_sessions` | CASCADE |
| `ai_runs` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `ai_runs` | `workspace_id, reply_message_id` | `messages` | SET NULL (reply_message_id) |
| `ai_runs` | `workspace_id, trigger_message_id` | `messages` | SET NULL (trigger_message_id) |
| `ai_runs` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `ai_tool_calls` | `run_id` | `ai_runs` | CASCADE |
| `api_access_tokens` | `organization_id` | `organizations` | CASCADE |
| `api_access_tokens` | `user_id` | `users` | CASCADE |
| `attachments` | `workspace_id, message_id` | `messages` | CASCADE |
| `attachments` | `organization_id, file_id` | `stored_files` | SET NULL (file_id) |
| `attachments` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `audit_logs` | `organization_id` | `organizations` | CASCADE |
| `automation_rule_executions` | `workspace_id, rule_id` | `automation_rules` | CASCADE |
| `automation_rule_executions` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `automation_rule_executions` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `automation_rules` | `created_by` | `users` | SET NULL |
| `automation_rules` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `billing_records` | `organization_id` | `organizations` | CASCADE |
| `billing_records` | `subscription_id` | `subscriptions` | SET NULL |
| `canned_responses` | `created_by` | `users` | SET NULL |
| `canned_responses` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `channel_connections` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `channel_connections` | `connected_by` | `users` | SET NULL |
| `channel_connections` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `channel_routes` | `channel_connection_id` | `channel_connections` | CASCADE |
| `channel_routes` | `organization_id` | `organizations` | CASCADE |
| `channel_routes` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `contact_identities` | `workspace_id, contact_id` | `contacts` | CASCADE |
| `contact_identities` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `contact_identities` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `contact_labels` | `workspace_id, contact_id` | `contacts` | CASCADE |
| `contact_labels` | `workspace_id, label_id` | `labels` | CASCADE |
| `contact_labels` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `contacts` | `organization_id, avatar_file_id` | `stored_files` | SET NULL (avatar_file_id) |
| `contacts` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `conversation_assignments` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `conversation_assignments` | `workspace_id, team_id` | `teams` | SET NULL (team_id) |
| `conversation_assignments` | `assigned_by_user_id` | `users` | SET NULL |
| `conversation_assignments` | `assignee_id` | `users` | SET NULL |
| `conversation_assignments` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `conversation_labels` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `conversation_labels` | `workspace_id, label_id` | `labels` | CASCADE |
| `conversation_labels` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `conversation_participants` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `conversation_participants` | `user_id` | `users` | CASCADE |
| `conversation_participants` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `conversations` | `workspace_id, ai_agent_id` | `ai_agents` | SET NULL (ai_agent_id) |
| `conversations` | `workspace_id, contact_identity_id` | `contact_identities` | CASCADE |
| `conversations` | `workspace_id, contact_id` | `contacts` | CASCADE |
| `conversations` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `conversations` | `workspace_id, team_id` | `teams` | SET NULL (team_id) |
| `conversations` | `assignee_id` | `users` | SET NULL |
| `conversations` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `custom_attribute_definitions` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `event_outbox` | `organization_id` | `organizations` | CASCADE |
| `facebook_pages` | `workspace_id, channel_connection_id` | `channel_connections` | CASCADE |
| `facebook_pages` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `idempotency_keys` | `organization_id` | `organizations` | CASCADE |
| `inbound_webhook_events` | `organization_id` | `organizations` | CASCADE |
| `inbox_members` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `inbox_members` | `user_id` | `users` | CASCADE |
| `inbox_members` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `inboxes` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `instagram_accounts` | `workspace_id, channel_connection_id` | `channel_connections` | CASCADE |
| `instagram_accounts` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `invitations` | `organization_id` | `organizations` | CASCADE |
| `invitations` | `organization_id, custom_role_id` | `roles` | SET NULL (custom_role_id) |
| `invitations` | `invited_by` | `users` | SET NULL |
| `kb_chunks` | `workspace_id, document_id` | `kb_documents` | CASCADE |
| `kb_chunks` | `workspace_id, knowledge_base_id` | `knowledge_bases` | CASCADE |
| `kb_chunks` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `kb_documents` | `workspace_id, knowledge_base_id` | `knowledge_bases` | CASCADE |
| `kb_documents` | `organization_id, file_id` | `stored_files` | SET NULL (file_id) |
| `kb_documents` | `created_by` | `users` | SET NULL |
| `kb_documents` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `knowledge_bases` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `labels` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `macros` | `created_by` | `users` | SET NULL |
| `macros` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `mentions` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `mentions` | `workspace_id, message_id` | `messages` | CASCADE |
| `mentions` | `user_id` | `users` | CASCADE |
| `mentions` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `messages` | `workspace_id, sender_ai_agent_id` | `ai_agents` | SET NULL (sender_ai_agent_id) |
| `messages` | `workspace_id, sender_contact_id` | `contacts` | SET NULL (sender_contact_id) |
| `messages` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `messages` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `messages` | `workspace_id, in_reply_to_id` | `messages` | SET NULL (in_reply_to_id) |
| `messages` | `sender_user_id` | `users` | SET NULL |
| `messages` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `notes` | `workspace_id, contact_id` | `contacts` | CASCADE |
| `notes` | `author_id` | `users` | SET NULL |
| `notes` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `notifications` | `workspace_id, conversation_id` | `conversations` | CASCADE |
| `notifications` | `workspace_id, message_id` | `messages` | CASCADE |
| `notifications` | `user_id` | `users` | CASCADE |
| `notifications` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `organization_memberships` | `organization_id` | `organizations` | CASCADE |
| `organization_memberships` | `organization_id, custom_role_id` | `roles` | SET NULL (custom_role_id) |
| `organization_memberships` | `invited_by` | `users` | SET NULL |
| `organization_memberships` | `user_id` | `users` | CASCADE |
| `organizations` | `created_by` | `users` | SET NULL |
| `platform_admins` | `granted_by` | `users` | SET NULL |
| `platform_admins` | `user_id` | `users` | CASCADE |
| `reporting_events` | `organization_id` | `organizations` | CASCADE |
| `reporting_events` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `role_permissions` | `permission_code` | `permissions` | CASCADE |
| `role_permissions` | `organization_id, role_id` | `roles` | CASCADE |
| `roles` | `organization_id` | `organizations` | CASCADE |
| `stored_files` | `organization_id` | `organizations` | CASCADE |
| `subscriptions` | `organization_id` | `organizations` | CASCADE |
| `subscriptions` | `plan_id` | `subscription_plans` | RESTRICT |
| `team_members` | `workspace_id, team_id` | `teams` | CASCADE |
| `team_members` | `user_id` | `users` | CASCADE |
| `team_members` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `teams` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `usage_records` | `organization_id` | `organizations` | CASCADE |
| `user_sessions` | `replaced_by_id` | `user_sessions` | SET NULL |
| `user_sessions` | `user_id` | `users` | CASCADE |
| `users` | `avatar_file_id` | `stored_files` | SET NULL |
| `web_widgets` | `workspace_id, channel_connection_id` | `channel_connections` | CASCADE |
| `web_widgets` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `webhook_deliveries` | `outbox_event_id` | `event_outbox` | SET NULL |
| `webhook_deliveries` | `workspace_id, subscription_id` | `webhook_subscriptions` | CASCADE |
| `webhook_deliveries` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `webhook_subscriptions` | `workspace_id, inbox_id` | `inboxes` | CASCADE |
| `webhook_subscriptions` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `whatsapp_business_accounts` | `organization_id` | `organizations` | CASCADE |
| `whatsapp_message_templates` | `organization_id, waba_id` | `whatsapp_business_accounts` | CASCADE |
| `whatsapp_phone_numbers` | `workspace_id, channel_connection_id` | `channel_connections` | CASCADE |
| `whatsapp_phone_numbers` | `organization_id, waba_id` | `whatsapp_business_accounts` | CASCADE |
| `whatsapp_phone_numbers` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `workspace_counters` | `workspace_id` | `workspaces` | CASCADE |
| `workspace_memberships` | `organization_id, membership_id` | `organization_memberships` | CASCADE |
| `workspace_memberships` | `organization_id, workspace_id` | `workspaces` | CASCADE |
| `workspaces` | `organization_id` | `organizations` | CASCADE |
