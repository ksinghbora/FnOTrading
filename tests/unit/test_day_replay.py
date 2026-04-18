"""Tests for src/backtest/day_replay.py — the deterministic single-day harness.

Contracts this file locks in:
  1. Frozen-config loader prefers <snapshot>/params.json when present.
  2. Loader falls back to live params.py defaults when snapshot is missing,
     malformed, or has no entry for the strategy's class.
  3. Overrides merge ON TOP of the resolved params (caller wins).
  4. Result hash is stable across runs with identical inputs (determinism).
  5. Result hash CHANGES when params change (sensitivity).
  6. Engine errors are captured into the result, not raised — every replay
     produces a hashable record, including failures.
  7. data/replay_runs.jsonl audit trail gets one well-formed line per call,
     even on engine error.
  8. snapshot_config.py emits params.json (sidecar contract for the loader).
  9. PARAMS_CLASS_BY_STRATEGY mapping covers the production strategy.
 10. ``db_session_factory`` is opt-in: default behaviour writes JSONL only.
     When provided, one ReplayRunModel row is added & committed mirroring
     the JSONL line. Errored replays still land a row (with ``error`` set
     and the engine-output columns NULL). DB write failures are caught and
     logged — JSONL stays the durable record.

These are pure unit tests — the ReplayBacktestEngine is monkey-patched so we
don't need real chain CSVs. Integration smoke (real engine, real fixture day)
belongs in tests/integration/ once we pick a golden day.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.backtest import day_replay as dr  # noqa: E402

# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

# A canned engine result that looks like what ReplayBacktestEngine.run()
# returns on a successful one-day run. Numbers are picked so a hash change
# is obvious if anything reorders.
_ENGINE_OK = {
    "strategy": "portfolio",
    "strategy_id": "portfolio_replay",
    "underlying": "NIFTY",
    "data_source": "replay",
    "snapshot_dir": "data/chain_snapshots",
    "spot_csv": "data/nifty_spot_minute_chain.csv",
    "period": "2026-04-15 to 2026-04-15",
    "num_days": 1,
    "lots": 1,
    "lot_size": 75,
    "initial_capital": 1_000_000,
    "params": {"underlying": "NIFTY", "quantity_lots": 1},
    "metrics": {
        "snapshot_coverage_pct": 100.0,
        "fills_via_bid_ask": 8,
        "fills_via_ltp_slip": 0,
        "snapshot_hits": 375,
        "bs_fallbacks": 0,
        "bid_ask_fill_pct": 100.0,
        "total_charges": 142.5,
        "num_days": 1,
        "sharpe_ratio": 1.23,
    },
    "daily_results": [
        {
            "date": "2026-04-15", "day_of_week": "Wednesday",
            "spot_open": 22500.0, "spot_close": 22580.0,
            "pnl": 1234.56, "charges": 142.5,
            "trades": 4, "equity": 1_001_234.56,
            "data_source": "snapshot",
        },
    ],
    "equity_curve": [
        {"date": "2026-04-15", "equity": 1_001_234.56, "pnl": 1234.56},
    ],
    "final_pnl": 1234.5678,
    "chain_quality": {"2026-04-15": "clean"},
    "skipped_days": [],
}


def _patch_engine(monkeypatch, engine_result):
    """Replace ReplayBacktestEngine.run with an AsyncMock returning the dict."""
    mock = AsyncMock(return_value=engine_result)
    monkeypatch.setattr(
        "src.backtest.day_replay.ReplayBacktestEngine.run", mock,
    )
    return mock


def _write_params_snapshot(snapshot_root: Path, target: date, payload: dict) -> Path:
    """Mimic snapshot_config.py's params.json output."""
    out_dir = snapshot_root / target.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "params.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


# ────────────────────────────────────────────────────────────────────
# Loader contract — _resolve_params
# ────────────────────────────────────────────────────────────────────

def test_resolve_params_prefers_snapshot(tmp_path: Path):
    target = date(2026, 4, 15)
    _write_params_snapshot(
        tmp_path, target,
        {"PortfolioParams": {"premium_score_threshold": 99, "underlying": "NIFTY"}},
    )

    params, source, snap_dir = dr._resolve_params(
        target, "portfolio", tmp_path, overrides=None,
    )

    assert params["premium_score_threshold"] == 99
    assert source.startswith("snapshot:")
    assert snap_dir == str(tmp_path / "2026-04-15")


def test_resolve_params_overrides_win_over_snapshot(tmp_path: Path):
    target = date(2026, 4, 15)
    _write_params_snapshot(
        tmp_path, target,
        {"PortfolioParams": {"premium_score_threshold": 99}},
    )
    params, _, _ = dr._resolve_params(
        target, "portfolio", tmp_path,
        overrides={"premium_score_threshold": 50},
    )
    assert params["premium_score_threshold"] == 50


def test_resolve_params_falls_back_to_live_when_snapshot_missing(tmp_path: Path):
    params, source, snap_dir = dr._resolve_params(
        date(2026, 4, 15), "portfolio", tmp_path, overrides=None,
    )
    # Live defaults always include `underlying`
    assert "underlying" in params
    assert source == "live"
    assert snap_dir is None


def test_resolve_params_falls_back_when_snapshot_malformed(tmp_path: Path, caplog):
    target = date(2026, 4, 15)
    out_dir = tmp_path / target.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "params.json").write_text("{not valid json")

    params, source, _ = dr._resolve_params(target, "portfolio", tmp_path, None)
    assert source == "live"
    assert "underlying" in params


def test_resolve_params_falls_back_when_class_key_missing(tmp_path: Path):
    target = date(2026, 4, 15)
    _write_params_snapshot(
        tmp_path, target,
        {"SomeOtherParams": {"foo": 1}},  # no PortfolioParams entry
    )
    params, source, _ = dr._resolve_params(target, "portfolio", tmp_path, None)
    assert source == "live"
    assert "underlying" in params


def test_resolve_params_skips_class_with_snapshot_error(tmp_path: Path):
    """snapshot_config writes ``{_snapshot_error: ...}`` when a class can't
    be instantiated (e.g. requires fields with no defaults). Loader must
    refuse to use that and fall back to live."""
    target = date(2026, 4, 15)
    _write_params_snapshot(
        tmp_path, target,
        {"PortfolioParams": {"_snapshot_error": "ValueError: missing field x"}},
    )
    params, source, _ = dr._resolve_params(target, "portfolio", tmp_path, None)
    assert source == "live"


def test_resolve_params_unknown_strategy_marked_no_mapping(tmp_path: Path):
    """A strategy NOT in _PARAMS_CLASS_BY_STRATEGY but registered live should
    still produce a params dict — but the source is tagged so audits know."""
    # Use an existing registered strategy that we deliberately don't put in
    # the mapping. Simulate by monkey-patching the mapping.
    saved = dict(dr._PARAMS_CLASS_BY_STRATEGY)
    try:
        dr._PARAMS_CLASS_BY_STRATEGY.pop("iron_condor", None)
        params, source, _ = dr._resolve_params(
            date(2026, 4, 15), "iron_condor", tmp_path, None,
        )
        assert source == "live (no mapping)"
        assert "underlying" in params
    finally:
        dr._PARAMS_CLASS_BY_STRATEGY.clear()
        dr._PARAMS_CLASS_BY_STRATEGY.update(saved)


def test_live_params_defaults_unknown_strategy_raises():
    with pytest.raises(KeyError, match="not in params registry"):
        dr._live_params_defaults("does_not_exist_strategy")


# ────────────────────────────────────────────────────────────────────
# Determinism — hash stability + sensitivity
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_hash_is_stable_across_runs(tmp_path: Path, monkeypatch):
    _patch_engine(monkeypatch, _ENGINE_OK)
    runs_log = tmp_path / "runs.jsonl"

    r1 = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=runs_log, seed=0,
    )
    r2 = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=runs_log, seed=0,
    )

    assert r1.replay_hash == r2.replay_hash
    assert r1.replay_hash  # not empty
    # run_at is excluded from hash → guaranteed to differ between runs
    # within the same second only by chance; we don't assert that.


@pytest.mark.asyncio
async def test_replay_hash_changes_when_params_change(tmp_path: Path, monkeypatch):
    """Hash sensitivity: different params_sha must produce different hash."""
    _patch_engine(monkeypatch, _ENGINE_OK)
    snap_root = tmp_path / "snapshots"

    r_default = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=snap_root, chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=None, seed=0,
    )

    # Same engine result, but different params via override.
    r_overridden = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=snap_root, chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=None, seed=0,
        extra_params={"premium_score_threshold": 999},
    )

    assert r_default.params_sha != r_overridden.params_sha
    assert r_default.replay_hash != r_overridden.replay_hash


@pytest.mark.asyncio
async def test_replay_hash_changes_when_engine_pnl_changes(tmp_path: Path, monkeypatch):
    """If strategy code mutates next month and produces a different P&L on
    the same day, the hash MUST change. This is the regression net."""
    snap_root = tmp_path / "snapshots"

    _patch_engine(monkeypatch, _ENGINE_OK)
    r1 = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=snap_root, chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=None, seed=0,
    )

    perturbed = dict(_ENGINE_OK)
    perturbed["final_pnl"] = 9999.99  # different P&L
    _patch_engine(monkeypatch, perturbed)
    r2 = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=snap_root, chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=tmp_path / "vix.csv",
        runs_log=None, seed=0,
    )
    assert r1.replay_hash != r2.replay_hash


# ────────────────────────────────────────────────────────────────────
# Error handling — engine failures still produce a hashed record
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_error_returns_result_with_error_field(tmp_path: Path, monkeypatch):
    _patch_engine(monkeypatch, {"error": "No spot data loaded"})

    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None,
    )

    assert result.error == "No spot data loaded"
    assert result.total_pnl == 0.0
    assert result.num_days == 0
    assert result.replay_hash  # still hashed


@pytest.mark.asyncio
async def test_engine_exception_is_swallowed_into_error(tmp_path: Path, monkeypatch):
    """If the engine raises, the harness must convert it into an error result.
    Otherwise the audit trail loses the failure entirely."""
    mock = AsyncMock(side_effect=RuntimeError("disk full"))
    monkeypatch.setattr("src.backtest.day_replay.ReplayBacktestEngine.run", mock)

    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None,
    )
    assert result.error and "disk full" in result.error
    assert result.replay_hash


# ────────────────────────────────────────────────────────────────────
# Audit trail — JSONL run log
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runs_log_appends_one_line_per_call(tmp_path: Path, monkeypatch):
    _patch_engine(monkeypatch, _ENGINE_OK)
    runs_log = tmp_path / "runs.jsonl"

    for _ in range(3):
        await dr.replay_day(
            target_date=date(2026, 4, 15), strategy_name="portfolio",
            snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
            spot_csv=tmp_path / "spot.csv", vix_csv=None,
            runs_log=runs_log,
        )

    lines = runs_log.read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        rec = json.loads(line)
        assert rec["date"] == "2026-04-15"
        assert rec["strategy"] == "portfolio"
        assert rec["replay_hash"]
        assert rec["params_sha"]
        assert "run_at" in rec


@pytest.mark.asyncio
async def test_runs_log_records_failed_runs_too(tmp_path: Path, monkeypatch):
    """Failed runs must still produce a log line. Otherwise nightly audits
    can't detect 'replay tried but engine threw' silently."""
    mock = AsyncMock(side_effect=RuntimeError("kaboom"))
    monkeypatch.setattr("src.backtest.day_replay.ReplayBacktestEngine.run", mock)
    runs_log = tmp_path / "runs.jsonl"

    await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=runs_log,
    )

    line = runs_log.read_text().strip()
    rec = json.loads(line)
    assert rec["error"] and "kaboom" in rec["error"]


@pytest.mark.asyncio
async def test_no_runs_log_when_disabled(tmp_path: Path, monkeypatch):
    _patch_engine(monkeypatch, _ENGINE_OK)
    await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None,
    )
    # Confirm we didn't accidentally write to the default path either
    assert not (tmp_path / "runs.jsonl").exists()


# ────────────────────────────────────────────────────────────────────
# Result shape — sanity
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_shape_on_success(tmp_path: Path, monkeypatch):
    _patch_engine(monkeypatch, _ENGINE_OK)
    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        underlying="NIFTY",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None, seed=42,
    )

    assert result.date == "2026-04-15"
    assert result.strategy == "portfolio"
    assert result.underlying == "NIFTY"
    assert result.seed == 42
    assert result.total_pnl == pytest.approx(1234.5678, rel=1e-6)
    assert result.snapshot_coverage_pct == 100.0
    assert result.fills_via_bid_ask == 8
    assert result.fills_via_ltp_slip == 0
    assert result.daily_results == [
        {
            "date": "2026-04-15", "day_of_week": "Wednesday",
            "spot_open": 22500.0, "spot_close": 22580.0,
            "pnl": 1234.56, "charges": 142.5,
            "trades": 4, "equity": 1_001_234.56,
            "data_source": "snapshot",
        },
    ]
    assert result.chain_quality == {"2026-04-15": "clean"}
    assert result.error is None
    assert result.replay_hash and len(result.replay_hash) == 64


# ────────────────────────────────────────────────────────────────────
# snapshot_config.py contract — params.json sidecar is emitted
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_config_emits_params_json(tmp_path: Path, monkeypatch):
    """day_replay's loader contract requires snapshot_config to emit JSON.
    Lock that contract here so a future yaml-only revert breaks fast."""
    import snapshot_config as sc

    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)
    params_json = out / "params.json"
    assert params_json.exists(), "snapshot_config must emit params.json sidecar"
    blob = json.loads(params_json.read_text())
    # Must contain the key day_replay looks up by name
    assert "PortfolioParams" in blob
    # Manifest must include the file's SHA
    manifest = json.loads((out / "manifest.json").read_text())
    assert "params.json" in manifest["files"]


@pytest.mark.asyncio
async def test_loader_consumes_real_snapshot_config_output(tmp_path: Path, monkeypatch):
    """End-to-end: snapshot_config writes params.json, day_replay reads it
    and produces a snapshot-sourced result."""
    import snapshot_config as sc

    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    target = date(2026, 4, 17)
    await sc.snapshot(target, with_instruments=False)

    _patch_engine(monkeypatch, _ENGINE_OK)
    result = await dr.replay_day(
        target_date=target, strategy_name="portfolio",
        snapshot_root=tmp_path, chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None,
    )
    assert result.params_source.startswith("snapshot:")
    assert result.snapshot_dir == str(tmp_path / target.isoformat())


# ────────────────────────────────────────────────────────────────────
# Mapping — production strategy is covered
# ────────────────────────────────────────────────────────────────────

def test_portfolio_strategy_is_in_mapping():
    """Portfolio is the only profitable strategy on real data per MEMORY.md.
    If someone removes it from the mapping, this test fires immediately."""
    assert "portfolio" in dr._PARAMS_CLASS_BY_STRATEGY
    assert dr._PARAMS_CLASS_BY_STRATEGY["portfolio"] == "PortfolioParams"


# ────────────────────────────────────────────────────────────────────
# Hashing helpers — small unit checks
# ────────────────────────────────────────────────────────────────────

def test_canonical_json_is_sorted():
    blob = dr._canonical_json({"b": 2, "a": 1})
    assert blob == b'{"a":1,"b":2}'


def test_round_floats_preserves_structure():
    rounded = dr._round_floats(
        {"pnl": 1.234567899, "rows": [{"v": 0.111111111}]},
    )
    assert rounded == {"pnl": 1.2346, "rows": [{"v": 0.1111}]}


def test_round_floats_leaves_non_floats_alone():
    obj = {"a": 1, "b": "hello", "c": True, "d": None, "e": [1, 2]}
    assert dr._round_floats(obj) == obj


def test_pin_seeds_is_callable_with_zero():
    """Smoke: pin_seeds(0) doesn't blow up even when numpy isn't pinned."""
    dr._pin_seeds(0)
    dr._pin_seeds(42)


# ────────────────────────────────────────────────────────────────────
# DB mirror — replay_runs row writer
#
# These tests pin the contract from §10 of the docstring above:
# the JSONL is the durable record; the DB row is a best-effort mirror.
# We use a fake async session factory rather than a real engine so the
# tests stay pure-unit (no DB, no event loop tricks).
# ────────────────────────────────────────────────────────────────────


class _FakeSession:
    """Stands in for an AsyncSession.

    ``add()`` is sync (matches SQLAlchemy), ``commit()`` is async. We
    record the rows passed to ``add`` so tests can introspect what would
    have been written.
    """

    def __init__(self):
        self.added: list = []
        self.committed: bool = False

    def add(self, row) -> None:  # SQLAlchemy AsyncSession.add is sync
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    """Mimics ``async_sessionmaker``: ``factory()`` returns an async ctx mgr.

    The factory itself is callable; the call returns ``self`` so we can
    drive ``__aenter__`` / ``__aexit__`` directly. One factory may be
    used for multiple calls — each ``__aenter__`` returns a fresh session
    so tests can assert per-call behaviour without crosstalk.
    """

    def __init__(self, *, raise_on_commit: Exception | None = None):
        self.sessions: list[_FakeSession] = []
        self.raise_on_commit = raise_on_commit
        self.calls: int = 0

    def __call__(self):
        self.calls += 1
        return self

    async def __aenter__(self) -> _FakeSession:
        sess = _FakeSession()
        if self.raise_on_commit is not None:
            async def boom() -> None:
                raise self.raise_on_commit
            sess.commit = boom  # type: ignore[assignment]
        self.sessions.append(sess)
        return sess

    async def __aexit__(self, *exc_info) -> None:
        return None


@pytest.mark.asyncio
async def test_db_writer_default_none_does_not_touch_db(
    tmp_path: Path, monkeypatch
):
    """Backwards compatibility: omitting ``db_session_factory`` must keep
    behaviour identical to before the slice landed. The JSONL is still
    written, the result is still returned, and ``_persist_to_db`` is
    NEVER called."""
    _patch_engine(monkeypatch, _ENGINE_OK)
    spy = AsyncMock()
    monkeypatch.setattr("src.backtest.day_replay._persist_to_db", spy)

    runs_log = tmp_path / "runs.jsonl"
    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=runs_log,
        # db_session_factory omitted → defaults to None
    )

    assert result.replay_hash
    assert runs_log.exists() and runs_log.read_text().strip()
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_db_writer_inserts_one_row_on_success(
    tmp_path: Path, monkeypatch
):
    """When a session factory is provided, one row is added and committed,
    populated from the DayReplayResult. This is the happy path."""
    _patch_engine(monkeypatch, _ENGINE_OK)
    factory = _FakeSessionFactory()

    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        underlying="NIFTY",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None, seed=7,
        db_session_factory=factory,
    )

    assert factory.calls == 1
    assert len(factory.sessions) == 1
    sess = factory.sessions[0]
    assert sess.committed
    assert len(sess.added) == 1

    row = sess.added[0]
    # Mirror of the in-memory result. Catches accidental field renames.
    assert row.target_date == date(2026, 4, 15)
    assert row.strategy == "portfolio"
    assert row.underlying == "NIFTY"
    assert row.seed == 7
    assert row.replay_hash == result.replay_hash
    assert row.params_sha == result.params_sha
    assert row.params_source == result.params_source
    assert row.code_sha == result.code_sha
    assert row.total_pnl == pytest.approx(1234.5678, rel=1e-6)
    assert row.num_days == 1
    assert row.snapshot_coverage_pct == 100.0
    assert row.fills_via_bid_ask == 8
    assert row.fills_via_ltp_slip == 0
    assert row.error is None


@pytest.mark.asyncio
async def test_db_writer_nullifies_engine_columns_on_error(
    tmp_path: Path, monkeypatch
):
    """An errored replay still lands a row — but with the engine-output
    columns NULL so analysts can ``WHERE error IS NULL`` to filter clean
    runs. This is the contract the migration's nullable=True relies on."""
    _patch_engine(monkeypatch, {"error": "No spot data loaded"})
    factory = _FakeSessionFactory()

    await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=None,
        db_session_factory=factory,
    )

    assert len(factory.sessions) == 1
    row = factory.sessions[0].added[0]
    assert row.error == "No spot data loaded"
    # Engine outputs nulled out — the in-memory result has 0/0.0 sentinels
    # but the DB row should be NULL so reporting queries can distinguish
    # "engine ran and produced 0 P&L" from "engine errored".
    assert row.total_pnl is None
    assert row.num_days is None
    assert row.snapshot_coverage_pct is None
    assert row.fills_via_bid_ask is None
    assert row.fills_via_ltp_slip is None
    # But determinism + audit fields ARE populated even on error — the
    # analyst still wants to know which code+params produced the failure.
    assert row.code_sha
    assert row.params_sha
    assert row.replay_hash
    assert row.params_source


@pytest.mark.asyncio
async def test_db_writer_failure_is_caught_and_logged(
    tmp_path: Path, monkeypatch, caplog
):
    """If the DB write raises (connection drop, schema mismatch, etc.) the
    replay must NOT crash. JSONL is durable; DB is mirror. Caller sees a
    normal DayReplayResult, the warning is logged, and the JSONL line is
    still written."""
    _patch_engine(monkeypatch, _ENGINE_OK)
    factory = _FakeSessionFactory(
        raise_on_commit=RuntimeError("connection refused"),
    )
    runs_log = tmp_path / "runs.jsonl"

    with caplog.at_level("WARNING"):
        result = await dr.replay_day(
            target_date=date(2026, 4, 15), strategy_name="portfolio",
            snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
            spot_csv=tmp_path / "spot.csv", vix_csv=None,
            runs_log=runs_log,
            db_session_factory=factory,
        )

    # Replay still succeeded
    assert result.error is None
    assert result.replay_hash

    # JSONL still written — that's the whole point of "best-effort DB"
    assert runs_log.exists()
    line = runs_log.read_text().strip()
    rec = json.loads(line)
    assert rec["replay_hash"] == result.replay_hash

    # Warning emitted with the underlying error visible
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("DB insert failed" in r.message for r in warnings), (
        f"Expected 'DB insert failed' in warnings, got: "
        f"{[r.message for r in warnings]}"
    )


@pytest.mark.asyncio
async def test_db_writer_failure_does_not_block_jsonl(
    tmp_path: Path, monkeypatch
):
    """Order matters: JSONL append happens BEFORE the DB write so a DB
    failure can't corrupt the durable trail. This test pins that order
    explicitly — if someone reorders the writes (or wraps both in a
    single try) this fires."""
    # Engine raises in __aenter__ (connection refused before commit)
    class _BrokenFactory:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return self

        async def __aenter__(self):
            raise RuntimeError("pool exhausted")

        async def __aexit__(self, *a):
            return None

    _patch_engine(monkeypatch, _ENGINE_OK)
    runs_log = tmp_path / "runs.jsonl"

    result = await dr.replay_day(
        target_date=date(2026, 4, 15), strategy_name="portfolio",
        snapshot_root=tmp_path / "snapshots", chain_dir=tmp_path,
        spot_csv=tmp_path / "spot.csv", vix_csv=None,
        runs_log=runs_log,
        db_session_factory=_BrokenFactory(),
    )

    assert result.error is None  # replay itself OK
    assert runs_log.exists()
    rec = json.loads(runs_log.read_text().strip())
    assert rec["replay_hash"] == result.replay_hash


@pytest.mark.asyncio
async def test_persist_to_db_round_trips_run_at_string(monkeypatch):
    """Direct unit on _persist_to_db: the helper parses the ISO 'Z' timestamp
    into a real datetime for the DB column. This is the boundary where
    JSONL's human-friendly string form meets SQLAlchemy's typed columns."""
    from datetime import datetime, timezone

    factory = _FakeSessionFactory()
    result = dr.DayReplayResult(
        date="2026-04-15",
        strategy="portfolio",
        underlying="NIFTY",
        seed=0,
        code_sha="abc123",
        params_sha="def456",
        params_source="snapshot:2026-04-15",
        snapshot_dir="/tmp/snap",
        total_pnl=1234.5,
        num_days=1,
        snapshot_coverage_pct=100.0,
        fills_via_bid_ask=4,
        fills_via_ltp_slip=0,
    )
    result.replay_hash = "0" * 64
    result.run_at = "2026-04-18T15:30:00Z"

    await dr._persist_to_db(factory, result)

    assert len(factory.sessions) == 1
    row = factory.sessions[0].added[0]
    assert row.run_at == datetime(2026, 4, 18, 15, 30, 0, tzinfo=timezone.utc)
    assert row.target_date == date(2026, 4, 15)
    assert row.snapshot_dir == "/tmp/snap"
