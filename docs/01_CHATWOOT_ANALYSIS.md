# 01 — Chatwoot Codebase Analysis

This document describes how Chatwoot actually works, based on reading the source in this repository at commit `625d5964e3` (branch `develop`). Every file path below was opened during the analysis. Where something was not inspected, the [analysis checklist](#analysis-checklist) says so.

---

## 1. Languages and frameworks

| Layer | Technology | Evidence |
|---|---|---|
| Backend language | Ruby 3.4.4 | `.ruby-version`, `Gemfile` (`ruby '3.4.4'`) |
| Web framework | Rails 7.2.3.1 (full stack, API controllers + server-rendered dashboards) | `Gemfile` |
| Database | PostgreSQL with `pg_trgm`, `pgcrypto`, `vector` (pgvector) | `db/schema.rb` (`enable_extension "vector"`), `docker-compose.production.yaml` uses `pgvector/pgvector:pg16` |
| Cache / locks / queues | Redis (`redis`, `redis-namespace`) | `Gemfile`, `lib/redis/` |
| Background jobs | Sidekiq 7.3 + `sidekiq-cron` | `Gemfile`, `config/sidekiq.yml`, `config/schedule.yml` |
| Realtime | ActionCable (WebSocket) | `app/channels/room_channel.rb`, `config/initializers/actioncable.rb` |
| Auth | Devise, `devise_token_auth`, `devise-two-factor`, `jwt` | `Gemfile`, `config/routes.rb` line 3 (`mount_devise_token_auth_for 'User'`) |
| Authorization | Pundit | `app/policies/*.rb` |
| In-process events | Wisper pub/sub (`wisper 2.0.0`) | `app/dispatchers/*.rb` |
| File storage | ActiveStorage (S3, GCS, Azure, local) | `Gemfile` (`aws-sdk-s3`, `google-cloud-storage`, `azure-blob`) |
| Search | `pg_trgm` GIN indexes; optional OpenSearch via Searchkick | `db/schema.rb`, `config/initializers/searchkick.rb` |
| Meta SDKs | `facebook-messenger` gem (Messenger webhooks), `koala` (Graph API) | `Gemfile`, `config/initializers/facebook_messenger.rb` |
| LLM | `ruby_llm`, `ai-agents` gem; models configured in `config/llm.yml` | `Gemfile`, `config/llm.yml` |
| Vector search | `neighbor` + `pgvector` gems | `Gemfile` |
| Billing | Stripe (`stripe ~> 18.0`), Enterprise only | `enterprise/app/services/enterprise/billing/` |
| Audit | `audited` gem | `Gemfile`, `config/initializers/audited.rb` |
| Rate limiting | `rack-attack` | `config/initializers/rack_attack.rb` |
| Observability | Sentry, Scout APM, Datadog, OpenTelemetry, Lograge | `Gemfile`, `config/initializers/` |
| Frontend | Vue 3.5, Vuex 4 + Pinia 3, Vite 6, Tailwind 3 | `package.json` |
| Frontend realtime client | `@rails/actioncable` 6.1.3 | `package.json`, `app/javascript/dashboard/helper/actionCable.js` |

## 2. Backend architecture

Chatwoot is a **Rails modular monolith** run as two process types: Puma web servers (`rails s`) and Sidekiq workers (`sidekiq -C config/sidekiq.yml`), sharing one PostgreSQL and one Redis (`docker-compose.production.yaml`).

Code is organised by Rails role, not by business domain:

| Directory | Role | Examples |
|---|---|---|
| `app/controllers/` | HTTP entry points. `api/v1/accounts/*` is the agent dashboard API, `api/v1/widget/*` is the website widget API, `public/api/v1/*` is the client API, `platform/*` is the platform API, `webhooks/*` receives provider webhooks | `app/controllers/api/v1/accounts/conversations_controller.rb`, `app/controllers/webhooks/whatsapp_controller.rb` |
| `app/models/` | ActiveRecord models with associations, validations and **lifecycle callbacks that emit events** | `app/models/message.rb`, `app/models/conversation.rb` |
| `app/models/channel/` | One model per channel type (polymorphic `inboxes.channel`) | `channel/whatsapp.rb`, `channel/facebook_page.rb`, `channel/instagram.rb`, `channel/web_widget.rb` |
| `app/builders/` | Multi-record creation (contact + contact inbox, conversation, message) | `contact_inbox_with_contact_builder.rb`, `conversation_builder.rb`, `messages/message_builder.rb` |
| `app/services/` | Business logic and provider clients, grouped per provider | `services/whatsapp/`, `services/instagram/`, `services/facebook/` |
| `app/jobs/` | Sidekiq jobs | `webhooks/whatsapp_events_job.rb`, `send_reply_job.rb` |
| `app/dispatchers/` + `app/listeners/` | Event bus (Wisper) | `dispatchers/dispatcher.rb`, `listeners/automation_rule_listener.rb` |
| `app/policies/` | Pundit authorization | `policies/conversation_policy.rb` |
| `app/finders/` | Query objects for list views | `finders/` |
| `lib/` | Framework-independent helpers: event names, Redis keys, webhook delivery, safe HTTP fetch, global config | `lib/events/types.rb`, `lib/webhooks/trigger.rb`, `lib/safe_fetch.rb` |
| `enterprise/` | Paid features injected with `prepend_mod_with` / `include_mod_with` | `enterprise/app/services/captain/`, `enterprise/app/models/custom_role.rb` |

**Key observation:** the conversation pipeline relies heavily on ActiveRecord callbacks. Creating a `Message` triggers `after_create_commit :execute_after_create_commit_callbacks` (`app/models/message.rb:141`), which reopens the conversation, dispatches `message.created` and enqueues `SendReplyJob` (`message.rb:415-418`). This keeps controllers thin. The cost is that side effects are implicit and hard to follow. The Python design makes these side effects explicit in a service layer (see [backend/MESSAGE_PROCESSING.md](backend/MESSAGE_PROCESSING.md)).

## 3. Frontend architecture

| App | Path | Purpose |
|---|---|---|
| Agent dashboard | `app/javascript/dashboard/` | Vue 3 SPA. Vuex modules in `store/modules/` (one per resource: `conversations`, `contacts`, `inboxes`, `labels`, `teams`, `automations`, …) with newer Pinia stores in `stores/`. API wrappers live in `dashboard/api/` |
| Website widget | `app/javascript/widget/` | Vue app rendered inside an iframe on customer sites |
| Widget SDK loader | `app/javascript/sdk/` | Small script (`IFrameHelper.js`, `bubbleHelpers.js`, `cookieHelpers.js`) that injects the iframe and exposes `window.$chatwoot` |
| Help center portal | `app/javascript/portal/` | Public knowledge-base pages |
| Super admin | `app/javascript/superadmin/`, `app/dashboards/` | Installation-level admin (Administrate gem) |
| Design system | `app/javascript/design-system/`, `components-next/` | Shared components |

The dashboard talks to the backend over REST (`/api/v1/accounts/:account_id/...`) and receives push events over ActionCable. `app/javascript/dashboard/helper/actionCable.js` maps event names to store mutations (`message.created`, `conversation.status_changed`, `assignee.changed`, `conversation.typing_on`, `presence.update`, `notification.created`, …).

## 4. Database structure

`db/schema.rb` defines **111 tables**. The core domain:

| Table | Purpose | Key columns |
|---|---|---|
| `accounts` | Tenant | `name`, `status`, `feature_flags` (bitmask, `bigint`), `limits` jsonb, `settings` jsonb, `custom_attributes` |
| `users` | Agents/admins (global, can belong to many accounts) | `email`, `encrypted_password`, `tokens` (devise_token_auth), `pubsub_token`, `otp_secret` |
| `account_users` | Membership + role | `account_id`, `user_id`, `role` (agent=0, administrator=1), `availability`, `custom_role_id` (Enterprise). Unique `(account_id, user_id)` |
| `inboxes` | A channel instance inside an account | `account_id`, polymorphic `channel_type` + `channel_id`, assignment/greeting/working-hours/CSAT settings |
| `channel_*` | Provider-specific config (one table per provider) | e.g. `channel_whatsapp.provider_config` jsonb, `channel_facebook_pages.page_access_token`, `channel_instagram.access_token` + `expires_at`, `channel_web_widgets.website_token` + `hmac_token` |
| `contacts` | Customer record, account-scoped | `email`, `phone_number`, `identifier`, `custom_attributes`, `additional_attributes`, `blocked`. Unique `(email, account_id)` and `(identifier, account_id)` |
| `contact_inboxes` | A contact's identity **on one inbox** | `contact_id`, `inbox_id`, `source_id` (PSID / IGSID / wa_id / widget UUID), `pubsub_token`, `hmac_verified`. Unique `(inbox_id, source_id)` |
| `conversations` | Thread between one contact identity and the business | `account_id`, `inbox_id`, `contact_id`, `contact_inbox_id`, `display_id` (per-account sequence), `status` (open/resolved/pending/snoozed), `assignee_id`, `team_id`, `priority`, `snoozed_until`, `waiting_since`, `first_reply_created_at`, `cached_label_list` |
| `messages` | Every message, note and activity | `conversation_id`, `message_type` (incoming/outgoing/activity/template), `content_type`, `private`, `status` (sent/delivered/read/failed), `source_id` (provider id), polymorphic `sender`, `content_attributes` json |
| `attachments` | Media on a message | `message_id`, `file_type`, `external_url`, coordinates, plus ActiveStorage blob |
| `teams`, `team_members`, `inbox_members` | Assignment scopes | |
| `labels` + `taggings`/`tags` | Labels via `acts-as-taggable-on` | |
| `custom_attribute_definitions` | Schema for custom attributes | |
| `canned_responses`, `macros`, `automation_rules` | Productivity and automation | `automation_rules.conditions`/`actions` jsonb |
| `webhooks` | Outgoing webhooks | `url`, `subscriptions` jsonb, `secret` |
| `reporting_events` | Pre-computed metrics (first response, resolution time) | |
| `notifications`, `mentions`, `conversation_participants` | Agent notifications | |
| `captain_*`, `article_embeddings` | AI assistant, documents, FAQ with `vector(1536)` embeddings (Enterprise) | |

**Notable design facts**

1. **Almost no database foreign keys on core tables.** `db/schema.rb` declares only 18 `add_foreign_key` lines, all on newer or Enterprise tables (`campaign_recipients`, `conversation_monitors`, `user_sessions`, …). Integrity between `conversations`, `messages`, `contacts` and `inboxes` relies on ActiveRecord `belongs_to` and on `dependent: :destroy_async`.
2. **Tenant column everywhere.** `account_id` is denormalised onto `messages`, `attachments`, `conversations`, `contacts`, `inboxes`, etc., so every hot query can filter by tenant without joins.
3. **Per-account human-readable numbers through database triggers.** `accounts_after_insert_row_tr` creates a PostgreSQL sequence `conv_dpid_seq_<account_id>` for every new account, and `conversations_before_insert_row_tr` sets `display_id := nextval(...)` (`db/schema.rb` lines 1875-1887). That means one sequence object per tenant.
4. **Integer primary keys** (`serial`/`bigserial`). `conversations` also has a `uuid` column for public references.
5. **Heavy JSONB use** for flexible data: `additional_attributes`, `custom_attributes`, `content_attributes`, `provider_config`, `settings`, `limits`.
6. **Encryption at the attribute level** with Rails `encrypts`: `channel/instagram.rb` (`access_token`), `channel/telegram.rb`, `channel/email.rb` (IMAP/SMTP passwords), `user.rb` (`otp_secret`). Some credentials are not encrypted, for example `channel_facebook_pages.page_access_token` and the `channel_whatsapp.provider_config` jsonb.

## 5. Authentication and authorization

**Agents (dashboard):** `devise_token_auth`. A login returns `access-token`, `client`, `uid` headers. Tokens are stored per user in the `users.tokens` json column. `app/controllers/concerns/access_token_auth_helper.rb` checks those three header values.

**API clients:** an `api_access_token` header maps to `access_tokens` rows (owner is a User or AgentBot). `Api::V1::Accounts::BaseController` (`app/controllers/api/v1/accounts/base_controller.rb`) rejects token access unless the account has `api_and_webhooks_enabled`.

**Website visitors:** `Widget::TokenService` (`app/services/widget/token_service.rb`) issues a JWT holding `source_id` and `inbox_id`, valid for 180 days by default. The widget sends it in the `X-Auth-Token` header. `WebsiteTokenHelper#set_contact` (`app/controllers/concerns/website_token_helper.rb`) resolves the `contact_inbox` from `source_id`. Optional HMAC identity verification uses `channel_web_widgets.hmac_token` (see `app/controllers/concerns/hmac_concern.rb`).

**MFA:** `devise-two-factor` (TOTP, `users.otp_secret`, backup codes). `MfaEnforcementGuard` can enforce MFA per account.

**Authorization:** Pundit policies. Example `ConversationPolicy#show?`: administrator, or agent bot, or an agent who is a member of the inbox **or** of the conversation's team (`app/policies/conversation_policy.rb`). The roles are `agent` and `administrator` (`AccountUser` enum). The Enterprise `CustomRole` adds granular permissions: `conversation_manage`, `conversation_unassigned_manage`, `conversation_participating_manage`, `contact_manage`, `report_manage`, `knowledge_base_manage` (`enterprise/app/models/custom_role.rb`).

**Rate limiting:** `rack-attack` throttles per IP (default 3000/min), login per IP and email (5 per 5 min / 10 per 15 min), password reset, MFA attempts, account signup, and widget endpoints (`config/initializers/rack_attack.rb` lines 144-278).

## 6. Multi-tenancy

- **Model:** shared database, shared schema, `account_id` discriminator column. No PostgreSQL row-level security.
- **Scoping:** routes are nested under `/api/v1/accounts/:account_id/`. `EnsureCurrentAccountHelper#current_account` (`app/controllers/concerns/ensure_current_account_helper.rb`) loads the account, rejects suspended accounts, checks that the user has an `account_users` row (or that the bot belongs to the account), and sets `Current.account` / `Current.account_user`.
- **Risk:** isolation depends on every query going through `Current.account.<association>`. One unscoped `Model.find(params[:id])` would leak data across tenants. The Python design adds database-enforced RLS as a second layer ([09_MULTI_TENANCY.md](09_MULTI_TENANCY.md)).
- **Feature flags per account:** bitmask columns `feature_flags`, `feature_flags_ext_1`, defined in `config/features.yml` (`channel_facebook`, `automations`, `agent_bots`, `help_center`, …).

## 7. Inbox management

An **inbox** is one connected channel (one Facebook Page, one WhatsApp number, one website). `inboxes.channel_type`/`channel_id` point to the provider row. Members are added through `inbox_members`. Inbox settings cover auto-assignment (`enable_auto_assignment`, `auto_assignment_config`), greetings, working hours (`working_hours` table), out-of-office message, CSAT, `lock_to_single_conversation` and `allow_messages_after_resolved`.

APIs: `resources :inboxes` under accounts (`config/routes.rb:344`), channel creation endpoints such as `register_facebook_page` (`routes.rb:168`), and the `instagram`/`whatsapp` namespaces (`routes.rb:426-447`).

## 8. Conversation lifecycle

`Conversation` (`app/models/conversation.rb`):

- `enum status: { open: 0, resolved: 1, pending: 2, snoozed: 3 }`, `enum priority: { low, medium, high, urgent }`.
- `before_create :determine_conversation_status`: a conversation that starts with an active agent bot is created as `pending` (bot handling). Otherwise it is `open`.
- `bot_handoff!` (line 184) switches a bot-handled conversation to `open` and dispatches `conversation.bot_handoff`.
- `after_update_commit :execute_after_update_commit_callbacks` dispatches `conversation.updated`, `conversation.status_changed`, `assignee.changed` and `team.changed` depending on what changed.
- Incoming messages reopen resolved conversations (`Message#reopen_conversation`, `message.rb:421`), unless the inbox locks to a single conversation.
- Snoozed conversations are reopened by `Conversations::ReopenSnoozedConversationsJob`. Auto-resolve runs through `trigger_conversations_resolution_job` (cron every 5 minutes, `config/schedule.yml`).
- `waiting_since` tracks how long the customer has been waiting. `first_reply_created_at` feeds the first-response metric.

**Auto-assignment:** `AutoAssignment::InboxRoundRobinService` keeps a Redis list per inbox (`lpush`/`lrem` of user ids). `AutoAssignment::AgentAssignmentService` picks an online agent, and `RateLimiter` caps assignments per agent.

## 9. Contact management

Contacts are account-scoped. Identity per channel lives in `contact_inboxes.source_id`. `ContactInboxWithContactBuilder` (`app/builders/contact_inbox_with_contact_builder.rb`):

1. Look up `inbox.contact_inboxes.find_by(source_id:)`.
2. Otherwise, inside a transaction, find an existing contact by identifier/email/phone or create one, then create the `contact_inbox`.
3. On `ActiveRecord::RecordNotUnique` (a concurrent webhook won the race), retry the lookup.

There are also merge (`contact.merged` event), labels, notes (`notes` table), custom attributes, companies, import (`data_imports`) and IP geolocation (`ContactIpLookupJob`).

## 10. Message management

`Message` (`app/models/message.rb`):

- `message_type`: `incoming` (from the customer), `outgoing` (agent/bot), `activity` (system events such as "assigned to X"), `template`.
- `private: true` = internal note. Notes are never sent to the channel (`Base::SendOnChannelService#invalid_message?`).
- `status`: `sent`, `delivered`, `read`, `failed`. Updated from provider status webhooks via `Messages::StatusUpdateService`.
- `source_id`: provider message id, used for deduplication and reply threading.
- `before_validation :prevent_message_flooding`: a per-conversation rate cap.
- Max content 150,000 characters (`validates :content, length: { maximum: 150_000 }`).
- After commit: reopen conversation → dispatch `message.created` → `send_reply` enqueues `SendReplyJob`, delayed 2 s if attachments exist (`message.rb:415-418`).

**Outbound:** `SendReplyJob` (`app/jobs/send_reply_job.rb`) maps the channel class to a sender service (`Whatsapp::SendOnWhatsappService`, `Instagram::SendOnInstagramService`, `Facebook::SendOnFacebookService`, …). All sender services inherit `Base::SendOnChannelService` (`app/services/base/send_on_channel_service.rb`), which skips private notes, skips messages that came *from* the channel (`source_id` present, to avoid echo loops) and calls `perform_reply`. This is the closest Chatwoot gets to a channel adapter interface: a shared base class for **outbound** only. Inbound parsing is different for every provider.

## 11. Social media integrations

### WhatsApp (Cloud API and 360dialog)

- Routes: `GET/POST /webhooks/whatsapp/:phone_number` (`config/routes.rb:738-739`).
- `Webhooks::WhatsappController` (`app/controllers/webhooks/whatsapp_controller.rb`):
  - `verify`: the Meta challenge. Compares `hub.verify_token` with `channel.provider_config['webhook_verify_token']` (`MetaTokenVerifyConcern#verify`).
  - `process_payload`: `before_action :verify_meta_signature!` checks `X-Hub-Signature-256` as HMAC-SHA256 of the raw body with the app secret, using constant-time comparison (`app/controllers/concerns/meta_token_verify_concern.rb`). It rejects inactive numbers, ignores `tracking_events`-only payloads, enqueues `Webhooks::WhatsappEventsJob` and returns `200` immediately.
- The channel is found from `entry[0].changes[0].value.metadata.phone_number_id` / `display_phone_number` (`Whatsapp::WebhookChannelFinderService`), falling back to the URL phone number.
- `Webhooks::WhatsappEventsJob` (`app/jobs/webhooks/whatsapp_events_job.rb`, queue `low`):
  - Serializes work per `(inbox, sender)` with a 30 s Redis mutex, so album uploads arriving as concurrent webhooks land in one conversation. Retries on `LockAcquisitionError` 20 × 2 s.
  - Separates *message echoes* (`smb_message_echoes`: the business replied from the WhatsApp Business app, "coexistence") from customer messages.
  - Prefers BSUID (`from_user_id`/`parent_user_id`) over the phone number as the sender identity.
- `Whatsapp::IncomingMessageBaseService` (`app/services/whatsapp/incoming_message_base_service.rb`): statuses → `Messages::StatusUpdateService`. Messages → skip reactions/ephemeral → **dedup** by existing `source_id` and an atomic Redis `SET NX EX 1 day` (`Whatsapp::MessageDedupLock`) → find/create contact → skip blocked contacts → in a transaction: find/create conversation and create the message plus attachments.
- Outbound: `Whatsapp::SendOnWhatsappService` → `Whatsapp::Providers::WhatsappCloudService` / `Whatsapp360DialogService`. Templates are synced into `channel_whatsapp.message_templates` jsonb. Onboarding uses Embedded Signup (`Whatsapp::EmbeddedSignupService`, `TokenExchangeService`) or manual setup. `Whatsapp::HealthService` checks number health. `Whatsapp::ReauthorizationService` flags channels that need re-auth.
- Country-specific phone normalisers (`phone_normalizers/brazil_phone_normalizer.rb`, etc.) handle numbers that Meta reports differently from user input.

### Facebook Messenger

- Uses the `facebook-messenger` gem mounted at `/bot` (`config/routes.rb:732`). `ChatwootFbProvider` (`config/initializers/facebook_messenger.rb`) supplies the verify token (`FB_VERIFY_TOKEN`), the per-page app secret (falling back to `FB_APP_SECRET`) and the page access token.
- Event hooks: `:message` and `:postback` → `Webhooks::FacebookEventsJob`. `:message_echo` → the same job **delayed 2 s** (so the echo of a message we just sent arrives after our send API call has stored `source_id`). `:delivery` and `:read` → `Webhooks::FacebookDeliveryJob`.
- `Webhooks::FacebookEventsJob` locks on `(sender_id, recipient_id)` and calls `Integrations::Facebook::MessageCreator` (`lib/integrations/facebook/message_creator.rb`). Echoes of messages sent by Chatwoot are dropped. Echoes of messages an agent sent from the Facebook Page inbox are stored as outgoing messages.
- Outbound: `Facebook::SendOnFacebookService`, with `human_agent_tag_helpers.rb` adding the `HUMAN_AGENT` message tag outside the 24 h window.

### Instagram

Two connection methods exist:

1. **Through a Facebook Page** (legacy): `channel_facebook_pages.instagram_id`. Conversations are marked `additional_attributes.type = 'instagram_direct_message'`, and `SendReplyJob#send_on_facebook_page` routes them to `Instagram::Messenger::SendOnInstagramService`.
2. **Instagram Login** (current): `Channel::Instagram` with an encrypted long-lived `access_token` and `expires_at`. `Instagram::RefreshOauthTokenService` refreshes it.

- Routes: `GET/POST /webhooks/instagram` (`routes.rb:740-741`). `Webhooks::InstagramController` verifies the signature against several possible secrets (`INSTAGRAM_APP_SECRET`, `FB_APP_SECRET`, channel secrets) and delays echo events by 2 s.
- `Webhooks::InstagramEventsJob` locks per `(sender, ig_account)` for only 3 s, and **processes without the lock** once retries run out rather than dropping the event. The code comment says plainly that ActiveJob retries are not FIFO, so lock retries do not preserve ordering.

### Website live chat

- Channel: `Channel::WebWidget` (`website_token`, `hmac_token`, `allowed_domains`, `pre_chat_form_*`, `feature_flags`, `reply_time`).
- SDK loader `app/javascript/sdk/` injects an iframe served by `WidgetsController`.
- Widget REST API under `/api/v1/widget/*` (`routes.rb:556-597`): `config`, `conversations` (create, toggle typing, update last seen, transcript), `messages`, `contact` (`set_user` for identity), `direct_uploads`, `labels`, `events`, `campaigns`.
- Realtime: the widget subscribes to `RoomChannel` with the `contact_inboxes.pubsub_token`.

### Other inspected channel entry points (not in Converse scope)

`webhooks/line`, `webhooks/telegram`, `webhooks/sms`, `webhooks/tiktok`, `webhooks/twitter`, Twilio, Email (IMAP + ActionMailbox). These follow the same pattern: controller → `Webhooks::*EventsJob` → provider service → shared builders.

## 12. Webhook architecture (outgoing)

- `webhooks` table: per-account or per-inbox (`webhook_type`), subscribed event list, optional `secret`.
- `WebhookListener` (async dispatcher) → `WebhookJob` (queue `medium`) → `Webhooks::Trigger.execute` (`lib/webhooks/trigger.rb`).
- Delivery uses `SafeFetch` (SSRF-safe HTTP, `lib/safe_fetch.rb`). Headers: `X-Chatwoot-Delivery`, `X-Chatwoot-Timestamp`, `X-Chatwoot-Signature: sha256=HMAC(secret, "<ts>.<body>")`.
- On failure for `message_created`/`message_updated`, the message is marked failed. Agent-bot webhooks retry on 429/500 (`RETRYABLE_AGENT_BOT_STATUSES`). Account webhooks are not retried beyond the Sidekiq default, and there is no delivery log table.

## 13. Background jobs and queues

`config/sidekiq.yml` defines strictly ordered queues: `critical`, `high`, `medium`, `default`, `monitors_live`, `mailers`, `action_mailbox_routing`, `low`, `scheduled_jobs`, `deferred`, `purgable`, `housekeeping`, … Lower queues run only when higher ones are empty. Examples:

| Job | Queue | Purpose |
|---|---|---|
| `ActionCableBroadcastJob` | `critical` | Push realtime events |
| `SendReplyJob` | `high` | Send outbound message to provider |
| `WebhookJob` | `medium` | Deliver outgoing webhook |
| `Webhooks::FacebookEventsJob`, `InstagramEventsJob` | `default` | Process inbound Meta events |
| `Webhooks::WhatsappEventsJob` | `low` | Process inbound WhatsApp events |
| `EventDispatcherJob` | — | Runs async listeners |

`MutexApplicationJob` gives jobs a Redis lock (`with_lock(key, ttl)`) and raises `LockAcquisitionError` for retry. `sidekiq-cron` (`config/schedule.yml`) runs periodic work: auto-resolution every 5 min, scheduled items, periodic assignment every 30 min, IMAP polling every minute, account deletion daily, orphan conversation cleanup every 12 h.

## 14. Real-time communication and the event bus

```text
Model callback → Rails.configuration.dispatcher.dispatch(event, ts, data)
                 ├─ SyncDispatcher  (in request)   → ActionCableListener, AgentBotListener
                 └─ AsyncDispatcher (EventDispatcherJob) → AutomationRuleListener, CampaignListener,
                    CsatSurveyListener, HookListener, InstallationWebhookListener, NotificationListener,
                    ParticipationListener, UnreadCounts::Listener, ReportingEventListener, WebhookListener
```

(`app/dispatchers/dispatcher.rb`, `sync_dispatcher.rb`, `async_dispatcher.rb`, `config/initializers/event_handlers.rb`)

- Event names are in `lib/events/types.rb` (`conversation.created`, `message.created`, `assignee.changed`, `conversation.bot_handoff`, `contact.merged`, …).
- `ActionCableListener` computes recipient tokens: the inbox members' `users.pubsub_token` plus the contact's `contact_inboxes.pubsub_token` (for non-private messages), then calls `ActionCableBroadcastJob`.
- `RoomChannel` (`app/channels/room_channel.rb`): a client subscribes with `pubsub_token` (plus `user_id` and `account_id` for agents). The server streams from the personal token stream and from `account_<id>` for agents. Presence lives in Redis (`OnlineStatusTracker`) and is broadcast as `presence.update`.
- **Durability gap:** events are dispatched from `after_commit` callbacks. If the process dies between commit and enqueue, async listeners (webhooks, automations, reporting) never run for that change. Converse uses a transactional outbox to close this gap.

## 15. File and attachment management

ActiveStorage blobs are attached to `attachments`. Inbound media is downloaded from the provider (`Down` gem / `SafeFetch`) and stored. Clients upload directly through `direct_uploads` endpoints for conversations and the widget. `Message#validate_attachments_limit` caps attachments per message. `SendReplyJob` waits 2 s when attachments exist, giving the upload time to finish analysis.

## 16. Automation engine

- `automation_rules`: `event_name` (`conversation_created`, `conversation_updated`, `conversation_opened`, `conversation_resolved`, `message_created`), `conditions` jsonb, `actions` jsonb, `active`.
- `AutomationRuleListener` (`app/listeners/automation_rule_listener.rb`) loads active rules for the event, evaluates them with `AutomationRules::ConditionsFilterService` and executes `AutomationRules::ActionService` (`app/services/automation_rules/action_service.rb`).
- Actions include `send_message`, `add_private_note`, `send_attachment`, `send_webhook_event`, assign agent/team, add/remove label, change status/priority, mute, snooze and send email.
- **Loop prevention:** events performed by an automation are ignored (`performed_by_automation?`), and `Current.executed_by = rule` tags the changes it makes.
- **Macros** (`macros` table, `MacrosExecutionJob`): action lists that agents run manually.

## 17. AI functionality (Enterprise "Captain")

> Read only to learn which capabilities exist. This code is under the Enterprise licence.

- Models: `Captain::Assistant`, `Captain::Document`, `Captain::AssistantResponse` (FAQ with `vector(1536)` embedding, ivfflat index), `Captain::Scenario`, `Captain::CustomTool`, `Captain::McpServer`, `CaptainInbox` (assistant ↔ inbox).
- Flow: an incoming message on a Captain-enabled inbox in `pending` status → `Captain::Conversation::ResponseBuilderJob` (`enterprise/app/jobs/captain/conversation/response_builder_job.rb`) → builds message history → `Captain::Assistant::AgentRunnerService` runs an agent with tools → reply or **handoff** (`handoff_completed?` → `bot_handoff!`).
- Safeguards: the job checks `conversation_pending?` before starting and **discards the reply if a newer customer message arrived** (`newer_customer_message_arrived?`).
- Knowledge: documents are crawled (`Captain::Documents::CrawlJob`, Firecrawl) → FAQ pairs are generated (`FaqGeneratorService`) → embeddings (`Captain::Llm::EmbeddingService`, `UpdateEmbeddingJob`) → retrieval with `SearchDocumentationService`.
- Copilot: an agent-assist feature with threads, runs, tool calls and approval cards (`copilot_*` tables).
- Model routing: `config/llm.yml` lists providers (OpenAI, Anthropic, Gemini) and models with credit multipliers.
- OSS agent bots: `agent_bots` + `AgentBotListener` send webhooks to an external bot. The bot replies through the API and can hand off.

## 18. API architecture

| Surface | Prefix | Auth | Consumer |
|---|---|---|---|
| Application API | `/api/v1/accounts/:account_id/...` | devise_token_auth headers or `api_access_token` | Dashboard, integrations |
| Profile/auth | `/auth/*`, `/api/v1/profile` | devise_token_auth | Dashboard |
| Widget API | `/api/v1/widget/...?website_token=` | `X-Auth-Token` JWT | Website widget |
| Public client API | `/public/api/v1/inboxes/:identifier/contacts/...` | Inbox identifier + contact source id | Custom chat UIs (API channel) |
| Platform API | `/platform/api/v1/...` | Platform app token | Hosting/reseller automation |
| Provider webhooks | `/webhooks/*`, `/bot` | Provider signature | Meta, Twilio, Telegram, … |
| v2 reports | `/api/v2/accounts/:id/reports/...` | Agent auth | Dashboard reports |

Responses are rendered with Jbuilder views (`app/views/api/v1/...`). The API is documented with Swagger (`swagger/`). Pagination uses Kaminari.

## 19. Deployment and infrastructure

- Docker image `chatwoot/chatwoot`. Compose services: `rails` (Puma :3000), `sidekiq`, `postgres` (`pgvector/pgvector:pg16`), `redis` (`docker-compose.production.yaml`).
- `Procfile`/`Procfile.dev` (overmind/foreman) for development. `deployment/` holds Linux install scripts. `clevercloud/` and `app.json` (Heroku) provide PaaS configs.
- Health: `HealthController`, `sidekiq_alive`.
- Config: `.env` (`.env.example`) plus `installation_configs` table (`GlobalConfigService.load('FB_APP_SECRET')`), editable from Super Admin.

---

## Module-by-module guide: what, why, how, and the Python equivalent

| Module | What it does / why it exists | How Chatwoot does it (tables, services, APIs) | Python equivalent |
|---|---|---|---|
| Accounts & tenancy | Isolates each customer business | `accounts`, `account_users`; `EnsureCurrentAccountHelper`; `/api/v1/accounts/:id` | `organizations` → `workspaces`; `TenantContext` dependency; PostgreSQL RLS (`09_MULTI_TENANCY.md`) |
| Users & auth | Agents log in from many devices | `users.tokens` (devise_token_auth), `access_tokens`, MFA | JWT access token (15 min) + rotating refresh tokens in `user_sessions`; hashed API tokens; TOTP (`10_AUTHENTICATION_AND_RBAC.md`) |
| RBAC | Limit what agents can see and do | Pundit policies, `role` enum, Enterprise `custom_roles.permissions` | `permissions` catalog, `roles` + `role_permissions`, `require_permission()` dependency, visibility query filters |
| Inboxes & channels | One inbox per connected account/number/site | `inboxes` + polymorphic `channel_*` | `inboxes` + `channel_connections` (encrypted credentials) + provider detail tables + global `channel_routes` |
| Contacts | Customer identity across channels | `contacts`, `contact_inboxes`, `ContactInboxWithContactBuilder` | `contacts`, `contact_identities`; `ContactResolver` with `INSERT … ON CONFLICT` |
| Conversations | Ticket-like thread with status and assignee | `conversations`, model callbacks, `display_id` trigger | `ConversationService` with explicit state transitions; `next_workspace_counter()` |
| Messages | All traffic, notes and activities | `messages`, `Message` callbacks, `SendReplyJob` | `MessageService` persists + writes outbox; `deliver_outbound_message` task calls the channel adapter |
| Channel integrations | Talk to Meta/widget | Per-provider controllers/jobs/services; `Base::SendOnChannelService` | `BaseChannelAdapter` contract, one package per provider, a single shared inbound pipeline (`integrations/CHANNEL_ADAPTER_ARCHITECTURE.md`) |
| Webhook ingress | Accept provider events safely | `MetaTokenVerifyConcern`, Redis mutex, `MessageDedupLock` | Signature check on raw body → `inbound_webhook_events` (unique dedup key) → Celery, with per-conversation ordering keys |
| Outgoing webhooks | Notify customer systems | `webhooks`, `WebhookJob`, `Webhooks::Trigger` | `webhook_subscriptions` + `webhook_deliveries` log with retries, backoff, auto-disable, HMAC signature |
| Event bus | Decouple side effects | Wisper sync/async dispatchers, listeners | Transactional outbox + relay → Celery handlers; realtime published after commit |
| Realtime | Live inbox updates | ActionCable `RoomChannel`, `pubsub_token`, `OnlineStatusTracker` | FastAPI WebSocket gateway + Redis pub/sub; per-user/per-contact tokens; presence in Redis |
| Background jobs | Async + scheduled work | Sidekiq priority queues, sidekiq-cron, `MutexApplicationJob` | Celery with priority queues, Celery beat, Redis locks |
| Attachments | Media in/out | ActiveStorage, direct uploads | `stored_files` + `StorageBackend` (local/S3), presigned uploads, MIME sniffing, AV scan hook |
| Automation | No-code rules | `automation_rules`, listener + condition/action services | Same rule model with typed Pydantic conditions/actions, `automation_rule_executions` log, loop guard |
| Canned responses, labels, attributes, notes, macros | Agent productivity | Dedicated tables | Same tables, workspace-scoped |
| AI agent | Auto-reply + handoff | Captain (Enterprise) | LangGraph graph with per-tenant config, RAG over pgvector, explicit handoff node (`ai/`) |
| Knowledge base | Ground AI answers | `captain_documents`, `captain_assistant_responses` embeddings | `knowledge_bases`, `kb_documents`, `kb_chunks` (HNSW, hybrid search) |
| Reporting | Metrics | `reporting_events`, `ReportingEventListener`, v2 summary reports | `reporting_events` written by outbox handler; SQL aggregates; later rollups |
| Billing | Plans and limits | Stripe in Enterprise, `accounts.limits` | `subscription_plans`, `subscriptions`, `billing_records`, `usage_records`; Stripe adapter |
| Audit | Who changed what | `audited` gem → `audits` | `audit_logs` (append-only for the app role) |
| Super admin | Installation management | `app/controllers/super_admin`, Administrate | `platform_admins` + `/api/v1/platform/*` endpoints |

## Comparison table

| Chatwoot Module | Existing Implementation | Python Equivalent | Priority |
|---|---|---|---|
| Accounts / tenancy | `accounts`, `account_users`, `EnsureCurrentAccountHelper` | `organizations`, `workspaces`, `organization_memberships`, RLS | P0 (MVP) |
| Authentication | Devise + devise_token_auth + devise-two-factor | FastAPI + JWT (PyJWT) + argon2 + pyotp | P0 |
| Authorization | Pundit, `role` enum, Enterprise `CustomRole` | Permission catalog + roles + FastAPI dependencies | P0 |
| Inboxes | `inboxes` + polymorphic `channel_*` | `inboxes` + `channel_connections` + detail tables | P0 |
| Contacts | `contacts`, `contact_inboxes` | `contacts`, `contact_identities` | P0 |
| Conversations | `Conversation` model + callbacks | `ConversationService` (explicit) | P0 |
| Messages | `Message` + `SendReplyJob` | `MessageService` + `deliver_outbound_message` | P0 |
| Realtime | ActionCable `RoomChannel` | FastAPI WebSockets + Redis pub/sub | P0 |
| Website widget | `Channel::WebWidget`, `/api/v1/widget`, SDK | `web_widget` adapter, `/api/v1/widget`, JS SDK | P0 |
| Facebook Messenger | `facebook-messenger` gem, `FacebookEventsJob` | `channels/meta/messenger` adapter | P0 |
| Instagram | `Channel::Instagram`, `InstagramEventsJob` | `channels/meta/instagram` adapter | P1 |
| WhatsApp Cloud | `Channel::Whatsapp`, `WhatsappEventsJob`, providers | `channels/meta/whatsapp` adapter | P0 |
| Attachments | ActiveStorage | `stored_files` + StorageBackend | P0 |
| Assignment | `AutoAssignment::*` round robin (Redis list) | `AssignmentService` (round robin + capacity) | P1 |
| Teams | `teams`, `team_members` | Same | P1 |
| Labels | acts-as-taggable-on | `labels`, `conversation_labels`, `contact_labels` | P1 |
| Custom attributes | `custom_attribute_definitions` + jsonb | Same + Pydantic validation | P1 |
| Canned responses | `canned_responses` | Same | P1 |
| Private notes | `messages.private` | `messages.is_private` | P0 |
| Contact notes | `notes` | `notes` | P2 |
| Automation | `automation_rules` + listener | `automation` module | P1 |
| Macros | `macros` | `macros` | P2 |
| Outgoing webhooks | `webhooks` + `WebhookJob` | `webhook_subscriptions` + `webhook_deliveries` | P1 |
| Agent bots (external) | `agent_bots`, `AgentBotListener` | Webhook subscriptions + API tokens (bot = token owner) | P2 |
| AI agent | Captain (Enterprise) | LangGraph agent | P1 |
| Knowledge base / RAG | Captain documents + embeddings | `knowledge_bases`, `kb_chunks` + pgvector | P1 |
| Copilot (agent assist) | Copilot (Enterprise) | Reply suggestions via the same graph in `assist_only` mode | P2 |
| Reports | `reporting_events`, v2 reports | `reporting_events` + analytics endpoints | P1 |
| Billing | Stripe (Enterprise) | `billing` module + Stripe adapter | P2 |
| Audit logs | `audited` | `audit_logs` | P1 |
| Notifications | `notifications`, push, email | `notifications` + WebSocket (email/push later) | P1 |
| Email channel | IMAP/SMTP, ActionMailbox | Future module | P3 |
| Help center portal | `portals`, `articles` | Future (KB documents cover AI needs) | P3 |
| Campaigns, CSAT, SLA | Dedicated tables | Future | P3 |
| Other channels (LINE, Telegram, SMS, TikTok, X) | Per-channel modules | Future adapters on the same contract | P3 |

Priority: **P0** = MVP must-have, **P1** = first release after MVP, **P2** = later, **P3** = future/not planned.

---

## Analysis checklist

| Area | Status | Files inspected |
|---|---|---|
| Repository layout, Gemfile, package.json | ✅ Inspected | `Gemfile`, `package.json`, top-level tree |
| Database schema (core tables, triggers, FKs, extensions) | ✅ Inspected | `db/schema.rb` (all 111 table names; full definitions of accounts, account_users, users, inboxes, contacts, contact_inboxes, conversations, messages, attachments, channel_whatsapp, channel_facebook_pages, channel_instagram, channel_web_widgets, webhooks, automation_rules, teams, labels, captain_assistant_responses) |
| Routes | ✅ Inspected | `config/routes.rb` (accounts API, widget API, webhooks, `/bot`) |
| Auth & account scoping | ✅ Inspected | `access_token_auth_helper.rb`, `ensure_current_account_helper.rb`, `api/v1/accounts/base_controller.rb`, `widget/token_service.rb`, `website_token_helper.rb` |
| Authorization | ✅ Inspected | `app/policies/` listing, `conversation_policy.rb`, `account_user.rb`, `enterprise/app/models/custom_role.rb` |
| Conversation & message models | ✅ Inspected | `app/models/conversation.rb`, `app/models/message.rb` (callbacks, enums, validations) |
| Event bus & realtime | ✅ Inspected | `app/dispatchers/*`, `config/initializers/event_handlers.rb`, `action_cable_listener.rb`, `room_channel.rb`, `action_cable_broadcast_job.rb`, `lib/events/types.rb`, `dashboard/helper/actionCable.js` |
| WhatsApp ingress & processing | ✅ Inspected | `webhooks/whatsapp_controller.rb`, `whatsapp_events_job.rb`, `incoming_message_base_service.rb`, `message_dedup_lock.rb`, `meta_token_verify_concern.rb`; service directory listing |
| Facebook Messenger | ✅ Inspected | `config/initializers/facebook_messenger.rb`, `facebook_events_job.rb`, `lib/integrations/facebook/message_creator.rb` |
| Instagram | ✅ Inspected | `webhooks/instagram_controller.rb`, `instagram_events_job.rb`; service directory listing |
| Outbound sending | ✅ Inspected | `send_reply_job.rb`, `base/send_on_channel_service.rb` |
| Contacts | ✅ Inspected | `contact_inbox_with_contact_builder.rb` |
| Widget API | ✅ Inspected | `api/v1/widget/base_controller.rb`, widget routes, `app/javascript/sdk/` listing |
| Automation | ✅ Inspected | `automation_rule_listener.rb`, `automation_rules/action_service.rb` |
| Outgoing webhooks | ✅ Inspected | `webhook_job.rb`, `lib/webhooks/trigger.rb` |
| Jobs & schedules | ✅ Inspected | `config/sidekiq.yml`, `config/schedule.yml`, job directory listings |
| Auto-assignment | ⚠️ Partially | `inbox_round_robin_service.rb` read. `agent_assignment_service.rb`, `rate_limiter.rb` listed only |
| Rate limiting | ✅ Inspected | `config/initializers/rack_attack.rb` (throttle definitions) |
| Encryption of credentials | ✅ Inspected | `grep encrypts app/models` |
| Captain AI (Enterprise) | ⚠️ Partially | `response_builder_job.rb`, model/service/job listings, `config/llm.yml`. Runner, tool and prompt internals were deliberately **not** studied (licence) |
| Billing (Enterprise) | ⚠️ Listing only | `enterprise/app/services/enterprise/billing/` file names |
| Deployment | ✅ Inspected | `docker-compose.production.yaml`, top-level `Procfile*`, `deployment/` listing |
| Frontend dashboard | ⚠️ Structure only | Directory layout, Vuex module list, `actionCable.js` event map. Components not reviewed |
| Widget frontend (`app/javascript/widget`) | ❌ Not inspected | — |
| Email channel (IMAP, ActionMailbox, mailers) | ❌ Not inspected | Out of Converse MVP scope |
| Twitter, LINE, Telegram, TikTok, SMS/Twilio, voice calls | ❌ Not inspected beyond routes | Out of scope |
| Help center (portals/articles/categories) | ❌ Not inspected | Out of scope |
| Campaigns, CSAT, SLA, conversation monitors | ❌ Not inspected | Out of scope |
| Integrations (Slack, Shopify, Linear, Notion, Dyte, dashboard apps) | ❌ Not inspected | Out of scope |
| Super admin / Platform API | ⚠️ Routes only | — |
| Reporting builders (`app/builders/v2`, `services/reports`) | ⚠️ Listing only | — |

**The entire codebase has not been analyzed.** The modules that matter for the requested scope (tenancy, auth, inbox, contacts, conversations, messages, Meta channels, widget, realtime, jobs, webhooks, automation, AI surface) were read in the files listed above. Out-of-scope channels and features were not.
