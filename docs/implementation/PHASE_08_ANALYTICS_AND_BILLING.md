# Phase 8 — Analytics and Billing (Steps 19–20)

## 1. Objective
Managers see how their support operation performs; the platform charges for plans and usage and enforces limits.

## 2. Features
**Analytics (R1)**
- Reporting events written by an outbox handler: `first_response`, `reply_time`, `conversation_opened`, `conversation_resolved`, `conversation_bot_handoff`, `conversation_bot_resolved` (same event names as Chatwoot's `reporting_events`).
- Business-hours-aware durations (`value_in_business_hours_seconds`) using inbox working hours.
- Report endpoints: overview, time series, per agent/inbox/team/label, AI report, live counts, CSV export.
- Read-replica routing for report queries.

**Billing (R2)**
- Plans, Stripe Checkout and customer portal, subscription sync via Stripe webhooks, invoices mirror.
- Seat counting (agent memberships), plan limits (`LimitsService`), feature flags from plans.
- Usage metering (AI responses/tokens), monthly quota enforcement, overage policy (block or bill).
- Trial expiry and dunning (`past_due` → grace period → `suspended`).

## 3. Files to create
```text
src/converse/modules/analytics/{models.py, handlers.py, business_hours.py, queries.py, service.py, schemas.py, export.py}
src/converse/modules/billing/{models.py, repository.py, service.py, limits.py, usage.py, stripe_adapter.py, provider.py, schemas.py, handlers.py}
src/converse/api/routes/{analytics.py, billing.py}
src/converse/api/webhooks/stripe.py
src/converse/workers/tasks/billing.py
tests/integration/analytics/*, tests/integration/billing/*
```

## 4. Database tables
`reporting_events`; `subscription_plans`, `subscriptions`, `billing_records`, `usage_records`, `billing_webhook_events`; reads `conversations`, `messages`, `ai_runs`, `organization_memberships`.

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §25 (reports), §26 (billing), §28 (Stripe webhook).

## 6. Dependencies
Phases 3, 6, 7 (conversation, AI and assignment events). `stripe` SDK (R2).

## 7. Implementation steps
**Analytics**
1. Handler on `message.created` (outgoing, non-private, human or AI): if it's the first reply → `first_response` (`value = reply.created_at - conversation.created_at` or since reopen); otherwise `reply_time` (since the oldest unanswered incoming message).
2. Handler on status changes: `conversation_resolved` (duration since open/reopen), `conversation_opened`; on handoff: `conversation_bot_handoff`; resolved while `ai_active` and no human message: `conversation_bot_resolved`.
3. Business hours calculator (per inbox timezone and `working_hours`), unit-tested across DST changes.
4. Report queries in SQLAlchemy (aggregates over `reporting_events` and conversation/message counts) with indexes from the schema; workspace timezone bucketing (`date_trunc` on `AT TIME ZONE`).
5. Route report queries to the read replica; cap ranges at 400 days; cache results 60 s.
6. When `reporting_events` exceeds ~50M rows, add daily rollup tables (future migration).

**Billing**
7. `BillingProvider` interface; `StripeBillingProvider` (customer create on org creation, Checkout session, portal session).
8. Stripe webhook endpoint: verify signature, dedup by event id in `billing_webhook_events`, handle `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid|payment_failed|finalized` → upsert `subscriptions` / `billing_records`; emit `account.billing_updated` realtime event.
9. `LimitsService.check(ctx, key)` used by inbox/member/workspace/KB creation; `require_feature` reads plan features ∪ org flags.
10. Usage: `usage_records` written by AI runs (`ai_responses`, `ai_tokens`); quota check in `tenant_context` node; monthly report; overage handling per plan.
11. Seats: count `organization_memberships` with role ≠ none; sync quantity to Stripe on member add/remove (debounced).
12. `reconcile_billing` daily task.
13. Trial/dunning state machine and suspension via `organizations.status`.

## 8. Testing requirements
- Reporting: first response vs reply time across reopen cycles; private notes and activities excluded; AI replies counted separately where reports split human/AI.
- Business hours across weekends, DST transitions and overnight shifts.
- Report numbers match a brute-force SQL computation on a seeded dataset.
- Stripe: signature verification, duplicate events ignored, out-of-order events (`updated` before `created`) handled by comparing `created` timestamps, plan change updates limits immediately.
- Limits: creating the N+1th inbox/agent returns `402 plan_limit_reached`; downgrades don't delete data but block new creations.
- Usage: one AI run → one usage record even if handled twice.

## 9. Expected output
Reports page data for a week of staging traffic matches manual counts; a test organization upgrades through Stripe Checkout (test mode) and gets the new limits within seconds.

## 10. Completion criteria
- [ ] Analytics tests pass; report endpoints p95 < 500 ms on the reference dataset (replica).
- [ ] Billing flows pass in Stripe test mode, including failed payment → past_due → suspension → reactivation.
- [ ] Billing can be disabled entirely (`BILLING_ENABLED=false`) for self-hosted installs, with plan limits read from configuration.
