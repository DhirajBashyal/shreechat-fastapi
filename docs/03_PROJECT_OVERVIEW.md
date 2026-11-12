# 03 — Project Overview

## 1. What we are building

Converse is a SaaS shared inbox. A business connects its website, Facebook Page, Instagram account and WhatsApp number. Customer messages from all of them land in one inbox, where human agents and an AI agent answer them in real time.

## 2. Users

| User | Needs |
|---|---|
| **Customer** (end user of a business) | Writes from WhatsApp/Messenger/Instagram/website and gets fast, correct answers |
| **Agent** | Sees assigned and unassigned conversations, replies, adds notes, labels, hands over |
| **Administrator** | Connects channels, manages members, teams, automation, AI settings |
| **Owner** | Everything an administrator can do, plus billing and deleting the organization |
| **Platform admin** (us) | Manages tenants, plans, suspensions, global configuration |
| **Developer** (customer's engineer) | Uses API tokens and webhooks to integrate their systems |

## 3. Goals

1. **Correct multi-tenancy.** No tenant can see another tenant's data, even through a coding mistake. Defence in depth: request scoping + composite foreign keys + PostgreSQL RLS.
2. **No lost or duplicated customer messages.** Every provider webhook is verified, recorded, deduplicated and processed at least once, with idempotent writes.
3. **Real-time.** An incoming message appears in the agent's browser within about 1 second (p95) of the webhook being received.
4. **Channel-independent core.** Adding a channel means adding an adapter package. The conversation/message core never imports provider code.
5. **Tenant-configurable AI.** Each workspace picks its provider, model, prompt, tools and knowledge bases. AI never runs while a human owns the conversation.
6. **Operable by a small team.** One modular monolith, two process types (API, worker) plus a scheduler. Standard Postgres and Redis.

## 4. Non-goals (for now)

- Microservices. Module boundaries are enforced in code (import rules), not by the network.
- Voice calls, video calls, campaigns, CSAT surveys, SLA policies, help-center portal.
- Email channel (designed for, scheduled as Future).
- Mobile apps (the API supports them; building them is out of scope).
- Building a frontend in these docs. The dashboard API and realtime protocol are specified. The SPA itself is a separate project (Vue 3 or React, team's choice).

## 5. Scope by release

| Release | Contents | Done when |
|---|---|---|
| **MVP** | Auth, organizations/workspaces, fixed roles, inboxes, website widget, Messenger, WhatsApp Cloud (manual setup), contacts, conversations, messages, notes, attachments, realtime, manual assignment | One tenant handles real traffic from 3 channels with no lost messages for 2 weeks in staging |
| **R1** | Instagram, Embedded Signup, teams, labels, custom attributes, canned responses, auto-assignment, automation, outgoing webhooks, API tokens, AI agent + RAG + handoff, reports, audit writes, notifications, MFA, platform admin API | First paying pilot customers |
| **R2** | Billing (Stripe), custom roles, macros, contact notes, agent-assist suggestions, audit UI | Self-serve signup and payment |
| **Future** | Email, more channels, campaigns, CSAT, SLA, help center | — |

## 6. Design principles

1. **Thin routes, explicit services.** Route handlers validate input, call one service method and serialize the result. Business rules live in services.
2. **Side effects through events.** A service changes state and records a domain event in the same transaction. Handlers (realtime, webhooks, automation, AI, reporting) react to events.
3. **Providers behind adapters.** Meta, the widget and later email implement `BaseChannelAdapter`. The core speaks only in normalised types (`InboundMessage`, `OutboundMessage`, `DeliveryUpdate`).
4. **AI is a consumer, not a core dependency.** The AI module reads conversations and posts replies through the same `MessageService` an agent uses. Removing AI breaks nothing else.
5. **Idempotency by default.** Every write that can be retried has a natural key (provider message id, client message id, outbox event id, idempotency key).
6. **Fail visibly.** Missing configuration or impossible states raise. Errors on channel connections are stored and shown to admins (`channel_connections.last_error`).
7. **Bound every workload.** Paginated lists, batched jobs, capped retries, timeouts on every outbound HTTP call.

## 7. Key risks identified up front

| Risk | Where | Mitigation (document) |
|---|---|---|
| Cross-tenant leak | Any query | RLS + composite FKs + tenant-scoped repositories ([09](09_MULTI_TENANCY.md)) |
| Forged provider webhooks | `/webhooks/meta/*` | HMAC on raw body, constant-time compare ([integrations/CHANNEL_ADAPTER_ARCHITECTURE.md](integrations/CHANNEL_ADAPTER_ARCHITECTURE.md)) |
| Duplicate messages from webhook retries | Inbound pipeline | `inbound_webhook_events` unique dedup key + `messages (inbox_id, external_id)` unique index ([backend/MESSAGE_PROCESSING.md](backend/MESSAGE_PROCESSING.md)) |
| Out-of-order messages | Concurrent workers | Per-conversation ordering key; order by provider timestamp, then `sequence_number` |
| Hot tenant starves others | Workers | Per-tenant rate limits and queue fairness ([backend/BACKGROUND_JOBS.md](backend/BACKGROUND_JOBS.md)) |
| Meta token expiry | Instagram (60-day tokens), Messenger page tokens revoked on password change | Refresh job + `reauthorization_required` status + admin notification |
| AI hallucination / prompt injection | AI agent | Grounding, guardrail node, tool allow-lists, tenant-scoped retrieval ([ai/AI_ARCHITECTURE.md](ai/AI_ARCHITECTURE.md)) |
| AI cost runaway | AI agent | Per-plan quotas in `usage_records`, max turns before handoff |
| Large `messages` table | Postgres | Indexes designed for `(conversation_id, sequence_number)`; partitioning plan in [06](06_DATABASE_ARCHITECTURE.md) |
| WebSocket fan-out at scale | Realtime | Redis pub/sub per tenant channel; horizontal gateway replicas ([backend/REALTIME_COMMUNICATION.md](backend/REALTIME_COMMUNICATION.md)) |
