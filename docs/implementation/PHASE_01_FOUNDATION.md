# Phase 1 — Foundation and Database (Steps 1–2)

## 1. Objective
A running, tested, deployable skeleton with the full database schema, so every later phase only adds modules.

## 2. Features
- Repository, tooling, CI.
- Settings, structured logging, error handling, request ids, health endpoints.
- Async and sync SQLAlchemy engines; unit of work with outbox + after-commit hooks; tenant binding helper; system session.
- Alembic baseline that creates the complete schema (66 tables), functions, roles, RLS; reference seed data.
- Encryption service (local keys), token helpers.
- Redis client, locks, rate limiter primitives.
- Celery app with queues and base task classes; beat with an empty schedule; relay process skeleton.
- Storage backends (local, S3) behind the protocol.
- Test harness with testcontainers.

## 3. Files to create
```text
pyproject.toml, uv.lock, .pre-commit-config.yaml, .env.example, Dockerfile, docker-compose.yml, alembic.ini
src/converse/settings.py
src/converse/core/{logging.py, telemetry.py, errors.py, clock.py, cache.py, locks.py, ratelimit.py}
src/converse/core/db/{engine.py, session.py, tenant.py, system.py, base.py, types.py}
src/converse/core/tenancy/context.py
src/converse/core/events/{outbox.py, schemas.py, relay.py}
src/converse/core/crypto/{encryptor.py, tokens.py}
src/converse/core/storage/{base.py, local.py, s3.py}
src/converse/core/http/{client.py, safe.py}
src/converse/api/{app.py, deps/session.py, middleware/request_id.py, middleware/logging.py, middleware/body_limit.py}
src/converse/api/routes/health.py
src/converse/workers/{celery_app.py, base.py}
src/converse/db/migrations/{env.py, script.py.mako}
src/converse/db/migrations/sql/{01_schema.sql, 02_functions_rls.sql}
src/converse/db/migrations/versions/{0001_initial.py, 0002_reference_data.py}
src/converse/cli.py
tests/conftest.py, tests/security/test_rls_coverage.py, tests/unit/core/test_encryptor.py, tests/integration/test_migrations.py
.github/workflows/ci.yml
```

## 4. Database tables
All 66 tables from [../database/INITIAL_SCHEMA_SQL.md](../database/INITIAL_SCHEMA_SQL.md) are created now (cheaper than many small migrations, and later phases only add code). ORM models are added per module in later phases. Reference data (`permissions`, `subscription_plans`) is seeded by `0002_reference_data`.

## 5. APIs
`GET /health/live`, `GET /health/ready`, `GET /metrics`.

## 6. Dependencies
`fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]`, `asyncpg`, `psycopg[binary]`, `alembic`, `redis`, `celery`, `httpx`, `orjson`, `structlog`, `cryptography`, `boto3`, `python-magic`, `opentelemetry-*`, `prometheus-client`, `sentry-sdk`, `typer`; dev: `pytest`, `pytest-asyncio`, `testcontainers`, `respx`, `polyfactory`, `ruff`, `mypy`, `import-linter`, `pip-audit`. Verify each library's maintenance status and latest release before pinning.

## 7. Implementation steps
1. `uv init`, layout from [../backend/PROJECT_STRUCTURE.md](../backend/PROJECT_STRUCTURE.md); configure ruff, mypy (strict for `src/`), pytest, import-linter contracts.
2. `Settings` with every variable from [../deployment/ENVIRONMENT_VARIABLES.md](../deployment/ENVIRONMENT_VARIABLES.md); fail fast on missing required values.
3. Logging (structlog JSON + redaction processor), request-id middleware, error model + exception handlers (`DomainError` → error JSON).
4. Engines and sessions: async for API (`asyncpg`, `statement_cache_size=0` for PgBouncer), sync for workers; `get_session` dependency with one transaction per request.
5. `bind_tenant()` with `set_config(…, true)`; `tenant_session()` (sync); `system_session()` using `DATABASE_SYSTEM_URL`.
6. `UnitOfWork`: `record_event()` (insert into `event_outbox`), `after_commit()` callbacks executed only after a successful commit.
7. Copy the tested SQL into `migrations/sql/`; `0001_initial` executes both files; `0002_reference_data` runs Part A of the seed SQL.
8. `Encryptor` (AES-256-GCM, versioned keys, AAD) + tests; `tokens.py` (random tokens, SHA-256 hashing, HMAC sign/verify, constant-time compare).
9. Redis helpers: `RedisLock` (SET NX PX + token-checked release), token bucket rate limiter.
10. Celery app, queues and routes from [../backend/BACKGROUND_JOBS.md](../backend/BACKGROUND_JOBS.md); `TenantTask` / `SystemTask`.
11. Outbox relay loop (`SKIP LOCKED`, LISTEN/NOTIFY + poll) with an empty handler registry.
12. Storage protocol + local + S3 backends.
13. `SafeHttpClient` with private-range blocking and redirect re-checks.
14. Health routes; Prometheus metrics middleware.
15. CLI: `seed-demo` (Part B seed with real argon2 hashes), `hash-password`.
16. CI: lint, types, import-linter, tests, `alembic upgrade head` + `alembic check`, Docker build, Trivy, `pip-audit`.

## 8. Testing requirements
- Migrations: empty DB → head creates 66 tables; RLS coverage test ([../database/TENANT_ISOLATION.md](../database/TENANT_ISOLATION.md#5-automated-tests-to-add-in-the-codebase)); `test_unbound_session_sees_nothing`; `test_set_local_does_not_leak`.
- Encryptor: round trip, wrong AAD fails, old key version decrypts, rotation re-encrypts.
- Unit of work: events written in the same transaction; `after_commit` callbacks not run on rollback.
- Relay: dispatches pending rows once under concurrency (two relays, no double dispatch).
- `SafeHttpClient`: blocks `127.0.0.1`, `10.0.0.0/8`, `169.254.169.254`, IPv6 loopback, redirect to private IP.
- Lock: second acquire fails; release only by owner.

## 9. Expected output
`docker compose up` starts all processes; `curl /health/ready` returns `{"status": "ok", "db": "ok", "redis": "ok", "migrations": "head"}`; CI green on the main branch.

## 10. Completion criteria
- [ ] All tests above pass in CI with real Postgres (pgvector) and Redis containers.
- [ ] The app connects as a `converse_app` member; an RLS-violating query fails in a test.
- [ ] Import-linter contracts enforced.
- [ ] Image builds and runs as non-root.
