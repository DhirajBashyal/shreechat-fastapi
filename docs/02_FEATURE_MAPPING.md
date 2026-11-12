# 02 — Feature Mapping

How each Chatwoot capability maps to a Converse module, and which release it belongs to. File references for the Chatwoot side are in [01_CHATWOOT_ANALYSIS.md](01_CHATWOOT_ANALYSIS.md).

**Release legend**

| Tag | Meaning |
|---|---|
| **MVP** | Needed for the first usable version: one organization can connect website chat + Messenger + WhatsApp, agents reply in real time |
| **R1** | First release after MVP: Instagram, automation, AI agent + RAG, webhooks, reports |
| **R2** | Billing, custom roles, macros, copilot suggestions, audit UI |
| **Future** | Designed for (tables or adapter contract allow it) but not scheduled |

## 1. Feature table

| # | Capability | Chatwoot behaviour (verified) | Converse module | Release | Changes from Chatwoot and why |
|---|---|---|---|---|---|
| 1 | Organizations | `accounts` tenant | `converse.modules.tenancy` | MVP | Two levels: **organization** (billing + security boundary) and **workspace** (operational unit: inboxes, contacts, conversations). An agency or multi-brand company needs several isolated inboxes under one bill |
| 2 | Workspaces | Not present | `tenancy` | MVP | New. Every operational row carries `organization_id` **and** `workspace_id` |
| 3 | Users & login | Devise token auth, `users.tokens` json | `converse.modules.auth` | MVP | Stateless JWT access token + server-side rotating refresh tokens with reuse detection. No growing token blob on the user row |
| 4 | MFA | `devise-two-factor` TOTP | `auth` | R1 | Same idea (TOTP + backup codes); secret encrypted |
| 5 | Roles | `agent`, `administrator`; Enterprise custom roles | `converse.modules.access` | MVP (fixed roles), R2 (custom roles) | `owner` added (cannot be removed, holds billing). Permissions are a catalog table rather than a code constant |
| 6 | Teams | `teams`, `team_members` | `converse.modules.teams` | R1 | Same |
| 7 | Inboxes | `inboxes` + polymorphic `channel_*` | `converse.modules.inboxes` | MVP | Polymorphic link replaced by `channel_connections` (1:1 with inbox, proper FK) plus per-provider detail tables |
| 8 | Channel credentials | Some encrypted (`encrypts`), some plaintext (FB page tokens, WhatsApp `provider_config`) | `converse.core.crypto` | MVP | **All** secrets encrypted with versioned keys (`*_ciphertext`, `*_key_version`) |
| 9 | Webhook routing | Lookup by URL phone number / payload metadata / page id | `converse.channels.routing` | MVP | One global `channel_routes` table keyed by `(provider, external_id)`, read before the tenant is known |
| 10 | Website chat | `Channel::WebWidget`, `/api/v1/widget`, SDK + iframe | `converse.channels.web_widget` + `widget-sdk/` | MVP | Same shape. Visitor JWT shortened to 30 days and refreshed silently. Origin allow-list enforced on REST and WebSocket |
| 11 | Facebook Messenger | `facebook-messenger` gem at `/bot`; echo delay 2 s | `converse.channels.meta.messenger` | MVP | Echo handling by `external_id` uniqueness + `client_message_id` instead of a fixed 2 s delay |
| 12 | Instagram | Two paths (via FB Page, via Instagram Login) | `converse.channels.meta.instagram` | R1 | **Instagram Login only** for new connections (Meta's current path). The FB Page path is documented as Future |
| 13 | WhatsApp Cloud | Cloud + 360dialog providers, Embedded Signup, templates, coexistence echoes, BSUID | `converse.channels.meta.whatsapp` | MVP | Cloud API only. 360dialog is a Future adapter. Embedded Signup is R1, manual setup (token + phone number id) is MVP |
| 14 | WhatsApp templates | Synced into jsonb on the channel | `whatsapp_message_templates` table | MVP | Normalised table, so templates are queryable and shared by numbers in the same WABA |
| 15 | Contacts | `contacts` + `contact_inboxes` | `converse.modules.contacts` | MVP | Same model (`contact_identities`). Race handled with `INSERT … ON CONFLICT DO NOTHING RETURNING` |
| 16 | Contact merge | `contact.merged` event | `contacts` | R1 | Explicit merge service: moves identities, conversations, notes, labels |
| 17 | Conversations | Model callbacks, status enum, per-account sequences | `converse.modules.conversations` | MVP | Explicit `ConversationService.transition()`; numbering with `workspace_counters` instead of one sequence per tenant |
| 18 | Messages & notes | `messages` with `private`, `message_type` | `converse.modules.messaging` | MVP | Same semantics. `sequence_number` gives a stable total order. `client_message_id` makes retries idempotent |
| 19 | Attachments | ActiveStorage, direct uploads | `converse.modules.files` + `converse.storage` | MVP | Presigned S3 uploads; MIME sniffing; size limits per plan; scan status column |
| 20 | Voice messages | Audio attachments | `files` | MVP | `attachment_kind = 'audio'`; WhatsApp voice notes are `audio` with `meta.voice = true` |
| 21 | Delivery status | `messages.status`, `StatusUpdateService` | `messaging` | MVP | Monotonic: status never moves backwards (`read` can't become `delivered`) |
| 22 | Real-time | ActionCable, `pubsub_token`, presence | `converse.realtime` | MVP | Native WebSockets + Redis pub/sub. Same token-per-user and token-per-contact idea |
| 23 | Assignment | Round robin (Redis list), rate limiter | `converse.modules.assignment` | R1 (MVP: manual only) | Round robin computed from DB + Redis presence; capacity limits per agent |
| 24 | Labels | `acts-as-taggable-on` | `converse.modules.labels` | R1 | Plain join tables with FKs |
| 25 | Custom attributes | Definitions + jsonb | `converse.modules.attributes` | R1 | Values validated against definitions at write time |
| 26 | Canned responses | `canned_responses` | `converse.modules.canned_responses` | R1 | Same; supports `{{contact.name}}` style variables (Jinja2 sandbox) |
| 27 | Contact notes | `notes` | `contacts` | R2 | Same |
| 28 | Automation | `automation_rules`, listener, condition/action services | `converse.modules.automation` | R1 | Typed conditions/actions (Pydantic). Execution log with once-per-event uniqueness. Same loop guard |
| 29 | Macros | `macros` | `automation` | R2 | Reuses the automation action executor |
| 30 | Outgoing webhooks | `webhooks` + `WebhookJob`, HMAC header | `converse.modules.webhooks` | R1 | Delivery log, exponential retry, auto-disable after N failures, replay endpoint |
| 31 | API tokens | `access_tokens` (plaintext token) | `auth` | R1 | Hashed tokens with visible prefix; scopes; expiry |
| 32 | External bots | `agent_bots` webhooks | `webhooks` + `auth` | R2 | A bot is an API token + webhook subscription. Same capability, fewer concepts |
| 33 | AI agent | Captain (Enterprise) | `converse.ai` | R1 | LangGraph graph; tenant-configurable provider/model/prompt/tools; explicit handoff |
| 34 | Knowledge base | Captain documents → FAQs → embeddings | `converse.modules.knowledge` + `converse.ai.rag` | R1 | Chunk-level retrieval with hybrid (vector + full-text) search; FAQ generation optional |
| 35 | Agent assist | Copilot (Enterprise) | `converse.ai` (`assist_only` mode) | R2 | The same graph produces a suggestion instead of sending |
| 36 | Reports | `reporting_events`, v2 summary reports | `converse.modules.analytics` | R1 | Same event types; computed by an outbox handler |
| 37 | Billing | Stripe in Enterprise | `converse.modules.billing` | R2 | Provider-agnostic interface, Stripe first; plan limits enforced in services |
| 38 | Audit log | `audited` gem | `converse.modules.audit` | R1 (write), R2 (UI) | Explicit audit calls in services for security-relevant actions |
| 39 | Notifications | `notifications`, email, web push | `converse.modules.notifications` | R1 | In-app over WebSocket first; email and push are Future |
| 40 | Platform admin | Super Admin (Administrate) | `converse.modules.platform` | R1 | API-only admin endpoints; UI later |
| 41 | Email channel | IMAP/SMTP/ActionMailbox | `converse.channels.email` | Future | Adapter contract supports it (threading via `external_thread_id`) |
| 42 | Help center | Portals/articles | — | Future | KB documents cover the AI use case |
| 43 | Campaigns, CSAT, SLA | Dedicated modules | — | Future | |
| 44 | Other channels | LINE, Telegram, SMS, TikTok, X, Twilio | `converse.channels.*` | Future | Each is a new adapter; no core changes needed |

## 2. What stays the same on purpose

These Chatwoot design choices are sound and are kept:

1. **Shared schema with a tenant column on every row**, including denormalised `organization_id`/`workspace_id` on `messages` and `attachments`, so hot queries never need joins to find the tenant.
2. **Contact vs contact identity split.** One person can write from WhatsApp and the website and still be one contact.
3. **Private notes are messages** (`is_private`), so they appear in the timeline in order.
4. **Activity messages** (`direction = 'activity'`) record "Assigned to Arjun", "Resolved by AI", etc. in the timeline.
5. **Acknowledge webhooks fast, process in a worker.**
6. **Per-contact serialization for inbound processing** (Chatwoot's Redis mutex). Converse keeps the goal and changes the mechanism (see [backend/MESSAGE_PROCESSING.md](backend/MESSAGE_PROCESSING.md)).
7. **Discard an AI reply when a newer customer message arrived during generation.**

## 3. What changes on purpose

| Chatwoot | Problem | Converse |
|---|---|---|
| Side effects in ActiveRecord callbacks | Hard to trace; easy to trigger by accident in scripts | Explicit service methods; events written to an outbox in the same transaction |
| `after_commit` → enqueue | Event lost if the process dies between commit and enqueue | Transactional outbox + relay |
| Few database FKs | Orphans possible; integrity depends on app code | FKs everywhere, including composite tenant FKs `(workspace_id, x_id)` |
| No RLS | One unscoped query leaks tenant data | RLS enforced for the app DB role |
| One PostgreSQL sequence per account for `display_id` | Thousands of sequence objects; awkward backups | `workspace_counters` row + `next_workspace_counter()` |
| Plaintext provider tokens in some tables | Database dump leaks Meta tokens | Envelope-encrypted columns with key versions |
| Integer ids in URLs | Enumerable | UUIDs (v4 now, v7 when Postgres 18 is the baseline). Human-friendly numbers only via `display_number` |
| Webhook delivery without a log | Customers can't see failures; no replay | `webhook_deliveries` with retries and replay |
| Echo handling with fixed 2 s delays | Timing-dependent | Idempotent writes keyed by provider message id |
