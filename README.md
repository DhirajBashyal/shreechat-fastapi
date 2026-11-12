# shreechat-ai-fastapi

FastAPI backend for Shree Chat AI

## Project structure

```
app/
├── main.py                 # FastAPI app, middleware, router registration
├── api/
│   └── v1/
│       ├── router.py       # aggregates all v1 routers (mounted at /api/v1)
│       └── endpoints/      # one module per resource (health.py, ...)
├── core/                   # config, db, logger, rate limiter, constants
├── models/                 # SQLAlchemy models (import new ones in __init__.py)
├── schemas/                # Pydantic request/response schemas
└── services/               # business logic / external integrations
migrations/                 # Alembic
tests/
embedding_service/          # separate embedding microservice
```

To add a v1 endpoint, create `app/api/v1/endpoints/<name>.py` with an `APIRouter`
and register it in `app/api/v1/router.py`. For a future `v2`, add `app/api/v2/`
with its own `router.py` and mount it in `app/main.py` under `/api/v2`.

## Local development setup

1. Install build tools
```
sudo apt install build-essential
```

For windows, install [choco](https://chocolatey.org/install) and in the administrative
shell run following command to install `make`
```
choco install make
```

2. Install uv
```
curl -LsSf https://astral.sh/uv/0.7.0/install.sh | sh
```

For windows, run the following command in administrative shell
```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.7.0/install.ps1 | iex"
```


3. Install packages
```
make install
```

4. Start development server
```
make dev

# After database is created by docker compose

make migrate
```
Development server should be running at `http://localhost:8000/docs`

5. Start embedding server
```
make embeddings
```
Embedding server should be running at `http://localhost:8001/docs`


6. Make sure to run following commands before pushing
```
make format
make lint
make mypy
```

To add any new dependency in the project
```
uv add <package-name>
```

## Running with Docker

Build and start the API, embedding service, Postgres and Redis:
```
make docker-up
```
- API: `http://localhost:8000/docs` (migrations run automatically on start)
- Embeddings: `http://localhost:8001/docs` (model downloads on first start, cached in the `embedding-models` volume)

Stop with `make docker-down`. Values in `.env` are passed to the API container;
`DB_URL`, `REDIS_URL` and `EMBEDDING_SERVICE_URL` are overridden to use the compose service names.

## Creating migration file

Run following command to generate migration file
```
uv run alembic revision --autogenerate -m <meaningful-migration-name>
```

Applying the migration
```
uv run alembic upgrade head
```