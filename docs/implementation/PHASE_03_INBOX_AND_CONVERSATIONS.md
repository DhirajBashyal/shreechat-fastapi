# Phase 3 — Inboxes, Contacts, Conversations, Messages, Attachments (Steps 6–8, 14)

## 1. Objective
The provider-independent core of the inbox: inbox and channel-connection lifecycle, contacts and identities, conversations with explicit status transitions, messages and notes, attachments, and domain events. Channels in Phase 4 plug into this.

## 2. Features
- Inbox CRUD (settings, members), `channel_connections` + `channel_routes` lifecycle (`InboxService.create_with_connection`, `disconnect`, `reconnect`), encrypted credentials.
- Contacts CRUD, search (trigram), labels (R1), notes (R2), identities, `ContactResolver`, merge (R1).
- Conversations: list with filters + visibility + keyset pagination, counts, detail, status transitions (open/pending/snoozed/resolved), priority, custom attributes, manual assignment + history, participants, read markers, `ConversationResolver`, display numbers.
- Messages: timeline, create outgoing/private note, mentions, activity messages, delivery status model, retries (stub adapter), `create_incoming` for the pipeline.
- Uploads and file downloads with presigned URLs; MIME sniffing; image previews.
- Domain events into the outbox; handler registry with an `outbound` handler using a **fake adapter** for tests.
- Labels, canned responses, custom attribute definitions (R1, but share the same patterns and are small).

## 3. Files to create
```text
src/converse/modules/inboxes/{models.py, repository.py, service.py, schemas.py, routes_registry.py}
src/converse/modules/contacts/{models.py, repository.py, service.py, resolver.py, merge.py, schemas.py}
src/converse/modules/conversations/{models.py, repository.py, service.py, resolver.py, transitions.py, schemas.py, events.py}
src/converse/modules/messaging/{models.py, repository.py, service.py, activity.py, mentions.py, delivery.py, schemas.py, events.py}
src/converse/modules/files/{models.py, service.py, verify.py}
src/converse/modules/labels/*, modules/canned_responses/*, modules/attributes/*
src/converse/channels/{base.py, types.py, registry.py}
src/converse/channels/pipeline/{inbound.py, outbound.py}
src/converse/api/routes/{inboxes.py, contacts.py, conversations.py, messages.py, uploads.py, labels.py, canned_responses.py, attributes.py}
src/converse/workers/tasks/{events.py, outbound.py, media.py, conversations.py}
tests/integration/conversations/*, tests/api/test_conversations.py, tests/api/test_messages.py, tests/contract/test_fake_adapter.py
```

## 4. Database tables
`inboxes`, `inbox_members`, `channel_connections`, `channel_routes`, `stored_files`, `contacts`, `contact_identities`, `conversations`, `messages`, `attachments`, `conversation_assignments`, `conversation_participants`, `mentions`, `labels`, `conversation_labels`, `contact_labels`, `custom_attribute_definitions`, `canned_responses`, `notes`, `notifications` (writes only), `event_outbox`, `idempotency_keys`, `workspace_counters`.

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §7 (inboxes), §13 (contacts), §14 (conversations), §15 (messages), §16 (uploads), §17–19 (labels, canned responses, attributes), §20 (assignment, manual).

## 6. Dependencies
Phase 2 (tenant context, permissions). `python-magic`, `Pillow` (previews), `jinja2` (canned response variables, sandboxed).

## 7. Implementation steps
1. ORM models for all tables above.
2. `InboxService` with a provider-agnostic `create_with_connection(ctx, provider, name, connect_result)` that creates inbox + connection + detail row + route in one transaction; `disconnect`/`reconnect`.
3. `ContactResolver` exactly as in [../backend/MESSAGE_PROCESSING.md](../backend/MESSAGE_PROCESSING.md#12-processing-inbound-worker) (`INSERT … ON CONFLICT DO NOTHING RETURNING`).
4. `ConversationService.transition()` implementing the state diagram in [../database/TABLE_DOCUMENTATION.md](../database/TABLE_DOCUMENTATION.md#conversations); invalid transitions raise `InvalidTransition`.
5. `ConversationResolver` with inbox settings (`lock_to_single_conversation`, `allow_messages_after_resolved`) and `next_workspace_counter`.
6. Visibility filter (`access/visibility.py`) used by list, count, detail and later by the realtime gateway.
7. Conversation list query with indexes from [../database/INDEXES_AND_CONSTRAINTS.md](../database/INDEXES_AND_CONSTRAINTS.md); keyset cursor on `(last_activity_at, id)`; counts cached 10 s.
8. `MessageService.create_outgoing` (validation, window checks via the adapter capability interface, private notes, mentions, `client_message_id` idempotency) and `create_incoming` (idempotent on `external_id`).
9. Activity messages for status/assignment/label changes.
10. `UnitOfWork.record_event` for all events in [../backend/MESSAGE_PROCESSING.md](../backend/MESSAGE_PROCESSING.md#7-domain-events); handler registry; `outbound.handle` → `deliver_outbound_message` with the adapter registry.
11. Uploads: presign → complete → `verify_upload` task; download endpoint with owner authorization.
12. Idempotency-Key middleware/dependency for creates.
13. Beat tasks: `wake_snoozed_conversations`, `auto_resolve_conversations`.
14. Labels, canned responses (render with sandboxed Jinja2), custom attribute definitions + value validation in contact/conversation services.

## 8. Testing requirements
- Transitions: every allowed and disallowed transition; snooze requires a time; wake-up task reopens.
- Resolver: concurrent first messages (two threads) for one identity → one conversation (with the ordering lock) ; resolved + lock_to_single → reopen; resolved + new → new number.
- Messages: idempotent `client_message_id`; private notes never enqueue outbound; mentions create notifications; `external_id` duplicate ignored.
- Visibility: agent sees member-inbox and team conversations only; admin sees all; counts match lists.
- Pagination: stable under inserts; no duplicates across pages.
- Uploads: oversize rejected; MIME mismatch rejected; download of another workspace's file → 404.
- Outbox: each mutation writes the expected events; rollback writes none.
- Performance: seed 1M messages / 100k conversations in one workspace + 1,000 small workspaces; `EXPLAIN ANALYZE` of list, timeline and search queries uses the intended indexes, p95 < 50 ms on the reference DB.

## 9. Expected output
Through the API alone: create an inbox with a fake provider, create a contact, start a conversation, add messages and notes, assign, snooze, resolve; the outbox shows the events and the fake adapter "sends" outgoing messages.

## 10. Completion criteria
- [ ] All tests pass, including the cross-tenant suite for every new route.
- [ ] Query plans reviewed and recorded in the PR.
- [ ] `channels/` contains no provider code yet; `modules/` imports nothing from `channels/`.
