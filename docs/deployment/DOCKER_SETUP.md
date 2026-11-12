# Docker Setup

## 1. Image

One image for every process type; the command decides what runs.

```dockerfile
# Dockerfile
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS runtime
RUN useradd --create-home --uid 10001 converse
COPY --from=deps /app/.venv /app/.venv
COPY src ./src
COPY alembic.ini ./
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
USER converse
EXPOSE 8000
CMD ["uvicorn", "converse.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

Pin the `uv` image tag to the version the team uses; the tag above is an example.

## 2. Process commands

| Process | Command |
|---|---|
| api | `uvicorn converse.api.app:app --host 0.0.0.0 --port 8000 --workers 4` |
| realtime | `uvicorn converse.realtime.app:app --host 0.0.0.0 --port 8001 --ws websockets` |
| worker-fast | `celery -A converse.workers.celery_app worker -Q outbound,inbound,events -c 16 --pool threads` |
| worker-io | `celery -A converse.workers.celery_app worker -Q webhooks,media -c 32 --pool threads` |
| worker-ai | `celery -A converse.workers.celery_app worker -Q ai -c 8 --pool prefork` |
| worker-batch | `celery -A converse.workers.celery_app worker -Q low,maintenance -c 4 --pool prefork` |
| beat | `celery -A converse.workers.celery_app beat` |
| relay | `python -m converse.core.events.relay` |
| migrate (job) | `alembic upgrade head` |

## 3. Development stack (`docker-compose.yml`)

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: converse
      POSTGRES_USER: converse_owner
      POSTGRES_PASSWORD: converse_dev_password
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data", "./docker/postgres/init:/docker-entrypoint-initdb.d:ro"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U converse_owner -d converse"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: converse
      MINIO_ROOT_PASSWORD: converse_dev_password
    ports: ["9000:9000", "9001:9001"]
    volumes: ["miniodata:/data"]

  migrate:
    build: .
    command: ["alembic", "upgrade", "head"]
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }

  api:
    build: .
    command: ["uvicorn", "converse.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]
    env_file: .env
    ports: ["8000:8000"]
    volumes: ["./src:/app/src"]
    depends_on:
      migrate: { condition: service_completed_successfully }
      redis: { condition: service_healthy }

  realtime:
    build: .
    command: ["uvicorn", "converse.realtime.app:app", "--host", "0.0.0.0", "--port", "8001", "--reload", "--reload-dir", "/app/src"]
    env_file: .env
    ports: ["8001:8001"]
    volumes: ["./src:/app/src"]
    depends_on:
      migrate: { condition: service_completed_successfully }

  worker:
    build: .
    command: ["celery", "-A", "converse.workers.celery_app", "worker",
              "-Q", "outbound,inbound,events,webhooks,ai,media,low,maintenance", "-c", "4", "--pool", "threads", "-l", "info"]
    env_file: .env
    volumes: ["./src:/app/src"]
    depends_on:
      migrate: { condition: service_completed_successfully }

  beat:
    build: .
    command: ["celery", "-A", "converse.workers.celery_app", "beat", "-l", "info"]
    env_file: .env
    depends_on:
      migrate: { condition: service_completed_successfully }

  relay:
    build: .
    command: ["python", "-m", "converse.core.events.relay"]
    env_file: .env
    volumes: ["./src:/app/src"]
    depends_on:
      migrate: { condition: service_completed_successfully }

  nginx:
    image: nginx:1.27-alpine
    ports: ["8080:80"]
    volumes: ["./nginx/converse.dev.conf:/etc/nginx/conf.d/default.conf:ro"]
    depends_on: [api, realtime]

volumes:
  pgdata:
  miniodata:
```

`docker/postgres/init/01_roles.sql` creates the login users for development:

```sql
CREATE ROLE converse_api LOGIN PASSWORD 'converse_dev_password';
CREATE ROLE converse_sys LOGIN PASSWORD 'converse_dev_password';
-- group roles converse_app / converse_system are created by the baseline migration;
-- the migration grants membership: GRANT converse_app TO converse_api; GRANT converse_system TO converse_sys;
```

(The baseline migration runs `GRANT converse_app TO converse_api` etc. when those users exist; in production the grants are part of infrastructure provisioning.)

## 4. Local workflow

```bash
cp .env.example .env
docker compose up -d postgres redis minio
uv sync
uv run alembic upgrade head
uv run python -m converse.cli seed-demo
uv run uvicorn converse.api.app:app --reload          # or: docker compose up api realtime worker beat relay
uv run pytest
```

Meta webhooks in development: expose the API with a tunnel (`cloudflared tunnel --url http://localhost:8000` or ngrok) and register `https://<tunnel>/webhooks/meta/whatsapp` etc. in the Meta app dashboard (the same idea as Chatwoot's `Procfile.tunnel`).

## 5. Test stack

Tests use `testcontainers` (`pgvector/pgvector:pg17`, `redis:7-alpine`) started once per session; each test runs in a transaction rolled back at the end (API tests use a nested-transaction session override). No Docker Compose needed for tests.
