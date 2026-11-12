# 04 — Technology Stack

Each choice lists **why**, the **alternatives considered**, and any **constraints** developers must follow. Pin exact versions in `pyproject.toml` / `uv.lock` when the project starts and check each library's current release notes at that time.

## 1. Summary

| Concern | Choice | Alternatives |
|---|---|---|
| Language | Python 3.12+ | — |
| Web framework | FastAPI | Litestar, Django + DRF |
| Validation / settings | Pydantic v2, pydantic-settings | attrs + cattrs, marshmallow |
| ORM | SQLAlchemy 2.0 (async, `asyncpg`) | SQLModel, Django ORM, Tortoise |
| Migrations | Alembic | — |
| Database | PostgreSQL 16+ | — |
| Vectors | pgvector (HNSW) | Qdrant, Weaviate, Pinecone |
| Cache / locks / pub-sub | Redis 7 | Valkey (drop-in), NATS (pub-sub only) |
| Background jobs | **Celery 5** (Redis broker) | ARQ, Dramatiq, Taskiq |
| Scheduler | Celery beat (single instance) | APScheduler, cron container |
| Realtime | FastAPI native WebSockets + Redis pub/sub | python-socketio, Centrifugo |
| AI orchestration | LangGraph | Plain function pipeline, LlamaIndex workflows |
| LLM clients | LangChain chat model integrations (`langchain-openai`, `langchain-anthropic`) behind our `LLMProvider` interface | Direct SDKs, LiteLLM |
| HTTP client | httpx (async) | aiohttp |
| Password hashing | argon2-cffi (argon2id) | bcrypt |
| JWT | PyJWT | python-jose (less maintained) |
| Encryption | `cryptography` (AES-256-GCM) | Fernet (simpler, also fine) |
| Storage | S3-compatible (boto3/aioboto3), local disk for dev | MinIO for self-hosted S3 |
| Payments | Stripe (official `stripe` SDK) | Paddle, Chargebee |
| Lint / format | Ruff | flake8 + black + isort |
| Types | mypy (strict on `converse/`) | pyright |
| Tests | pytest, pytest-asyncio, testcontainers, respx | — |
| Logging | structlog (JSON) | stdlib logging + json formatter |
| Tracing / metrics | OpenTelemetry → OTLP; Prometheus metrics | Datadog agent, Sentry performance |
| Errors | Sentry | — |
| Packaging | uv | Poetry, pip-tools |
| Containers | Docker, Docker Compose (dev), Kubernetes or Compose (prod) | — |
| Reverse proxy | Nginx | Traefik, Caddy |

## 2. Details

### FastAPI
- **Why:** async-first (we spend most time waiting on Postgres, Redis and Meta), WebSocket support built in, dependency injection that fits tenant/auth scoping, automatic OpenAPI for the public API.
- **Alternatives:** *Litestar* is similar and slightly faster, but has a smaller ecosystem. *Django + DRF* gives an admin and ORM out of the box, but async support is partial and the ORM is weaker for composite keys and RLS session settings.
- **Rule:** no business logic in route functions (see [backend/PROJECT_STRUCTURE.md](backend/PROJECT_STRUCTURE.md)).

### Pydantic v2
- **Why:** request/response schemas, settings, normalised channel types (`InboundMessage`), automation condition/action schemas, LLM structured output. One validation library everywhere.
- **Rule:** API schemas (`schemas/`) are separate from ORM models. Never return ORM objects directly.

### SQLAlchemy 2.0 + asyncpg
- **Why:** mature, explicit SQL control, composite FKs, partial indexes, `ON CONFLICT`, typed `Mapped[]` models. Async engine for the API. Workers use the same models with a sync `psycopg` engine (Celery is synchronous).
- **Alternatives:** *SQLModel* merges Pydantic and ORM models, which we explicitly don't want. *Tortoise* is less mature.
- **Rules:** queries go through repositories; no raw SQL strings in application code except the documented functions (`next_workspace_counter`) and `SET LOCAL app.current_org_id`, which goes through a single helper.

### Alembic
- **Why:** the standard for SQLAlchemy. The initial migration executes the SQL in [database/INITIAL_SCHEMA_SQL.md](database/INITIAL_SCHEMA_SQL.md). Later migrations use `op.*` APIs. Autogenerate is only a starting point and every migration is reviewed by hand.

### PostgreSQL 16+
- **Why:** RLS, JSONB, partial and GIN indexes, `pg_trgm` search, pgvector in the same database (one backup, one transaction for document + chunks). The schema uses `ON DELETE SET NULL (column)`, which needs PostgreSQL 15+.
- **Constraint:** managed Postgres must allow the `vector`, `pg_trgm`, `pgcrypto`, `citext` extensions (RDS, Cloud SQL, Supabase, Neon and Crunchy all do).

### pgvector
- **Why:** keeps embeddings next to tenant data under the same RLS. HNSW index gives good recall without training (unlike IVFFlat, which Chatwoot uses and which needs `lists` tuning and data present at build time).
- **When to move off it:** above roughly 50–100M chunks, or when filtered search latency becomes a problem. The `Retriever` interface keeps the swap to Qdrant contained.

### Redis 7
- **Uses:** Celery broker and result backend (results are rarely needed), WebSocket pub/sub, presence, rate-limit counters, short-lived locks, OAuth `state` storage, idempotency fast-path.
- **Rule:** Redis is never the source of truth for anything that must survive a restart. Durable state lives in Postgres.

### Celery vs ARQ — recommendation: **Celery**

| Criterion | Celery | ARQ |
|---|---|---|
| Maturity / community | Very large, 15+ years | Small, maintained by one main author |
| Priority queues & routing | Yes (separate queues per priority, worker per queue set) | Single queue per worker; priorities are limited |
| Scheduling | Celery beat | Built-in cron |
| Retries with backoff | Built-in (`autoretry_for`, `retry_backoff`, `max_retries`) | Built-in, simpler |
| `acks_late` / redelivery on crash | Yes | Yes (job stays in Redis until done) |
| Rate limits per task | Yes | No |
| async code | Tasks are sync. Run async code with `asyncio.run` or use the sync engine | Native asyncio |
| Monitoring | Flower, Prometheus exporters | Minimal |

Converse needs strict queue priorities (realtime > outbound sends > inbound processing > webhooks > AI > housekeeping, mirroring Chatwoot's Sidekiq queue order), per-task rate limits (Meta send API), and mature operational tooling. That favours **Celery**. The cost is sync tasks, handled by giving workers a sync SQLAlchemy engine and sync `httpx.Client`. The AI graph runs in its own worker pool with `asyncio.run()` per task, so LangGraph's async APIs can be used.

*When ARQ would be better:* a small, fully async team that doesn't need queue priorities. Keeping tasks thin (each task calls a service function) means switching later only touches `converse/workers/`.

### Realtime: native WebSockets + Redis pub/sub
- **Why:** the dashboard and widget need one bidirectional channel with a small, documented JSON protocol. Native WebSockets avoid Socket.IO's extra protocol layer, and every client language can speak them. Fan-out across API replicas uses Redis pub/sub channels per organization, user and contact.
- **Alternatives:** *python-socketio* (rooms, auto-reconnect, long-polling fallback) is a good choice if the frontend team prefers the Socket.IO client. The event names and payloads in [backend/REALTIME_COMMUNICATION.md](backend/REALTIME_COMMUNICATION.md) stay the same either way. *Centrifugo* is a standalone server worth considering above ~100k concurrent connections.

### LangGraph + LangChain
- **Why LangGraph:** the AI agent is a state machine with branches (intent routing, tool use, guardrails, handoff), retries and persisted state. LangGraph models exactly that, gives checkpointing (`PostgresSaver`) and supports human-in-the-loop interrupts.
- **Why LangChain only for model/tool integrations:** we use `langchain-openai` / `langchain-anthropic` chat models and the tool-calling interface, wrapped behind our own `LLMProvider` protocol so business code doesn't depend on LangChain types. We do **not** use LangChain's high-level agents or retrieval chains, since retrieval is our own SQL over pgvector.
- **Model choice:** never hard-code model names. Each tenant's `ai_agents.chat_model` is validated against a platform allow-list in configuration (`config/llm_models.yaml`). Before adding an OpenAI model, check OpenAI's deprecations page and skip models with a shutdown date. Same for Anthropic's model deprecation list.

### httpx
- **Why:** sync and async clients with the same API, timeouts, HTTP/2, `respx` for test mocking. All Meta Graph API calls go through one `MetaGraphClient` with timeouts, retry rules and error classification.

### Security libraries
- **argon2-cffi** for passwords (argon2id, OWASP-recommended parameters).
- **PyJWT** for access tokens (HS256 with a rotated secret, or EdDSA if tokens must be verified by other services).
- **cryptography** AES-256-GCM for envelope encryption of credentials (see [06_DATABASE_ARCHITECTURE.md](06_DATABASE_ARCHITECTURE.md#5-secrets-and-credentials)).
- **pyotp** for TOTP MFA.
- **python-magic** (libmagic) for MIME sniffing on uploads.

### Storage
- S3-compatible through a `StorageBackend` protocol with `LocalStorage` (dev/test) and `S3Storage` (prod; AWS S3, Cloudflare R2, MinIO, GCS interop). Files are referenced only by `stored_files.id`, so the backend can change.

### Tooling
- **uv** for dependency management (fast, lockfile, Python version management).
- **Ruff** for lint + format; **mypy** strict for `converse/`, relaxed for `tests/`.
- **pytest** with **testcontainers** (real Postgres with pgvector, real Redis) for integration tests. No SQLite: RLS, JSONB and pgvector behaviour must be tested on Postgres.
- **pre-commit** running ruff, mypy and an import-boundary check (`import-linter`).

### Observability
- **structlog** JSON logs with `request_id`, `organization_id`, `workspace_id`, `task_id` bound automatically.
- **OpenTelemetry** auto-instrumentation for FastAPI, SQLAlchemy, httpx, Celery, Redis.
- **Prometheus** metrics endpoint on API and workers.
- **Sentry** for exceptions, with PII scrubbing (message content is never sent).
