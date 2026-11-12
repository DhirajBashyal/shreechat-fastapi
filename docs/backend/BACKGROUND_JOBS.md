# Background Jobs

Celery 5 with a Redis broker (rationale in [../04_TECHNOLOGY_STACK.md](../04_TECHNOLOGY_STACK.md#celery-vs-arq--recommendation-celery)). Tasks are **thin**: they bind the tenant, call one service method, and translate exceptions into retry decisions.

## 1. Queues

Mirrors the intent of Chatwoot's ordered Sidekiq queues (`critical`, `high`, `medium`, `default`, `low`, …; `config/sidekiq.yml`), but with **separate worker deployments** per queue group instead of one worker draining queues in priority order. A flood of low-priority work can then never delay a customer reply.

| Queue | Work | Worker deployment | Concurrency (start) | Time limit |
|---|---|---|---|---|
| `outbound` | `deliver_outbound_message` | `worker-fast` | 16 (threads) | 60 s |
| `inbound` | `process_inbound_event`, delivery updates | `worker-fast` | 16 | 60 s |
| `events` | `handle_event` for realtime-adjacent handlers (notifications, assignment, analytics) | `worker-fast` | 8 | 30 s |
| `webhooks` | `deliver_webhook` | `worker-io` | 32 (gevent/threads) | 30 s |
| `ai` | `run_ai_agent`, `summarize_ai_session` | `worker-ai` | 8 | 120 s |
| `media` | `download_inbound_media`, image previews, MIME scan | `worker-io` | 16 | 300 s |
| `low` | KB ingestion, template sync, token refresh, health checks, exports, imports | `worker-batch` | 4 | 900 s |
| `maintenance` | Purges, outbox pruning, re-encryption, reindexing | `worker-batch` | 2 | 3600 s |

```python
# converse/workers/celery_app.py
app = Celery("converse", broker=settings.redis_broker_url)
app.conf.update(
    task_acks_late=True,                 # redeliver if a worker dies mid-task
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,        # fair dispatch for long tasks
    task_serializer="json",
    accept_content=["json"],
    task_default_queue="events",
    broker_transport_options={"visibility_timeout": 3700},   # > longest time limit
    task_routes={
        "converse.workers.tasks.outbound.*": {"queue": "outbound"},
        "converse.workers.tasks.inbound.*": {"queue": "inbound"},
        "converse.workers.tasks.webhooks.*": {"queue": "webhooks"},
        "converse.workers.tasks.ai.*": {"queue": "ai"},
        "converse.workers.tasks.media.*": {"queue": "media"},
        "converse.workers.tasks.knowledge.*": {"queue": "low"},
        "converse.workers.tasks.channels.*": {"queue": "low"},
        "converse.workers.tasks.maintenance.*": {"queue": "maintenance"},
    },
)
```

**Consequence of `acks_late`:** a task can run more than once (worker crash after the work but before the ack). Every task must be idempotent; see §4.

## 2. Task base classes

```python
# converse/workers/base.py
class TenantTask(Task):
    """Binds organization_id for RLS and logging. Every tenant task takes organization_id as a kwarg."""
    abstract = True

    def __call__(self, *args, organization_id: str, **kwargs):
        org = UUID(organization_id)
        structlog.contextvars.bind_contextvars(organization_id=organization_id, task=self.name, task_id=self.request.id)
        if not tenant_is_processable(org):            # suspended / pending deletion
            return None                               # inbound events stay recorded; see 09_MULTI_TENANCY.md
        with tenant_session(org) as session:
            return self.run(*args, session=session, **kwargs)


class SystemTask(Task):
    """Cross-tenant scans. May only enqueue TenantTasks; never writes tenant data itself."""
    abstract = True
```

Task example:

```python
@app.task(base=TenantTask, bind=True, max_retries=6, autoretry_for=(RetryableChannelError,),
          retry_backoff=10, retry_backoff_max=1200, retry_jitter=True)
def deliver_outbound_message(self, message_id: str, *, session) -> None:
    OutboundDispatcher.from_session(session).deliver(UUID(message_id), attempt=self.request.retries)
```

## 3. Scheduled tasks (Celery beat, single replica)

| Task | Schedule | What |
|---|---|---|
| `wake_snoozed_conversations` | every 1 min | `status = snoozed AND snoozed_until <= now()` → enqueue per-conversation reopen |
| `auto_resolve_conversations` | every 5 min | Workspaces with `auto_resolve_after_minutes`: open conversations idle beyond it → resolve with an activity message (like Chatwoot's `trigger_conversations_resolution_job`) |
| `sweep_inbound_events` | every 1 min | `received` events older than 1 min (broker was down) → re-enqueue; `processing` older than 10 min → back to `failed` |
| `sweep_outbox` | every 1 min | Safety net if the relay process is down: dispatch pending rows older than 30 s |
| `retry_webhook_deliveries` | every 1 min | `webhook_deliveries` due for retry |
| `refresh_expiring_tokens` | daily 02:00 UTC | Instagram tokens expiring within 10 days |
| `check_channel_health` | every 6 h | WhatsApp number quality, Meta token validity |
| `sync_whatsapp_templates` | daily 03:00 UTC | Per WABA |
| `resync_kb_documents` | daily 04:00 UTC | URL documents older than their re-sync interval |
| `offline_inactive_agents` | every 1 min | `auto_offline` agents with no socket for 5 min |
| `prune_outbox` / `prune_inbound_events` / `prune_webhook_deliveries` | daily | Retention (7 / 14 / 30 days) in batches |
| `purge_deleted_organizations` | daily 01:00 UTC | Organizations `pending_deletion` for 30 days → batched purge |
| `expire_idempotency_keys` | hourly | Delete expired rows |
| `reconcile_billing` | daily | Compare `subscriptions` with Stripe, fix drift, alert |

Beat runs as **exactly one** replica (Kubernetes `replicas: 1` + `Recreate` strategy). Every scheduled task is safe if it runs twice (e.g. during a deploy overlap) because the work it enqueues is idempotent.

## 4. Idempotency rules

| Task | Idempotency key |
|---|---|
| `process_inbound_event` | `inbound_webhook_events` state machine (`claim` only from `received`/`failed`); `messages (inbox_id, external_id)` |
| `deliver_outbound_message` | Skip if the message already has `external_id` or `delivery_status in (sent, delivered, read)`. The **only** non-idempotent step is the provider API call itself: if a worker dies after Meta accepted but before we stored `external_id`, a retry sends a duplicate. Mitigations: Messenger `metadata` = our id (the echo links it); WhatsApp has no client-side id, so we accept a small duplicate risk and keep the window short (store `external_id` immediately after the response, before anything else) |
| `handle_event` | Each handler's own unique key (`automation_rule_executions`, `webhook_deliveries`, `ai_runs`, …) |
| `run_ai_agent` | `ai_runs (session_id, trigger_message_id)` unique + `client_message_id = ai:{run_id}` |
| `download_inbound_media` | Skip if `attachments.file_id` already set |
| `deliver_webhook` | `webhook_deliveries` row state; receivers dedupe on `X-Converse-Delivery` |
| Usage metering | `usage_records (organization_id, metric, source_id)` unique |

## 5. Retries and dead letters

| Task | Retries | Backoff | Final state ("dead letter") | Recovery |
|---|---|---|---|---|
| `process_inbound_event` | 5 | 5 s → 5 min, jitter | `inbound_webhook_events.state = dead`, `last_error` | Platform admin replay (`POST /platform/inbound-events/{id}/replay`) after a fix |
| `deliver_outbound_message` | 6 | 10 s → 20 min | `messages.delivery_status = failed`, `delivery_error` | Agent "Retry" button |
| `deliver_webhook` | 8 | 1 min, 5 min, 15 min, 1 h, 3 h, 6 h, 12 h, 24 h | `webhook_deliveries.state = dead`; after 20 consecutive dead deliveries the subscription is disabled and admins are emailed | Redeliver endpoint; re-enable subscription |
| `handle_event` (automation) | 3 | 10 s → 2 min | `automation_rule_executions.error` | Visible in the rule log |
| `run_ai_agent` | 0 (graph retries inside nodes) | — | `ai_runs.status = failed` + fallback handoff | Automatic handoff |
| `download_inbound_media` | 5 | 30 s → 30 min | Attachment `meta.download_error` | Agent sees "Media unavailable" |
| KB ingestion | 3 | 1 min → 30 min | `kb_documents.status = failed` | Resync button |

The dead-letter store is the **owning table's state column**, not a separate Redis queue. Dead items are queryable, visible in the right UI, and replayable with the same code path. Celery's own failure results are not relied on (`task_ignore_result = True`).

## 6. Fairness between tenants

- Each task carries `organization_id`. A Redis counter `inflight:{queue}:{org}` (INCR on start, DECR in `finally`, TTL safety) limits concurrency per tenant per queue (default: `ai` 5, `webhooks` 10, `media` 8, `low` 2).
- Over the limit, the task re-schedules itself with `countdown = 2–5 s` (randomised) instead of occupying a worker slot. That is not counted as a failure retry.
- Large batch operations (imports, bulk actions, exports) are chunked into tasks of ≤ 500 items.

## 7. Monitoring

- Prometheus metrics via `celery-exporter`: queue depth per queue, task latency (enqueue → start), runtime, failures, retries.
- Alerts: `inbound` or `outbound` wait time p95 > 30 s; outbox oldest pending > 60 s; any `dead` inbound event in the last 15 minutes; webhook dead-letter rate > 5% per subscription.
- Flower (internal only, behind auth) for ad-hoc inspection.

## 8. Rules for writing tasks

1. Task arguments are ids and small scalars only (JSON serialisable). Never pass ORM objects or large payloads; load fresh state inside the task.
2. Tasks call services. No business logic in task functions.
3. Set an explicit `queue`, `time_limit`, retry policy and idempotency strategy for every task.
4. Enqueue only **after commit** (`uow.after_commit(lambda: task.delay(...))`), or via the outbox. Never enqueue inside a transaction that might roll back.
5. Don't use `Thread`s inside tasks; use more tasks or a Celery `group`/`chord` when work must fan out.
