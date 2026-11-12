help:
	@echo
	@echo "install                            -- install backend dependencies"
	@echo "lint                               -- lint backend"
	@echo "format                             -- format backend"
	@echo "mypy                               -- type check backend"
	@echo "embeddings                         -- start embedding server"
	@echo


.PHONY: install
install:
	uv sync --frozen

.PHONY: lint
lint:
	uv run ruff check .

.PHONY: mypy
mypy:
	uv run mypy .

.PHONY: format
format:
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: embeddings
embeddings:
	uv run uvicorn main:app --reload --host "127.0.0.1" --port 8001 --workers 1 --log-level info
