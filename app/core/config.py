import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application settings
    ENV: str = "local"
    DEBUG: int = 0

    # Database settings
    DB_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/shreechat-ai"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Redis settings
    REDIS_URL: str = "redis://localhost:6379"

    # Embedding service settings
    EMBEDDING_SERVICE_URL: str = "http://localhost:8001/embeddings"

    # API settings
    API_V1_PREFIX: str = "/api/v1"

    # Auth settings (docs/10_AUTHENTICATION_AND_RBAC.md)
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    JWT_SECRET_KEY: str = ""
    JWT_ISSUER: str = "shreechat"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30  # "remember me" sliding window
    REFRESH_TOKEN_SHORT_TTL_HOURS: int = 24  # without "remember me"
    REFRESH_TOKEN_ABSOLUTE_MAX_DAYS: int = 90
    PASSWORD_RESET_TTL_MINUTES: int = 60
    INVITATION_TTL_DAYS: int = 7
    TRIAL_PLAN_CODE: str = "business_monthly"
    TRIAL_DAYS: int = 14
    # Brute-force guard on POST /auth/login, per client IP (slowapi syntax).
    # Loosen only in local dev, e.g. LOGIN_RATE_LIMIT=1000/minute in .env.
    LOGIN_RATE_LIMIT: str = "5/5minutes"
    # AI agent (docs/ai/AI_ARCHITECTURE.md §4). Platform LLM used when an agent
    # has no tenant credential. AI_PROVIDER: openai | gemini | anthropic.
    AI_PROVIDER: str = "openai"
    AI_API_KEY: str = ""
    AI_CHAT_MODEL: str = "gpt-4o-mini"
    AI_CLASSIFIER_MODEL: str = ""
    # Fernet key for tenant AI provider keys (ai_provider_credentials). Empty in
    # local/test → derived from JWT_SECRET_KEY. Generate:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    AI_CREDENTIALS_KEY: str = ""
    AI_RUN_DEADLINE_SECONDS: int = 45
    # Total tokens (all nodes) one run may spend before it hands off.
    AI_RUN_TOKEN_BUDGET: int = 20_000
    # Concurrent AI runs per organization (queue worker reschedules beyond it).
    AI_MAX_CONCURRENT_RUNS_PER_ORG: int = 5
    # Wait for more customer messages before answering (last message wins).
    AI_DEBOUNCE_SECONDS: float = 2.5
    # LangSmith/Langfuse tracing through LangChain callbacks. Off by default:
    # traces contain customer content.
    AI_TRACING: bool = False
    # Prometheus-format metrics at GET /metrics. Set METRICS_TOKEN to require
    # "Authorization: Bearer <token>".
    METRICS_ENABLED: bool = True
    METRICS_TOKEN: str = ""

    # Owner-deleted organizations can be restored for this long, then purged.
    ORGANIZATION_DELETION_GRACE_DAYS: int = 30

    # Frontend URL used to build links in emails (password reset, invitations)
    FRONTEND_URL: str = "http://localhost:3000"

    # Website live chat (docs/integrations/WEBSITE_CHAT.md). WIDGET_APP_URL is
    # where widget-sdk / widget-app are served (defaults to FRONTEND_URL); its
    # origin is always allowed, next to each widget's allowed_origins.
    WIDGET_APP_URL: str = ""
    WIDGET_VISITOR_TOKEN_TTL_DAYS: int = 30
    WIDGET_SESSION_RATE_LIMIT: str = "5/minute"  # per IP
    WIDGET_MESSAGE_RATE_LIMIT: str = "20/minute"  # per visitor

    # Meta channels (docs/integrations/{WHATSAPP,FACEBOOK_MESSENGER,INSTAGRAM}.md).
    # One platform Meta app serves all tenants.
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_GRAPH_API_VERSION: str = "v24.0"
    META_GRAPH_URL: str = "https://graph.facebook.com"
    META_MESSENGER_VERIFY_TOKEN: str = ""
    META_WHATSAPP_VERIFY_TOKEN: str = ""
    META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID: str = ""
    META_INSTAGRAM_APP_ID: str = ""
    META_INSTAGRAM_APP_SECRET: str = ""  # empty = META_APP_SECRET
    META_INSTAGRAM_VERIFY_TOKEN: str = ""
    META_INSTAGRAM_GRAPH_URL: str = "https://graph.instagram.com"
    META_INSTAGRAM_OAUTH_URL: str = "https://api.instagram.com"
    # Public base URL of this API (OAuth redirect URIs, signed media URLs).
    PUBLIC_API_URL: str = "http://localhost:8000"

    # File storage (docs/backend/STORAGE_ARCHITECTURE.md). Only "local" today.
    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_PATH: str = "./var/storage"
    MAX_UPLOAD_MB: int = 40

    # SMTP (leave SMTP_HOST empty in local dev: emails are logged instead)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@shreechat.local"
    SMTP_STARTTLS: bool = True

    # Gunicorn settings
    GUNICORN_BIND: str = "127.0.0.1:8000"
    GUNICORN_WORKERS: int = 1
    GUNICORN_THREADS: int = 8
    GUNICORN_ACCESS_LOG: str = "-"
    GUNICORN_ERROR_LOG: str = "-"

    @field_validator("DEBUG", mode="before")
    @classmethod
    def validate_debug(cls, v: str) -> int:
        if int(v) in [0, 1]:
            return int(v)
        raise ValueError("DEBUG must be 0 or 1")

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if v and len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    @property
    def is_local(self) -> bool:
        return self.ENV in ("local", "test")

    @field_validator("GUNICORN_WORKERS", mode="before")
    @classmethod
    def validate_gunicorn_workers(cls, v: str) -> int:
        if int(v) > 0:
            return int(v)
        raise ValueError("GUNICORN_WORKERS must be a positive integer")

    @field_validator("GUNICORN_THREADS", mode="before")
    @classmethod
    def validate_gunicorn_threads(cls, v: str) -> int:
        if int(v) > 0:
            return int(v)
        raise ValueError("GUNICORN_THREADS must be a positive integer")


settings = Settings()
