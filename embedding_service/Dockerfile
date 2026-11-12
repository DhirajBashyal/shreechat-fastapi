# syntax=docker/dockerfile:1

# ---------- Build stage: install dependencies with uv ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# ---------- Runtime stage ----------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# The model is downloaded on first start and cached here; mount a volume to persist it.
RUN mkdir -p /app/models && chown appuser:appuser /app/models

# Dependencies first, then code; files stay root-owned (only models/ is writable)
COPY --from=builder /app/.venv /app/.venv
COPY main.py logger.py settings.py ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/models/.hf-cache

USER appuser
EXPOSE 8001
VOLUME ["/app/models"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
