# Phase 7 — Automation, Outgoing Webhooks and Auto-assignment (Step 18 + related R1 features)

## 1. Objective
No-code rules that react to conversation and message events, signed outgoing webhooks for customer systems, and automatic assignment of new conversations.

## 2. Features
- Automation rules: events, conditions, actions, ordering, dry-run test, execution log, loop prevention.
- Macros (R2) reusing the action executor.
- Outgoing webhooks: subscriptions, signing, delivery log, retries, auto-disable, redeliver, test ping.
- Auto-assignment: round robin and least-busy, respecting availability, `accepts_conversations`, capacity limits, team scoping.

## 3. Files to create
```text
src/converse/modules/automation/{models.py, repository.py, service.py, conditions.py, actions.py, executor.py, handlers.py, schemas.py, macros.py}
src/converse/modules/webhooks/{models.py, repository.py, service.py, signer.py, delivery.py, handlers.py, schemas.py}
src/converse/modules/assignment/{service.py, strategies.py, handlers.py}
src/converse/api/routes/{automation.py, webhooks.py}
src/converse/workers/tasks/webhooks.py
tests/integration/automation/*, tests/integration/webhooks/*, tests/integration/assignment/*
```

## 4. Database tables
`automation_rules`, `automation_rule_executions`, `macros`, `webhook_subscriptions`, `webhook_deliveries`, `conversation_assignments`, `inboxes.auto_assignment_config`, `organization_memberships.availability/accepts_conversations`.

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §20 (assignment), §21 (automation, macros), §24 (webhooks).

## 6. Dependencies
Phases 3–5 (events, services, realtime). Phase 4 for channel-specific conditions (inbox, provider).

## 7. Rule model

Events (`automation_rules.event_name`): `conversation.created`, `conversation.updated`, `conversation.opened`, `conversation.resolved`, `message.created` (incoming only by default). `opened`/`resolved` are derived from `conversation.status_changed`. Same event set as Chatwoot's `AutomationRuleListener`.

Conditions (attribute → allowed operators):

| Attribute | Operators | Values |
|---|---|---|
| `message.content` | `contains`, `does_not_contain`, `equal_to`, `starts_with`, `matches_regex` (RE2-safe) | strings |
| `message.direction` | `equal_to` | `incoming`/`outgoing` |
| `conversation.status` / `priority` | `equal_to`, `not_equal_to` | enum values |
| `conversation.inbox_id` / `team_id` / `assignee_id` | `equal_to`, `not_equal_to`, `is_present`, `is_not_present` | ids (validated in the workspace) |
| `conversation.labels` | `contains`, `does_not_contain` | label titles |
| `conversation.custom_attributes.<key>` / `contact.custom_attributes.<key>` | depends on `value_type` (`equal_to`, `is_greater_than`, `is_less_than`, `contains`, …) | typed |
| `contact.email` / `phone_number` / `country_code` | `equal_to`, `contains`, `is_present` | strings |
| `conversation.additional_attributes.referer_url` | `contains`, `starts_with` | strings |
| `conversation.created_during_business_hours` | `equal_to` | boolean |

Conditions combine left to right with `query_operator` (`and`/`or`), `and` binding tighter than `or`.

Actions:

| Action | Params |
|---|---|
| `assign_agent` | `user_id` or `"round_robin"` |
| `assign_team` | `team_id` |
| `unassign` | — |
| `add_label` / `remove_label` | `labels: []` |
| `change_status` | `status` (+ `snoozed_until` offset) |
| `change_priority` | `priority` |
| `send_message` | `content` (canned-response variables allowed) |
| `send_template` | WhatsApp template payload (WhatsApp inboxes only) |
| `add_private_note` | `content` |
| `send_webhook` | `url` (https, SSRF-safe) |
| `set_custom_attribute` | `scope`, `key`, `value` |
| `disable_ai` / `enable_ai` | — |
| `mute_conversation` | — |

## 8. Implementation steps
1. Pydantic discriminated unions for conditions/actions; validation of ids against the workspace on save.
2. Condition evaluator: in-memory evaluation against a loaded snapshot (conversation, contact, message, labels) — no SQL per condition.
3. Executor: runs actions in order through the normal services with `ctx.principal = AutomationPrincipal(rule_id)`; each action's result recorded; one failing action doesn't stop the rest (like Chatwoot's per-action rescue).
4. Handler: on each event, load active rules for `(workspace, event)` ordered by `position`; skip events whose `actor` is an automation (loop prevention, same idea as Chatwoot's `performed_by_automation?`); insert `automation_rule_executions` first (unique `(rule_id, outbox_event_id)`) to guarantee at-most-once per event.
5. Dry-run endpoint using the same evaluator without executing.
6. Webhooks: subscription CRUD (secret encrypted, shown once), fan-out handler creates `webhook_deliveries` per matching subscription, `deliver_webhook` task (SafeHttpClient, 10 s timeout, signature headers, response excerpt), retry schedule, auto-disable, redeliver, test ping.
7. Payload builders per event (stable, documented JSON; include `X-Converse-Delivery`).
8. Auto-assignment handler on `conversation.created` / reopen / handoff: eligible agents = inbox (or team) members who are online/busy-allowed, `accepts_conversations`, below `max_open_per_agent`; round robin via Redis rotating index per inbox (`INCR` modulo eligible list sorted by id) or least-busy by open count.
9. Macros (R2): CRUD + run endpoint using the executor.

## 9. Testing requirements
- Every operator × type combination; `and`/`or` precedence.
- Loop prevention: a rule that adds a label on `conversation.updated` doesn't trigger itself.
- At-most-once: same outbox event delivered twice → one execution.
- Cross-workspace ids in actions rejected at save time.
- Webhooks: signature verifiable with the documented algorithm; retries with respx-simulated 500s; auto-disable after threshold; SSRF targets rejected; redeliver creates a new attempt with the same delivery id.
- Assignment: fair distribution over 1,000 conversations; offline agents skipped; capacity respected; concurrency (parallel creations) never exceed capacity.

## 10. Expected output and completion criteria
- A rule "when message contains *refund* → label *billing*, assign team Tier 1, reply with canned text" works end to end on WhatsApp and widget.
- A webhook endpoint receives signed `message.created` events and can verify them with the sample code in the docs.
- [ ] All tests pass; execution logs and delivery logs visible via API.
