.PHONY: setup run recorder trader test lint migrate docker-up docker-down instruments token \
        replay-day replay-golden replay-ci snapshot-config \
        archive upload restore-test cold-storage

# Setup
setup:
	uv venv
	uv sync --all-extras
	cp -n .env.example .env || true

# Run application (legacy single-process mode — recorder + trader together)
run:
	uv run python -m src.main

# Run the recorder process (data capture only). In split mode this owns
# the Kite WebSocket and publishes ticks to Redis for the trader.
# Set recorder_split_mode=true in Settings before using this.
recorder:
	uv run python -m src.recorder_main

# Run the trader process (strategies + OMS + risk + API). Subscribes to
# ticks via Redis from the recorder process. Same module as `make run`,
# but the split-mode flag changes its wiring at startup.
trader:
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

# ─────────────────────────────────────────────────────────────────────
# Replay harness (DATA_RELIABILITY_PLAN §7)
# ─────────────────────────────────────────────────────────────────────

# Replay one trading day with frozen-day-of params.
#   DATE     — required; YYYY-MM-DD
#   STRATEGY — defaults to portfolio (the only strategy profitable on real data)
#   SEED     — RNG seed for determinism (default 0)
# Example: make replay-day DATE=2026-04-15
#          make replay-day DATE=2026-04-15 STRATEGY=iron_condor SEED=42
replay-day:
	@if [ -z "$(DATE)" ]; then \
		echo "Usage: make replay-day DATE=YYYY-MM-DD [STRATEGY=portfolio] [SEED=0]"; \
		exit 2; \
	fi
	uv run python scripts/replay_day.py \
		--date $(DATE) \
		--strategy $(or $(STRATEGY),portfolio) \
		--seed $(or $(SEED),0)

# Replay the golden day — locked-in regression check for strategy refactors.
# Pinned to 2026-04-15 (suggested in DATA_RELIABILITY_PLAN §12 Open Q5);
# override with `make replay-golden GOLDEN_DAY=YYYY-MM-DD` if the golden
# corpus moves.
GOLDEN_DAY ?= 2026-04-15
replay-golden:
	@echo "[replay-golden] Replaying golden day $(GOLDEN_DAY) — hash should match the recorded baseline"
	uv run python scripts/replay_day.py --date $(GOLDEN_DAY) --strategy portfolio --seed 0

# Replay-as-CI hook (DATA_RELIABILITY_PLAN §10 P2 + §7.6).
# Runs the determinism integration test against the pinned golden day.
# Wire this into any post-merge CI step (GitHub Actions, etc.) — failure
# means a strategy refactor introduced non-determinism or shifted P&L on
# the golden corpus, which should block the merge.
#
# Skips gracefully when the chain corpus isn't on disk, so a fresh checkout
# without recorded data exits 0 (the test file's pytestmark.skipif handles it).
replay-ci:
	@echo "[replay-ci] Running determinism + sensitivity contracts on golden day"
	uv run pytest tests/integration/test_replay_determinism.py -v --tb=short

# Daily config snapshotter — usually run from cron at 09:00 IST, but expose
# a make target for manual snapshot before a backtest run.
snapshot-config:
	uv run python scripts/snapshot_config.py

# ─────────────────────────────────────────────────────────────────────
# Cold storage (DATA_RELIABILITY_PLAN §9)
# Three crons in sequence, every weekday + weekly restore-test:
#   22:00 IST (mon–fri)  archive_to_parquet  → local Parquet+ZSTD
#   23:00 IST (mon–fri)  upload_to_b2        → push to B2
#   09:00 IST (sun)      restore_test        → prove round-trip works
# Local-dir mode for dev/CI:  ARCHIVE_REMOTE=local-dir:./fake-b2
# ─────────────────────────────────────────────────────────────────────

# Archive eligible chain CSVs to Parquet+ZSTD (default: files ≥7 days old).
# Override with --date for a specific day:  make archive ARGS="--date 2026-04-15"
archive:
	uv run python scripts/archive_to_parquet.py $(ARGS)

# Upload archived Parquets to remote cold storage. Requires either
# B2 creds in env (B2_KEY_ID, B2_APP_KEY, B2_BUCKET) OR
# ARCHIVE_REMOTE=local-dir:<path>.
upload:
	uv run python scripts/upload_to_b2.py $(ARGS)

# Weekly disaster-recovery proof. Downloads the most recently uploaded
# archive, verifies SHA1 + Parquet decode + row-count match against original
# CSV. Exits 2 on any failure so cron monitoring fires.
restore-test:
	uv run python scripts/restore_test.py $(ARGS)

# Convenience: run the full cold-storage chain end-to-end (dev / smoke).
# Uses local-dir backend so no B2 creds needed.
cold-storage:
	@echo "[cold-storage] full chain: archive → upload → restore-test"
	$(MAKE) archive
	ARCHIVE_REMOTE=local-dir:./fake-b2 $(MAKE) upload
	ARCHIVE_REMOTE=local-dir:./fake-b2 $(MAKE) restore-test
