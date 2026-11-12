# 11 — Security and Production Readiness

One place for every security and reliability control, with links to the detailed design. Use it as a pre-launch checklist.

## 1. Tenant isolation

| Control | Detail |
|---|---|
| Request scoping | `TenantContext` resolved from the path's workspace/organization + membership; 404 for anything not visible ([09](09_MULTI_TENANCY.md)) |
| Repository filters | Every query filters by `workspace_id`; `session.get()` banned in app code |
| Composite FKs | Cross-workspace references can't be stored ([06](06_DATABASE_ARCHITECTURE.md#3-tenant-columns)) |
| RLS | Forced on all tenant tables for `converse_app`; tested ([database/TENANT_ISOLATION.md](database/TENANT_ISOLATION.md)) |
| Caches, storage keys, Redis channels | Always tenant-prefixed through helpers |
| AI | Retrieval, tools and memory built from the run's tenant context ([ai/RAG_ARCHITECTURE.md](ai/RAG_ARCHITECTURE.md#5-tenant-isolation-of-knowledge)) |
| Tests | Cross-tenant suite runs every endpoint with foreign ids |

## 2. Authentication

Short-lived JWT access tokens (15 min), rotating refresh tokens with reuse detection, argon2id passwords, TOTP MFA, hashed API tokens with scopes, visitor tokens scoped to one identity, MFA-only platform admin ([10](10_AUTHENTICATION_AND_RBAC.md)).

## 3. Authorization

Permission catalog + base/custom roles, `require_permission()` on every mutating route, conversation visibility as a query filter, scope intersection for API tokens. CI check: every route under `/api/v1/workspaces` and `/api/v1/organizations` declares a permission dependency (a test walks `app.routes`).

## 4. OAuth security (Meta, Instagram, Stripe Connect later)

- `state` = 32 random bytes, stored server-side in Redis for 10 minutes with `{organization_id, workspace_id, user_id, provider, inbox_id?}`, **single use** (deleted on callback). Callback rejects missing/unknown/expired state.
- After the callback, re-check that the user still has `inbox.manage` in that workspace.
- Exact redirect URIs registered with Meta; no open redirects (the post-OAuth redirect target is a fixed dashboard path, not a parameter).
- Exchange codes server-side only; never expose app secrets to the browser.
- Verify granted scopes (`/debug_token`) and fail clearly if a required permission was unchecked.
- Store long-lived tokens encrypted; never log them; redact `access_token` query params in httpx logging hooks.
- Handle Meta deauthorization and data-deletion callbacks ([integrations/FACEBOOK_MESSENGER.md](integrations/FACEBOOK_MESSENGER.md#6-disconnect--reconnect)).

## 5. Webhook signature validation

**Inbound (Meta):**
- HMAC-SHA256 over the **raw** request body with the app secret, compared to `X-Hub-Signature-256` using `hmac.compare_digest`. Same mechanism Chatwoot implements in `MetaTokenVerifyConcern#valid_meta_signature?`.
- Body read with a 1 MB limit before parsing; JSON parsing happens before verification only to extract routing ids for candidate secrets. **Nothing is stored or enqueued until verification succeeds.**
- Verification handshake compares `hub.verify_token` in constant time.
- No "accept unsigned while pending" mode. Chatwoot has a temporary `app_secret_pending?` path for WhatsApp channels that logs and accepts unverified webhooks; Converse requires a secret before a connection becomes `active`.
- Stripe: official SDK `Webhook.construct_event` with tolerance 300 s.

**Outbound (ours):** `X-Converse-Signature: sha256=HMAC(secret, "<timestamp>.<body>")` + `X-Converse-Timestamp` + stable `X-Converse-Delivery` id. Secrets are per subscription, rotatable with a 24 h overlap.

## 6. Duplicate webhook prevention

Meta can deliver the same event several times (retries after timeouts, Business Manager misconfigurations; Chatwoot's code comments mention both). Defences in order:

1. `inbound_webhook_events (provider, dedup_key)` unique: duplicates are dropped at ingress before enqueueing.
2. `messages (inbox_id, external_id)` unique: catches the same message arriving in a differently shaped payload.
3. `contact_identities (inbox_id, source_id)` unique + `ON CONFLICT`: identity races.
4. Ordering lock per `(inbox, sender)`: prevents two conversations being created by concurrent first messages.
5. Status updates are monotonic, so a replayed `delivered` after `read` changes nothing.

## 7. Message ordering

Per-sender ordering lock for inbound; provider timestamps preserved; `sequence_number` tie-break; outbound per-conversation lock sends older pending messages first ([backend/MESSAGE_PROCESSING.md](backend/MESSAGE_PROCESSING.md#13-ordering-guarantees)). Lock contention re-queues instead of blocking, and never drops events.

## 8. Rate limiting and abuse prevention

| Surface | Limit (defaults, configurable) | Key |
|---|---|---|
| Global per IP | 3,000 req/min (same as Chatwoot's default `RACK_ATTACK_LIMIT`) | IP |
| Login | 5 / 5 min per IP; 10 / 15 min per email | IP, email |
| Password reset, email verification, signup | 5 / 30 min per IP; 5 / h per email | IP, email |
| MFA verify | 5 / min per IP and per `mfa_token` | IP, token |
| Authenticated API | Plan based (e.g. 600 / min per org; 120 / min per API token) | org, token |
| Widget sessions | 5 / min per IP; captcha above 20 / h | IP |
| Widget messages | 20 / min per visitor; 100 / min per IP | visitor, IP |
| Message creation (agents) | 60 / min per conversation (flood guard, like Chatwoot's `prevent_message_flooding`) | conversation |
| Outbound to provider | Per-inbox token bucket (WhatsApp 20/s default) | inbox |
| Webhook ingress | No auth-based limit (Meta must get through); body size cap; signature required | — |

Implementation: Redis token buckets (`core/ratelimit.py`) as FastAPI dependencies; Nginx `limit_req` as an outer coarse layer. Responses include `RateLimit-*` headers and `Retry-After`.

Other abuse controls: signup email verification before connecting channels; disposable-email blocklist; captcha (Turnstile/hCaptcha) on signup and widget session spikes; platform admin can suspend organizations.

## 9. Secret encryption and token storage

| Secret | Storage |
|---|---|
| Passwords | argon2id hash |
| Refresh tokens, API tokens, invitation/reset/verification tokens | SHA-256 hash of 256-bit random value |
| Meta tokens, WhatsApp tokens, AI provider keys, webhook secrets, widget HMAC secrets, MFA secrets | AES-256-GCM envelope encryption with versioned keys and row-bound AAD ([06](06_DATABASE_ARCHITECTURE.md#5-secrets-and-credentials)) |
| Platform secrets (JWT keys, Meta app secret, Stripe keys, encryption master keys) | Secret manager (AWS Secrets Manager / GCP Secret Manager / Vault) → env at startup; never in images or the repo |

Rotation: JWT signing keys (two active `kid`s), encryption keys (`reencrypt_secrets` task), webhook secrets (overlap window), Meta app secret (re-subscribe), database passwords (dual users).

## 10. PII protection

- Data minimisation in logs: message content, emails, phones and tokens are never logged; structlog processors redact keys matching `token|secret|password|authorization|content|email|phone`.
- Sentry `before_send` scrubs request bodies and the same keys.
- AI: optional PII redaction before LLM calls; private notes never sent to LLMs; tenants informed of provider data handling; per-tenant BYO keys.
- Encryption at rest (managed Postgres, S3 SSE) and in transit (TLS 1.2+ everywhere, including Postgres and Redis in production).
- GDPR: contact erasure (hard delete + message content scrub), organization export, data retention setting, data processing records in `audit_logs`.
- Access to production data by staff only through audited platform admin endpoints or break-glass DB access with approval.

## 11. File upload validation

Presigned uploads with `content-length-range`; declared type allow-list per purpose; server-side MIME sniffing (libmagic) and mismatch rejection; image re-encoding and EXIF stripping; SVG/HTML rejected; downloads via short-lived signed URLs with `Content-Disposition: attachment` and `nosniff`; optional ClamAV scan hook (`scan_status`) ([backend/STORAGE_ARCHITECTURE.md](backend/STORAGE_ARCHITECTURE.md)).

## 12. SQL injection prevention

- All queries through SQLAlchemy expressions with bound parameters.
- The only SQL text is the reviewed baseline migration and named functions (`next_workspace_counter`, `set_config`), called through `func.*`.
- Dynamic sorting/filtering uses allow-listed column maps (`SORT_COLUMNS = {"last_activity_at": Conversation.last_activity_at, ...}`), never user strings.
- Automation conditions and contact/conversation filters compile from typed Pydantic models to SQLAlchemy expressions; JSONB paths come from validated custom attribute keys (`^[a-z][a-z0-9_]*$`).
- CI lint: forbid `text(` and f-strings inside `execute(` outside `converse/db/migrations`.

## 13. Other web security

- SSRF: every user-supplied URL (webhook targets, KB URLs, avatar URLs, custom tools) goes through `SafeHttpClient`: resolve DNS, block private/loopback/link-local/metadata ranges (incl. IPv6 and `169.254.169.254`), re-check on each redirect, cap response size, timeouts. Chatwoot uses the `ssrf_filter` gem / `SafeFetch` for the same purpose.
- XSS: API returns JSON only; clients render Markdown through a sanitiser; CSP on dashboard and widget (`default-src 'self'`, no inline scripts).
- CORS: explicit allow-lists (dashboard origin; per-widget origins).
- Security headers at Nginx: HSTS, `X-Content-Type-Options`, `Referrer-Policy: strict-origin-when-cross-origin`, `frame-ancestors` (dashboard: `'none'`; widget iframe: `*` because it is embedded).
- Dependency scanning (`pip-audit`, Dependabot), container scanning (Trivy), secret scanning in CI.

## 14. Audit logging

Append-only `audit_logs` (the app role can't update/delete). Logged actions: auth security events, member/role changes, API token create/revoke, channel connect/disconnect/reauthorize, AI credential and agent changes, webhook changes, automation changes, data export/erasure, organization settings/deletion, all platform admin actions, impersonation. Each entry: actor, action, resource, `changes` (secrets redacted), IP, user agent, request id. Retention ≥ 1 year (configurable per plan).

## 15. Retry mechanisms and idempotency

| Layer | Mechanism |
|---|---|
| Client → API | `Idempotency-Key` header for creates (`idempotency_keys`, 24 h); `client_message_id` for messages |
| Provider → us | Dedup tables + unique indexes (§6) |
| Us → provider | Classified errors (retryable vs permanent), exponential backoff with jitter, `Retry-After`, per-inbox rate limiting |
| Us → customer webhooks | 8 attempts over ~2 days, stable delivery id, auto-disable after sustained failure |
| Internal events | Transactional outbox, at-least-once handlers with per-handler dedup keys |
| AI | Node-level retry policy; whole runs never retried (no double replies) |

## 16. Dead-letter strategy

Dead items live in their owning tables (`inbound_webhook_events.state = dead`, `messages.delivery_status = failed`, `webhook_deliveries.state = dead`, `kb_documents.status = failed`, `ai_runs.status = failed`), each with the last error, visible in the relevant UI and replayable through the same code path ([backend/BACKGROUND_JOBS.md](backend/BACKGROUND_JOBS.md#5-retries-and-dead-letters)). Alerts fire on any new dead inbound event.

## 17. Background task failure recovery

`acks_late` + `reject_on_worker_lost` (redelivery on crash); visibility timeout above the longest task; sweeper tasks for stuck `received`/`processing` states and pending outbox rows; beat as a single replica with idempotent schedules; graceful shutdown (`SIGTERM` → finish current task within the time limit).

## 18. Backup and recovery

| Item | Policy |
|---|---|
| PostgreSQL | Managed service with PITR (WAL archiving), 14-day retention minimum; daily snapshot copied to another region; monthly restore test into staging (documented runbook, measured RTO) |
| Targets | RPO ≤ 5 minutes, RTO ≤ 1 hour for a full region failure of the database |
| Object storage | Versioning + cross-region replication for the attachments bucket |
| Redis | Not backed up for correctness (no durable state). AOF on for the broker to reduce redelivery storms after a restart |
| Secrets | Secret manager with versioning; encryption master keys in KMS with deletion protection |
| Configuration | Infrastructure as code (Terraform), images tagged by git SHA |
| Tenant-level restore | Not supported from backups in MVP; soft delete (30 days) covers accidental deletes |

## 19. Scalability and security risks found before implementation

| # | Risk | Mitigation in design |
|---|---|---|
| 1 | Shared HNSW index + RLS filtering may return too few results for small tenants in a huge index | pgvector iterative scans; monitor recall; partition `kb_chunks` by org hash if needed |
| 2 | `messages` table growth | Index design for keyset access; partition plan at ~200M rows |
| 3 | WhatsApp duplicate send on crash between API success and `external_id` save | Narrow window; documented accepted risk; monitor duplicates via status webhooks referencing unknown ids |
| 4 | Meta app review delays (permissions like `pages_messaging`, Human Agent tag) | Start review in Phase 4; test users/dev mode until approved |
| 5 | Prompt injection via knowledge documents or customer messages | Data/instruction separation, tool allow-lists, no side-effect tools, guardrails |
| 6 | One tenant's webhook endpoint slow/down consuming worker capacity | Per-tenant concurrency caps on `webhooks` queue; 10 s timeout; auto-disable |
| 7 | `converse_system` misuse | Import-linter contract; code review rule; audit of system-session call sites |
| 8 | Realtime visibility logic diverging from REST visibility | Both use the same `visible()` predicate module; property-based test compares them |
| 9 | Long-running tenant purge locking tables | Batched deletes by id range with `lock_timeout` |
| 10 | Clock skew affecting 24 h window checks | Use provider timestamps for `last_incoming_at`; NTP on all hosts |
