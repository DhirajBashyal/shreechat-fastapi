# Phase 5 — Real-time Messaging (Step 9, widget sockets for Step 13)

## 1. Objective
Agents and visitors receive conversation and message updates instantly, with correct visibility filtering, presence and typing indicators.

## 2. Features
- WebSocket gateway service (`/ws`, `/ws/widget`), auth frame, origin checks, heartbeats.
- Redis pub/sub fan-out with per-replica subscription management.
- `RealtimePublisher` wired into `UnitOfWork.after_commit` for all events in [../backend/REALTIME_COMMUNICATION.md](../backend/REALTIME_COMMUNICATION.md#4-protocol).
- Visibility filtering identical to REST.
- Presence (online/busy/offline, auto-offline), typing indicators, read markers.
- Notifications over `rt:user:{id}` (mentions, assignment).
- Reconnect/recovery contract (`updated_since`, `after_seq`).

## 3. Files to create
```text
src/converse/realtime/{app.py, gateway.py, protocol.py, presence.py, auth.py, subscriptions.py}
src/converse/core/realtime/publisher.py
src/converse/modules/access/visibility.py          # shared predicate (REST + gateway), extended
src/converse/modules/notifications/{models.py, service.py, handlers.py}
src/converse/workers/tasks/presence.py
tests/realtime/test_gateway_auth.py, test_visibility.py, test_presence.py, test_fanout_multi_replica.py
```

## 4. Database tables
Reads `organization_memberships`, `inbox_members`, `team_members`, `workspace_memberships`, `contact_identities`; writes `notifications`, `conversations.agent_last_seen_at` / `contact_last_seen_at`.

## 5. APIs
WebSocket protocol in [../backend/REALTIME_COMMUNICATION.md](../backend/REALTIME_COMMUNICATION.md); REST: `POST /conversations/{id}/typing`, `POST /conversations/{id}/read`, `PATCH /organizations/{org}/me/availability`, notifications list/read endpoints, `GET /conversations?updated_since=`.

## 6. Dependencies
Phase 3 (events and services). Libraries: `websockets` (Uvicorn WS implementation), `redis.asyncio`.

## 7. Implementation steps
1. Gateway app: connection manager, auth frame with 5 s timeout, close codes (`4401` unauthorized, `4403` forbidden, `4408` auth timeout).
2. Agent connection state: memberships, permissions, inbox/team ids (cached, refreshed on `permissions_changed`).
3. Subscription manager: one Redis subscription per channel per replica; reference counting.
4. Envelope format with `workspace_id`, `audience`, conversation ref (`inbox_id`, `team_id`, `assignee_id`); publisher helpers per event.
5. Visibility predicate shared with REST; property-based test comparing both.
6. Visitor sockets: visitor JWT, `allowed_origins`, `rt:contact:{token}`; only non-private message events.
7. Presence counters, heartbeat, sweeper, auto-offline delayed task; `presence.update` debounce.
8. Typing (6 s auto-off) and read markers; provider typing/read for Messenger/WhatsApp via adapter `mark_read`.
9. Notifications service and handlers (mention, assignment, new message in participating conversation).
10. Recovery endpoints (`updated_since`, `after_seq`) and client contract documented in OpenAPI.

## 8. Testing requirements
- Auth: missing/expired/invalid token → close codes; token in URL rejected.
- Visibility: agent without inbox membership never receives that inbox's events; private notes never reach visitor sockets.
- Multi-replica: two gateway instances + one publisher → events reach sockets on both.
- Presence: connect/disconnect counts, crash sweep, auto-offline after 5 min.
- Load: k6 10,000 sockets on one replica, 1,000 events/s, p95 latency < 150 ms.

## 9. Expected output
Two agent browsers and one widget visitor: a visitor message appears for both agents immediately; agent typing appears in the widget; presence dots update.

## 10. Completion criteria
- [ ] Tests and load test pass.
- [ ] REST and realtime visibility proven identical by the property test.
- [ ] Gateway drains cleanly on rolling deploy (clients reconnect without losing state).
