# syntax=docker/dockerfile:1

# ---------- Stage 1: build the virtualenv (uv is only present here) ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Rebuilt only when pyproject.toml / uv.lock change
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# ---------- Stage 2: runtime (final image) ----------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENV=production \
    GUNICORN_BIND=0.0.0.0:8000

# Dependencies first, then code: code changes only rebuild the last layers.
# Files stay root-owned so the app user can't modify them.
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini gunicorn.conf.py ./
COPY migrations ./migrations
COPY app ./app

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/')" || exit 1

# Apply migrations, then start gunicorn (reads ./gunicorn.conf.py)
CMD ["sh", "-c", "alembic upgrade head && exec gunicorn"]
