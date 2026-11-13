help:
	@echo
	@echo "install                            -- install backend dependencies"
	@echo "lint                               -- lint backend"
	@echo "format                             -- format backend"
	@echo "mypy                               -- type check backend"
	@echo "test                               -- test backend"
	@echo "embeddings                         -- start embedding server"
	@echo "dev                                -- start backend development server"
	@echo "migrate                            -- run database migrations"
	@echo "seed                               -- seed demo tenant (dev only; run migrate first)"
	@echo "platform-admin ARGS=...            -- manage platform admins: grant <email> [--create] | revoke <email> | list"
	@echo "ai-reply MESSAGE=<uuid> [QUEUE=1]  -- run the AI agent for one incoming message (or enqueue it)"
	@echo "outbox-relay                       -- publish committed events (realtime, website widget replies)"
	@echo "ai-worker                          -- AI queue worker (debounce, one run per conversation, per-org limit)"
	@echo "ai-eval WS=<uuid> FILE=<json> [AGENT=<uuid>] -- evaluate retrieval (and answers) on a test set"
	@echo "purge-organizations                -- close organizations past their 30-day deletion grace period"
	@echo "docker-up                          -- build and run api + embeddings in docker"
	@echo "docker-down                        -- stop docker app containers"
	@echo "clean                              -- remove backend containers and volumes"
	@echo "clean-test                         -- remove test containers and volumes"
	@echo


.PHONY: install
install:
	uv sync --frozen

.PHONY: lint
lint:
	uv run ruff check .

.PHONY: mypy
mypy:
	MYPY_PATH=./,embedding_service uv run mypy .

.PHONY: format
format:
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: test
test:
	docker compose -f docker-compose.test.yml up -d
	@echo "⏳ Waiting 5 seconds for docker services to be healthy..."
	@sleep 5
	ENV_FILE=.env.test uv run pytest

.PHONY: embeddings
embeddings:
	cd embedding_service && uv run uvicorn main:app --reload --host "127.0.0.1" --port 8001 --workers 1 --log-level info

.PHONY: dev
dev:
	docker compose up -d
	@echo "⏳ Waiting 5 seconds for docker services to be healthy..."
	@sleep 5
	uv run uvicorn app.main:app --reload --host "127.0.0.1" --port 8000 --workers 1 --log-level info

.PHONY: migrate
migrate:
	uv run alembic upgrade head


.PHONY: platform-admin
platform-admin:
	uv run python -m app.jobs.platform_admin $(ARGS)

.PHONY: ai-reply
ai-reply:
	uv run python -m app.jobs.ai_reply $(MESSAGE) $(if $(QUEUE),--queue,)

.PHONY: ai-worker
ai-worker:
	uv run python -m app.jobs.ai_worker

.PHONY: outbox-relay
outbox-relay:
	uv run python -m app.jobs.outbox_relay

.PHONY: ai-eval
ai-eval:
	uv run python -m app.jobs.ai_eval $(WS) $(FILE) $(AGENT)

.PHONY: purge-organizations
purge-organizations:
	uv run python -m app.jobs.purge_organizations

.PHONY: docker-up
docker-up:
	docker compose --profile app up -d --build

.PHONY: docker-down
docker-down:
	docker compose --profile app down

.PHONY: seed
seed:
	uv run python seeder.py

.PHONY: clean
clean:
	docker compose --profile app down -v

.PHONY: clean-test
clean-test:
	docker compose -f docker-compose.test.yml down -v
