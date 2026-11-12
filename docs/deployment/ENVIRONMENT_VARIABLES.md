# Environment Variables

Loaded by `converse/settings.py` (pydantic-settings). Missing required values fail at startup with a clear error. In production, secrets come from the secret manager, never from files in the image.

## Core

| Variable | Required | Example | Notes |
|---|---|---|---|
| `CONVERSE_ENV` | yes | `development` / `staging` / `production` | Enables production checks (HTTPS URLs, no debug) |
| `PUBLIC_BASE_URL` | yes | `https://app.converse.example` | Used for OAuth redirects, webhook URLs, widget SDK URL |
| `DASHBOARD_ORIGINS` | yes | `https://app.converse.example` | CORS allow-list (comma separated) |
| `LOG_LEVEL` | no | `INFO` | |
| `LOG_FORMAT` | no | `json` | `console` in development |

## Database

| Variable | Required | Example | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | `postgresql+asyncpg://converse_api:***@db:5432/converse` | Login user in group `converse_app` (RLS enforced) |
| `DATABASE_SYNC_URL` | yes | `postgresql+psycopg://converse_api:***@db:5432/converse` | Same user, sync driver for workers |
| `DATABASE_SYSTEM_URL` | yes | `postgresql+psycopg://converse_sys:***@db:5432/converse` | Login user in group `converse_system` (BYPASSRLS) |
| `DATABASE_MIGRATION_URL` | migrate job | `postgresql+psycopg://converse_owner:***@db:5432/converse` | Schema owner |
| `DATABASE_POOL_SIZE` | no | `10` | Per process |
| `DATABASE_STATEMENT_TIMEOUT_MS` | no | `5000` (api), `30000` (workers) | |

## Redis

| Variable | Required | Example |
|---|---|---|
| `REDIS_URL` | yes | `rediss://:***@redis:6379/0` (cache, locks, rate limits, presence) |
| `REDIS_PUBSUB_URL` | no | defaults to `REDIS_URL` |
| `CELERY_BROKER_URL` | yes | `rediss://:***@redis:6379/1` |

## Security

| Variable | Required | Example | Notes |
|---|---|---|---|
| `JWT_SIGNING_KEYS` | yes | `{"2": "<base64 32+ bytes>", "1": "<old>"}` | JSON; highest id signs, all verify |
| `JWT_ACCESS_TTL_SECONDS` | no | `900` | |
| `REFRESH_TTL_DAYS` / `REFRESH_ABSOLUTE_TTL_DAYS` | no | `30` / `90` | |
| `ENCRYPTION_KEYS` | yes (dev) | `{"1": "<base64 32 bytes>"}` | Local-key encryptor |
| `ENCRYPTION_ACTIVE_KEY_VERSION` | yes | `1` | |
| `KMS_KEY_ID` | prod | `arn:aws:kms:…` | If set, KMS encryptor is used |
| `PASSWORD_BREACH_CHECK` | no | `hibp` / `local` / `off` | |
| `CAPTCHA_PROVIDER` / `CAPTCHA_SECRET` | no | `turnstile` / `***` | |

## Storage

| Variable | Required | Example |
|---|---|---|
| `STORAGE_BACKEND` | yes | `s3` or `local` |
| `STORAGE_LOCAL_PATH` | if local | `./var/storage` |
| `S3_BUCKET` | if s3 | `converse-prod-files` |
| `S3_REGION` | if s3 | `eu-west-1` |
| `S3_ENDPOINT_URL` | no | `http://minio:9000` (MinIO/R2) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | no | prefer IAM roles |
| `MAX_UPLOAD_MB` | no | `40` |

## Meta (one platform app)

| Variable | Required | Notes |
|---|---|---|
| `META_APP_ID` | for Messenger/WhatsApp | |
| `META_APP_SECRET` | for Messenger/WhatsApp | Verifies `X-Hub-Signature-256` |
| `META_GRAPH_API_VERSION` | yes | e.g. `v24.0`; check Meta's current version at implementation time |
| `META_MESSENGER_VERIFY_TOKEN` | for Messenger | Random string entered in the Meta dashboard |
| `META_WHATSAPP_VERIFY_TOKEN` | for WhatsApp | |
| `META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID` | R1 | Embedded Signup configuration id |
| `META_INSTAGRAM_APP_ID` / `META_INSTAGRAM_APP_SECRET` | for Instagram | Instagram Login app credentials |
| `META_INSTAGRAM_VERIFY_TOKEN` | for Instagram | |

## AI

| Variable | Required | Notes |
|---|---|---|
| `AI_PLATFORM_PROVIDER` | if AI enabled | `openai` / `anthropic` / … (used when the tenant has no own key) |
| `OPENAI_API_KEY` | if used | Platform key |
| `ANTHROPIC_API_KEY` | if used | Platform key |
| `GEMINI_API_KEY` | if used | |
| `AI_MODELS_FILE` | no | `config/llm_models.yaml` |
| `AI_DEFAULT_EMBEDDING_MODEL` | no | Must be in the models file |
| `AI_RUN_DEADLINE_SECONDS` | no | `45` |
| `AI_TRACING` | no | `off` (default) / `langsmith` / `langfuse` (not for production customer data without a DPA) |

## Billing

| Variable | Required | Notes |
|---|---|---|
| `BILLING_ENABLED` | no | `false` for self-hosted |
| `STRIPE_SECRET_KEY` | if billing | |
| `STRIPE_WEBHOOK_SECRET` | if billing | |

## Email (transactional)

| Variable | Required | Example |
|---|---|---|
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | yes | Or `EMAIL_PROVIDER=ses` with IAM |
| `EMAIL_FROM` | yes | `Converse <no-reply@converse.example>` |

## Observability

| Variable | Required | Notes |
|---|---|---|
| `SENTRY_DSN` | no | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | `http://otel-collector:4317` |
| `OTEL_SERVICE_NAME` | no | Set per process: `converse-api`, `converse-worker-fast`, … |
| `METRICS_ENABLED` | no | `true` |

## `.env.example`

```dotenv
CONVERSE_ENV=development
PUBLIC_BASE_URL=http://localhost:8080
DASHBOARD_ORIGINS=http://localhost:5173
LOG_FORMAT=console

DATABASE_URL=postgresql+asyncpg://converse_api:converse_dev_password@localhost:5432/converse
DATABASE_SYNC_URL=postgresql+psycopg://converse_api:converse_dev_password@localhost:5432/converse
DATABASE_SYSTEM_URL=postgresql+psycopg://converse_sys:converse_dev_password@localhost:5432/converse
DATABASE_MIGRATION_URL=postgresql+psycopg://converse_owner:converse_dev_password@localhost:5432/converse

REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1

JWT_SIGNING_KEYS={"1":"ZGV2LW9ubHktand0LWtleS1yZXBsYWNlLW1lLTMyYnl0ZXM="}
ENCRYPTION_KEYS={"1":"ZGV2LW9ubHktZW5jcnlwdGlvbi1rZXktMzJieXRlcyE="}
ENCRYPTION_ACTIVE_KEY_VERSION=1

STORAGE_BACKEND=s3
S3_BUCKET=converse-dev
S3_REGION=us-east-1
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY_ID=converse
S3_SECRET_ACCESS_KEY=converse_dev_password

META_GRAPH_API_VERSION=v24.0
META_APP_ID=
META_APP_SECRET=
META_MESSENGER_VERIFY_TOKEN=change-me-messenger
META_WHATSAPP_VERIFY_TOKEN=change-me-whatsapp
META_INSTAGRAM_APP_ID=
META_INSTAGRAM_APP_SECRET=
META_INSTAGRAM_VERIFY_TOKEN=change-me-instagram

AI_PLATFORM_PROVIDER=openai
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

BILLING_ENABLED=false
SMTP_HOST=localhost
SMTP_PORT=1025
EMAIL_FROM=Converse Dev <dev@localhost>
```

The dev keys above are placeholders (base64 of fixed strings). Generate real keys with `python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`.
