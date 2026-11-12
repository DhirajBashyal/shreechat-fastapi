# Phase 4 — Channel Integrations (Steps 10–13, 14 provider media)

## 1. Objective
Real customer traffic: website widget, WhatsApp Cloud API, Facebook Messenger (MVP) and Instagram (R1), all through the shared adapter contract and pipeline.

## 2. Features
- Webhook ingress for Meta (verify handshake, signature, routing, recording, enqueueing).
- Inbound pipeline with ordering locks, dedup, contact/conversation resolution, media download.
- Outbound dispatcher with retries, rate limiting, error mapping, connection status changes.
- **Website widget**: widget settings API, public widget API, visitor sessions, identify (HMAC), uploads; SDK + iframe app contract (frontend team builds the UI).
- **WhatsApp**: manual setup, templates sync + picker data, all inbound types, statuses, 24 h window, media upload/download, health checks. Embedded Signup in R1.
- **Messenger**: OAuth, page selection, subscription, inbound/echo/delivery/read, profiles, HUMAN_AGENT tag, deauthorize/data-deletion callbacks.
- **Instagram** (R1): Instagram Login OAuth, token refresh, inbound/echo/read, outbound.

## 3. Files to create
```text
src/converse/channels/pipeline/ingress.py
src/converse/channels/web_widget/adapter.py
src/converse/channels/meta/common/{graph_client.py, signature.py, errors.py, oauth.py, signed_request.py}
src/converse/channels/meta/whatsapp/{adapter.py, parser.py, client.py, templates.py, media.py, schemas.py, embedded_signup.py}
src/converse/channels/meta/messenger/{adapter.py, parser.py, client.py, oauth.py, schemas.py}
src/converse/channels/meta/instagram/{adapter.py, parser.py, client.py, oauth.py, schemas.py}
src/converse/api/webhooks/meta.py
src/converse/api/routes/channels/{messenger.py, instagram.py, whatsapp.py, widget_settings.py, oauth_callbacks.py}
src/converse/api/routes/widget/{config.py, sessions.py, conversations.py, messages.py, uploads.py, contact.py}
src/converse/workers/tasks/{inbound.py, channels.py}
tests/fixtures/channels/{whatsapp,messenger,instagram}/*.json
tests/unit/channels/*, tests/contract/test_adapter_contract.py, tests/e2e/test_inbound_to_reply.py
widget-sdk/ (separate package: loader, postMessage bridge, public API)
```

## 4. Database tables
`facebook_pages`, `instagram_accounts`, `whatsapp_business_accounts`, `whatsapp_phone_numbers`, `whatsapp_message_templates`, `web_widgets`, `inbound_webhook_events` (+ all Phase 3 tables).

## 5. APIs
[../backend/API_DESIGN.md](../backend/API_DESIGN.md) §8–12, §28; widget API in [../integrations/WEBSITE_CHAT.md](../integrations/WEBSITE_CHAT.md#3-server-api-apiv1widget).

## 6. Dependencies
Phases 3 and 5 (realtime core). External: Meta developer app (Messenger, Instagram, WhatsApp products), test WABA + test number, test Facebook Page, Instagram professional account; tunnel for local webhooks. **Start Meta App Review at the beginning of this phase.**

## 7. Implementation steps
1. Shared: `verify_meta_signature`, verify handshake routes, `Ingress.record_and_enqueue`, `process_inbound_event` task, `InboundPipeline`, ordering lock, sweeper task.
2. Record real payload fixtures for each message type from the Meta test tools; scrub ids/PII.
3. **Website widget** (week 1–2): settings API → public API → visitor tokens → identify → realtime visitor channel (with Phase 5) → `WebWidgetAdapter.send_message` publishes → SDK contract test page.
4. **WhatsApp** (parallel): manual setup `connect()` validation → parser for every type in [../integrations/WHATSAPP.md](../integrations/WHATSAPP.md#message-types--normalised) → statuses → media download → outbound text/media/interactive/template → 24 h window → error mapping → health and template sync tasks.
5. **Messenger**: OAuth (state in Redis) → page list → connect/subscribe → parser + echo matching via `metadata` → watermark statuses → profile fetch → outbound + HUMAN_AGENT → disconnect → deauthorize/data-deletion callbacks.
6. **Instagram** (R1): Instagram Login OAuth → long-lived token → subscribe → parser (stories, shares, reactions, read) → echo matching → outbound → `refresh_expiring_tokens`.
7. Reauthorization flows for each Meta product attach to the existing inbox.
8. Admin-facing errors: `channel_connections.last_error` surfaced via inbox API; `inbox.connection_changed` realtime event.

## 8. Testing requirements
- Parser unit tests for **every** fixture; unknown types → `unsupported`, never an exception.
- Signature: valid, invalid, missing header, wrong secret, body modified by one byte.
- Dedup: same webhook delivered 3× → one message; same `wamid` in two different payloads → one message.
- Album: 5 concurrent image webhooks → one conversation, 5 messages in order (real Redis, real workers in the e2e test).
- Echo race (Messenger, Instagram): echo before/after send result → one message with `external_id`.
- Outbound: retryable errors retry with backoff (respx), permanent errors fail with the mapped code, `190` → `reauthorization_required`.
- 24 h window and template validation.
- Widget: origin enforcement; unverified identify never merges; verified identify merges history; visitor can't read other identities' conversations.
- E2E: recorded WhatsApp webhook → message visible over WebSocket to an agent → agent reply → respx asserts the Graph API payload → status webhook → `delivered`.
- Soak: replay 10,000 recorded webhooks with random duplication and reordering; assert message counts and no duplicate conversations.

## 9. Expected output
In staging, messages sent from a phone on WhatsApp, from Messenger, and from a test website appear in the inbox within ~1 s; agent replies arrive on the phone; ticks update.

## 10. Completion criteria
- [ ] MVP channels (widget, WhatsApp, Messenger) pass all tests and the soak test.
- [ ] Meta App Review submitted (approved before production launch).
- [ ] Runbook for token expiry/reauthorization and for dead-letter replay written.
- [ ] Import-linter confirms adapters don't import `modules/`.
