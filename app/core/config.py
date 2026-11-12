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

    # Frontend URL used to build links in emails (password reset, invitations)
    FRONTEND_URL: str = "http://localhost:3000"

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
