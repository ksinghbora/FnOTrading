.PHONY: setup run test lint migrate docker-up docker-down instruments token

# Setup
setup:
	uv venv
	uv sync --all-extras
	cp -n .env.example .env || true

# Run application
run:
	uv run python -m src.main

# Docker infrastructure
docker-up:
	docker compose up -d

docker-down:
	docker compose down

# Database migrations
migrate:
	uv run alembic upgrade head

migrate-new:
	uv run alembic revision --autogenerate -m "$(msg)"

# Download instruments
instruments:
	uv run python scripts/download_instruments.py

# Generate Kite token
token:
	uv run python scripts/generate_token.py

# Testing
test:
	uv run pytest tests/ -v

test-cov:
	uv run pytest tests/ -v --cov=src --cov-report=html

# Linting
lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

# Type checking
typecheck:
	uv run mypy src/
