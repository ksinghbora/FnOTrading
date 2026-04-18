"""Golden-day end-to-end replay test.

This is the regression net described in :mod:`src.backtest.day_replay` and
``docs/DATA_RELIABILITY_PLAN.md`` §7.4: pin one trading day, replay it twice
through the *real* engine, and assert the result hashes are bit-equal. If a
strategy refactor silently changes behaviour, this test catches it before
merge.

Why ``2026-04-15``
------------------
Suggested in plan §12 Open Q5 as a clean day with mixed regime — and the
chain CSV is already on disk in ``data/chain_snapshots/``. The Makefile
``replay-golden`` target also defaults to the same date so manual and
automated runs agree.

This file lives under ``tests/integration/`` because it depends on real
recorded data and exercises the full ``ReplayBacktestEngine`` (~1s per run,
not negligible). It is intentionally excluded from the default
``pytest tests/ --ignore=tests/integration`` sweep — wire it in to CI as a
separate job once the determinism baseline has stabilised over a few code
landings.

Tests skip gracefully when the data isn't on disk (e.g. fresh checkout
without the recorded corpus), so the file never breaks ``pytest tests/``.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

# Force shadow-mode override OFF before any strategy imports — same reason
# scripts/replay_day.py and scripts/replay_23days.py do it. With
# PAPER_TRADING=true, portfolio_strategy turns "score below threshold" into
# "enter anyway" for data collection, which makes the replay non-deterministic
# w.r.t. the production behaviour we're trying to lock in.
os.environ.setdefault("PAPER_TRADING", "false")

from src.backtest.day_replay import DayReplayResult, replay_day  # noqa: E402

GOLDEN_DAY = date(2026, 4, 15)
REPO_ROOT = Path(__file__).resolve().parents[2]
CHAIN_DIR = REPO_ROOT / "data" / "chain_snapshots"
SPOT_CSV = REPO_ROOT / "data" / "nifty_spot_minute_chain.csv"
VIX_CSV = REPO_ROOT / "data" / "india_vix_minute_chain.csv"
CHAIN_CSV = CHAIN_DIR / f"chain_{GOLDEN_DAY.isoformat()}.csv"

# Skip-if-data-absent guard. Keep the reason specific so a fresh-checkout
# contributor can fix it in one minute (download the chain corpus).
GOLDEN_DATA_PRESENT = CHAIN_CSV.exists() and SPOT_CSV.exists()
SKIP_REASON = (
    f"golden-day data missing: need {CHAIN_CSV}, {SPOT_CSV}, {VIX_CSV}. "
    f"Run the recorder for a clean day or populate from B2 archive."
)

pytestmark = pytest.mark.skipif(not GOLDEN_DATA_PRESENT, reason=SKIP_REASON)


@pytest.fixture
def runs_log(tmp_path: Path) -> Path:
    """Fresh JSONL run-log per test. Don't pollute production
    ``data/replay_runs.jsonl`` from CI."""
    return tmp_path / "replay_runs.jsonl"


async def _replay_golden(runs_log: Path, **overrides) -> DayReplayResult:
    """Replay the golden day with sane defaults; ``overrides`` win."""
    kwargs = dict(
        target_date=GOLDEN_DAY,
        strategy_name="portfolio",
        underlying="NIFTY",
        seed=0,
        chain_dir=CHAIN_DIR,
        spot_csv=SPOT_CSV,
        vix_csv=VIX_CSV,
        runs_log=runs_log,
        skip_degraded=True,
        accept_partial=True,
    )
    kwargs.update(overrides)
    return await replay_day(**kwargs)


# ────────────────────────────────────────────────────────────────────
# Smoke — engine actually runs end-to-end on the golden day
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_golden_day_replay_completes(runs_log: Path):
    """End-to-end smoke: real engine, real data, no error returned."""
    result = await _replay_golden(runs_log)
    assert result.error is None, f"replay errored: {result.error}"
    assert result.num_days >= 1, "engine reported zero trading days"
    # Hash is non-empty SHA256 hex
    assert result.replay_hash and len(result.replay_hash) == 64
    # Audit-trail line was written
    assert runs_log.exists(), "runs log not written"
    line = runs_log.read_text().strip().split("\n")[-1]
    rec = json.loads(line)
    assert rec["replay_hash"] == result.replay_hash
    assert rec["date"] == GOLDEN_DAY.isoformat()


# ────────────────────────────────────────────────────────────────────
# Determinism — two runs, same hash
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_golden_day_is_byte_deterministic(runs_log: Path):
    """The headline contract: same (date, code_sha, params_sha, seed)
    MUST produce equal replay_hash across runs. If this ever fails on
    main, a strategy refactor introduced non-determinism."""
    r1 = await _replay_golden(runs_log)
    r2 = await _replay_golden(runs_log)

    assert r1.replay_hash == r2.replay_hash, (
        f"determinism broken: {r1.replay_hash[:16]} != {r2.replay_hash[:16]}\n"
        f"  pnl1={r1.total_pnl}  pnl2={r2.total_pnl}\n"
        f"  daily1={len(r1.daily_results)} rows  daily2={len(r2.daily_results)} rows"
    )
    assert r1.params_sha == r2.params_sha
    assert r1.total_pnl == r2.total_pnl


@pytest.mark.asyncio
async def test_golden_day_deterministic_across_seeds(runs_log: Path):
    """The engine doesn't currently consume the RNG, so different seeds
    should still produce equal hashes. If this starts failing, something
    introduced stochastic behaviour and the determinism contract needs
    a real seed-aware path (not just :func:`random.seed`)."""
    r0 = await _replay_golden(runs_log, seed=0)
    r42 = await _replay_golden(runs_log, seed=42)
    # Seed is part of the hash body, so the hashes WILL differ — but the
    # underlying P&L must not. That's the property we want to lock.
    assert r0.total_pnl == r42.total_pnl, (
        f"seed leaked into P&L: seed=0 → {r0.total_pnl}, seed=42 → {r42.total_pnl}"
    )


# ────────────────────────────────────────────────────────────────────
# Sensitivity — params change → hash change
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_golden_day_hash_changes_with_params(runs_log: Path):
    """Sensitivity: params override → different params_sha → different
    replay_hash. Catches the failure mode where the hash is computed
    over a stale snapshot of params."""
    base = await _replay_golden(runs_log)
    perturbed = await _replay_golden(
        runs_log,
        extra_params={"premium_score_threshold": 999},  # impossibly strict
    )
    assert base.params_sha != perturbed.params_sha
    assert base.replay_hash != perturbed.replay_hash


# ────────────────────────────────────────────────────────────────────
# Audit trail — multiple runs each get a line
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_golden_day_runs_log_grows_per_call(runs_log: Path):
    """Two replays → two well-formed JSONL records. Locks the contract
    that nothing batches/collapses runs into a single line."""
    await _replay_golden(runs_log)
    await _replay_golden(runs_log)

    lines = runs_log.read_text().strip().split("\n")
    assert len(lines) == 2

    recs = [json.loads(line) for line in lines]
    for rec in recs:
        assert rec["date"] == GOLDEN_DAY.isoformat()
        assert rec["strategy"] == "portfolio"
        assert rec["replay_hash"]
        assert rec["params_sha"]
        assert rec["error"] is None

    # Same hash both times (determinism repeats the §7.4 contract from
    # the lens of the audit trail).
    assert recs[0]["replay_hash"] == recs[1]["replay_hash"]
