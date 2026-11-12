# Monitoring and Logging

## 1. Logging

structlog, JSON to stdout, collected by the platform (CloudWatch, Cloud Logging, Loki, Datadog).

```json
{"ts": "2026-11-08T10:16:44.120Z", "level": "info", "event": "outbound.sent",
 "service": "converse-worker-fast", "request_id": "req_01JC8Z4X7S3QK2", "task_id": "8c1f…",
 "organization_id": "00000000-0000-0000-0000-00000000b001", "workspace_id": "00000000-0000-0000-0000-00000000e001",
 "inbox_id": "f3a1…", "provider": "whatsapp_cloud", "message_id": "a8d7…", "attempt": 0, "duration_ms": 412}
```

| Rule | Detail |
|---|---|
| Context binding | Middleware binds `request_id` (from `X-Request-ID` or generated), `organization_id`, `workspace_id`, `user_id`; `TenantTask` binds the same for tasks; request id is propagated into task headers |
| Event names | `domain.action` (`inbound.received`, `inbound.duplicate`, `outbound.failed`, `ai.handoff`, `webhook.dead`) so they can be counted |
| PII | Never log message content, emails, phone numbers, tokens, request bodies. Redaction processor as a safety net |
| Levels | `error` = needs a human; `warning` = degraded but handled; `info` = business events; `debug` off in production |
| Access logs | Uvicorn access logs off; a middleware logs method, route template (not raw path with ids), status, duration |
| Retention | 30 days hot, 1 year archive (audit logs live in the DB) |

## 2. Metrics (Prometheus)

| Metric | Type | Labels |
|---|---|---|
| `http_requests_total`, `http_request_duration_seconds` | counter / histogram | route, method, status |
| `webhook_received_total` | counter | provider, outcome (`accepted`, `duplicate`, `invalid_signature`) |
| `inbound_processing_seconds` | histogram | provider |
| `inbound_events_state` | gauge | state |
| `message_end_to_end_seconds` (webhook received → realtime published) | histogram | provider |
| `outbound_send_total` | counter | provider, result (`sent`, `retry`, `failed`), error_code |
| `outbound_send_seconds` | histogram | provider |
| `outbox_pending`, `outbox_oldest_pending_seconds` | gauge | — |
| `celery_queue_length`, `celery_task_wait_seconds`, `celery_task_runtime_seconds`, `celery_task_failures_total` | gauge / histogram / counter | queue, task |
| `realtime_connections` | gauge | kind (agent, visitor) |
| `realtime_events_sent_total` | counter | event |
| `webhook_delivery_total` | counter | result |
| `ai_runs_total` | counter | status |
| `ai_handoff_total` | counter | reason |
| `ai_run_seconds`, `ai_node_seconds` | histogram | node |
| `ai_tokens_total` | counter | model, kind (input, output) |
| `channel_connections` | gauge | provider, status |
| `db_pool_in_use`, `db_query_seconds` | gauge / histogram | pool |

Tenant ids are **not** metric labels (cardinality). Per-tenant analysis uses logs and the database.

## 3. Tracing

OpenTelemetry auto-instrumentation for FastAPI, SQLAlchemy, httpx, Redis and Celery; manual spans for `inbound.pipeline`, each AI node, adapter calls. Trace context propagates from API → Celery headers → worker spans, so one trace shows "webhook → process → AI → outbound send". Sample 10% in production, 100% for errors (tail sampling in the collector).

## 4. Dashboards

1. **Customer message health:** webhooks/s by provider, duplicates, end-to-end latency p50/p95, outbound success rate, failures by error code.
2. **Queues:** depth and wait time per queue, task failure rate, outbox lag.
3. **Realtime:** connections, event rate, publish→deliver latency.
4. **AI:** runs/min, latency, handoff rate by reason, tokens/cost per model, failures.
5. **API:** RPS, error rate, latency by route.
6. **Database:** CPU, connections, slow queries (`pg_stat_statements`), replication lag, table/index sizes (`messages`, `kb_chunks`).
7. **Channels:** connections by status, reauthorization-required count, WhatsApp quality ratings.

## 5. Alerts

| Alert | Condition | Severity |
|---|---|---|
| Webhook ingress errors | 5xx on `/webhooks/*` > 1% for 5 min | page |
| Invalid signatures spike | > 50/min | ticket (possible attack or secret misconfig) |
| Inbound backlog | `inbound` wait p95 > 30 s for 5 min | page |
| Outbound failures | failure rate > 5% for 10 min (per provider) | page |
| Outbox lag | oldest pending > 60 s | page |
| Dead inbound events | any in 15 min | ticket |
| Realtime drop | connections fall > 50% in 5 min | page |
| AI failures | `ai_runs{status=failed}` > 10% for 15 min | ticket |
| Token expiry | Instagram connections expiring < 3 days without refresh | ticket |
| DB | CPU > 80% 15 min, connections > 85%, replication lag > 30 s, storage > 80% | page/ticket |
| Redis | memory > 80%, evictions > 0 | page |
| Certificates | expiry < 14 days | ticket |

## 6. Health endpoints

- `/health/live`: process responds (Kubernetes liveness).
- `/health/ready`: DB `SELECT 1` (through PgBouncer), Redis `PING`, Alembic revision equals head. Readiness only, so a DB blip doesn't restart pods.
- Workers: Celery `inspect ping` via a liveness exec probe every 60 s (similar purpose to Chatwoot's `sidekiq_alive`).
- Realtime: `/health/ready` + Redis subscription alive.

## 7. Error tracking

Sentry for api, realtime and workers: release = git SHA, environment tag, `organization_id` as a tag (not the name), request/body data scrubbed, breadcrumbs without SQL parameters. Alert on new issues in `channels.*` and `ai.*`.
