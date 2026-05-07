"""Single-day deterministic replay harness.

Wraps :class:`ReplayBacktestEngine` with three guarantees the bare engine
doesn't give. The motivation is documented at length in
``docs/DATA_RELIABILITY_PLAN.md`` §7 ("Replay strategy"); this module is the
implementation of §7.3 and §7.4.

What this adds on top of ReplayBacktestEngine
----------------------------------------------

1. **Frozen-day-of params** — loads strategy parameters from
   ``config/snapshots/<date>/params.json`` when present, falling back to the
   currently-imported defaults from :mod:`src.strategy.params` otherwise. The
   result records *which source was used* (``"snapshot"`` vs ``"live"``) and
   the path it loaded from, so downstream tooling can refuse to compare runs
   that drifted across code bumps.

2. **Deterministic seed** — pins :func:`random.seed` and
   :func:`numpy.random.seed` before the run so any future stochastic component
   (slippage jitter, tie-breaking) doesn't desync the result hash across
   runs. Today the engine is already deterministic; the seed wiring is here
   so it stays that way as features land.

3. **Result hash** — canonicalizes the result dict (sorted keys, fixed
   numeric precision, ``run_at``/``replay_hash`` fields excluded from the
   hashed body) and SHA256s the canonical bytes. Two runs with the same
   ``(date, code_sha, params_sha, seed)`` MUST produce equal hashes; the
   determinism test in :mod:`tests.unit.test_day_replay` locks this in.

Append-only JSONL audit trail at ``data/replay_runs.jsonl`` records every run
(``date``, ``code_sha``, ``params_sha``, ``replay_hash``, ``total_pnl``,
``snapshot_coverage_pct``, ``run_at``). This is the on-disk substitute for
the future ``replay_runs`` TimescaleDB hypertable described in plan §7.7
item 5 — same fields, same retention model, no DB dependency. We migrate to
the hypertable when the rest of the storage layer (P2) lands.

Out of scope for this module
----------------------------

* **Tick-mode replay** — currently the engine walks 60s chain snapshots only.
  Tick-level replay is plan §7.3 mode 2 and tracked under P3.
* **Counterfactual nightly hookup** — adding the divergence check to
  ``scripts/nightly_audit.py`` is a separate change once this harness has
  baked for a few days. See plan §7.5.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.backtest.replay_engine import ReplayBacktestEngine
from src.strategy.registry import get_params_schema  # noqa: F401  (forces strategy import paths)
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_ROOT = Path("config/snapshots")
DEFAULT_CHAIN_DIR = Path("data/chain_snapshots")
DEFAULT_SPOT_CSV = Path("data/nifty_spot_minute_chain.csv")
DEFAULT_VIX_CSV = Path("data/india_vix_minute_chain.csv")
DEFAULT_RUNS_LOG = Path("data/replay_runs.jsonl")

#: Map strategy name → expected key inside ``params.json``. Snapshot writer
#: dumps every Pydantic params class by **class name**, so the loader needs to
#: know which class belongs to which registered strategy. Add new strategies
#: here when they ship; missing entries fall through to the live-defaults
#: path with a ``params_source="live (no mapping)"`` breadcrumb in the result.
_PARAMS_CLASS_BY_STRATEGY: dict[str, str] = {
    "iron_condor": "IronCondorParams",
    "iron_butterfly": "IronButterflyParams",
    "short_strangle": "ShortStrangleParams",
    "short_straddle": "ShortStraddleParams",
    "long_calendar": "LongCalendarParams",
    "long_straddle": "LongStraddleParams",
    "trend_daily": "TrendDailyParams",
    "trend_itm": "TrendITMParams",
    "trend_debit_spread": "TrendDebitSpreadParams",
    "orchestrator": "OrchestratorParams",
}


@dataclass
class DayReplayResult:
    """Immutable record of a single replay run.

    Field order matters: the determinism hash is computed over
    ``asdict(self)`` minus ``replay_hash`` and ``run_at``. Adding fields is
    safe (they get hashed in too); reordering is not (would invalidate every
    historical hash). Append, don't insert.
    """

    date: str
    strategy: str
    underlying: str
    seed: int
    code_sha: str | None
    params_sha: str
    params_source: str            # "snapshot:<path>" | "live" | "live (no mapping)"
    snapshot_dir: str | None
    total_pnl: float
    num_days: int
    snapshot_coverage_pct: float
    fills_via_bid_ask: int
    fills_via_ltp_slip: int
    daily_results: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    chain_quality: dict[str, str] = field(default_factory=dict)
    skipped_days: list[str] = field(default_factory=list)
    error: str | None = None
    # Computed last so the hash body can exclude them.
    replay_hash: str = ""
    run_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────
# Helpers — frozen params, hashing, code identity
# ────────────────────────────────────────────────────────────────────

def _git_sha() -> str | None:
    """Resolve the current HEAD short SHA. Returns None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _canonical_json(payload: Any) -> bytes:
    """JSON encode with sorted keys and minimal separators — bit-stable across runs."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _params_sha(params: dict[str, Any]) -> str:
    return _sha256(_canonical_json(params))


def _resolve_params(
    target_date: date,
    strategy_name: str,
    snapshot_root: Path,
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, str | None]:
    """Resolve the params dict to use for a replay.

    Lookup order:
      1. ``<snapshot_root>/<date>/params.json`` (preferred — frozen day-of)
      2. Class defaults from the live :mod:`src.strategy.params` module

    Overrides are merged on top in either case. The returned tuple is
    ``(params_dict, source_label, snapshot_dir or None)`` where the source
    label is one of ``"snapshot:<path>"``, ``"live"``, or
    ``"live (no mapping)"``.

    The function is intentionally tolerant: a missing snapshot, an unknown
    strategy class, or a malformed JSON file all fall through to live
    defaults rather than crashing the replay. A breadcrumb is logged so
    the discrepancy is visible in audits.
    """
    overrides = dict(overrides or {})
    snapshot_dir = snapshot_root / target_date.isoformat()
    params_json = snapshot_dir / "params.json"

    class_key = _PARAMS_CLASS_BY_STRATEGY.get(strategy_name)
    if class_key is None:
        # Unknown strategy → live defaults only. Tests cover this branch.
        defaults = _live_params_defaults(strategy_name)
        defaults.update(overrides)
        return defaults, "live (no mapping)", None

    if params_json.exists():
        try:
            blob = json.loads(params_json.read_text())
        except json.JSONDecodeError as e:
            logger.warning(
                "params.json malformed, falling back to live defaults",
                extra={
                    "tag": Tag.SUMMARY, "phase": "replay",
                    "path": str(params_json), "error": str(e),
                },
            )
        else:
            section = blob.get(class_key)
            if isinstance(section, dict) and not section.get("_snapshot_error"):
                resolved = dict(section)
                resolved.update(overrides)
                return resolved, f"snapshot:{params_json}", str(snapshot_dir)
            logger.warning(
                "params.json missing class key — using live defaults",
                extra={
                    "tag": Tag.SUMMARY, "phase": "replay",
                    "path": str(params_json), "class_key": class_key,
                },
            )

    defaults = _live_params_defaults(strategy_name)
    defaults.update(overrides)
    return defaults, "live", None


def _live_params_defaults(strategy_name: str) -> dict[str, Any]:
    """Instantiate the params class with no args and return ``model_dump()``.

    Only used as a fallback. The class lookup goes through the registry so
    a strategy name typo surfaces as ``KeyError`` here, not as a silent
    "all defaults" run.
    """
    # Import locally so this module doesn't pay the strategy-import cost
    # unless replay_day actually runs. _import_strategies() is the canonical
    # registration trigger — `from src.strategy import implementations`
    # alone does NOT side-effect-load the submodules.
    from src.backtest.engine import _import_strategies
    from src.strategy.registry import _PARAMS_REGISTRY

    _import_strategies()

    if strategy_name not in _PARAMS_REGISTRY:
        # Honest failure: "we tried both sources, neither worked"
        raise KeyError(
            f"Strategy '{strategy_name}' not in params registry. "
            f"Known: {sorted(_PARAMS_REGISTRY.keys())}"
        )
    cls = _PARAMS_REGISTRY[strategy_name]
    return json.loads(cls().model_dump_json())


# ────────────────────────────────────────────────────────────────────
# Determinism — seed pinning + result canonicalization
# ────────────────────────────────────────────────────────────────────

def _pin_seeds(seed: int) -> None:
    """Pin Python and NumPy RNGs.

    Strategy code currently doesn't use either, but the engine creates
    objects whose hashes can leak through to dict ordering on some
    platforms. Pinning here is a one-line guarantee against future surprise.
    """
    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except ImportError:
        # NumPy is a hard dep, but keep the harness importable in any env.
        pass


def _hash_result_body(result: DayReplayResult) -> str:
    """Hash everything except the hash itself and the wall-clock timestamp."""
    body = result.to_dict()
    body.pop("replay_hash", None)
    body.pop("run_at", None)
    return _sha256(_canonical_json(body))


def _round_floats(obj: Any, places: int = 4) -> Any:
    """Round all floats inside a nested dict/list to ``places`` decimals.

    Floats are the worst stability hazard for hashing — different platforms
    can produce different last-digit values. Rounding to 4 decimals before
    hashing kills that whole class of false positives without hiding any
    real change (₹0.0001 P&L delta isn't a regression we care about).
    """
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: _round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, places) for v in obj]
    return obj


# ────────────────────────────────────────────────────────────────────
# Audit trail
# ────────────────────────────────────────────────────────────────────

def _append_run_log(runs_log: Path, result: DayReplayResult) -> None:
    """Append a one-line summary of the run to the JSONL audit trail.

    Atomic-enough for our use: O_APPEND on POSIX is atomic for writes
    smaller than PIPE_BUF (4096 bytes), and the summary record is well
    under that. Two concurrent replays still won't tear lines.

    We deliberately do NOT include ``daily_results`` or full ``metrics`` in
    the log — those live in the returned ``DayReplayResult``. The log is for
    "did this run happen, what's its hash, did it match the previous one".
    """
    runs_log.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_at": result.run_at,
        "date": result.date,
        "strategy": result.strategy,
        "underlying": result.underlying,
        "seed": result.seed,
        "code_sha": result.code_sha,
        "params_sha": result.params_sha,
        "params_source": result.params_source,
        "total_pnl": result.total_pnl,
        "num_days": result.num_days,
        "snapshot_coverage_pct": result.snapshot_coverage_pct,
        "replay_hash": result.replay_hash,
        "error": result.error,
    }
    line = json.dumps(summary, sort_keys=True, default=str)
    with open(runs_log, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


async def _persist_to_db(session_factory: Any, result: DayReplayResult) -> None:
    """Insert one row into the ``replay_runs`` table mirroring the JSONL line.

    The JSONL is the durable record (DATA_RELIABILITY_PLAN §7.7); this is
    its queryable mirror. Best-effort by design — DB unreachable / migration
    not applied / connection blip must never crash the replay. Caller wraps
    in try/except.

    Imports are deferred so :mod:`day_replay` doesn't import SQLAlchemy at
    module load (keeps unit tests fast and CLI startup snappy).
    """
    from src.db.models.replay_run import ReplayRunModel

    # ISO strings → typed values for SQLAlchemy. The DayReplayResult shape
    # uses strings (so the JSONL is human-friendly); the DB column wants
    # real types.
    run_at_dt = datetime.fromisoformat(result.run_at.replace("Z", "+00:00"))
    target_date = date.fromisoformat(result.date)

    async with session_factory() as session:
        row = ReplayRunModel(
            run_at=run_at_dt,
            target_date=target_date,
            strategy=result.strategy,
            underlying=result.underlying,
            seed=result.seed,
            code_sha=result.code_sha,
            params_sha=result.params_sha,
            params_source=result.params_source,
            snapshot_dir=result.snapshot_dir,
            total_pnl=result.total_pnl if result.error is None else None,
            num_days=result.num_days if result.error is None else None,
            snapshot_coverage_pct=result.snapshot_coverage_pct if result.error is None else None,
            fills_via_bid_ask=result.fills_via_bid_ask if result.error is None else None,
            fills_via_ltp_slip=result.fills_via_ltp_slip if result.error is None else None,
            replay_hash=result.replay_hash,
            error=result.error,
        )
        session.add(row)
        await session.commit()


# ────────────────────────────────────────────────────────────────────
# Public entrypoint
# ────────────────────────────────────────────────────────────────────

async def replay_day(
    target_date: date,
    strategy_name: str = "portfolio",
    *,
    underlying: str = "NIFTY",
    seed: int = 0,
    snapshot_root: Path | str = DEFAULT_SNAPSHOT_ROOT,
    chain_dir: Path | str = DEFAULT_CHAIN_DIR,
    spot_csv: Path | str = DEFAULT_SPOT_CSV,
    vix_csv: Path | str | None = DEFAULT_VIX_CSV,
    initial_capital: float = 1_000_000,
    extra_params: dict[str, Any] | None = None,
    runs_log: Path | str | None = DEFAULT_RUNS_LOG,
    db_session_factory: Any = None,
    skip_degraded: bool = True,
    accept_partial: bool = True,
) -> DayReplayResult:
    """Replay a single day with frozen-day-of params and emit a hashed result.

    Parameters
    ----------
    target_date :
        The trading day to replay. Must have a chain snapshot CSV in
        ``chain_dir`` (or the engine returns an error result).
    strategy_name :
        Registered strategy name (default ``"portfolio"`` — the one that's
        actually profitable on real data per ``MEMORY.md``).
    seed :
        RNG seed pinned before the run. Same seed → same hash.
    snapshot_root :
        Where to look for ``<date>/params.json``. Pass a temp dir in tests.
    runs_log :
        Append-only JSONL audit trail. Pass ``None`` to skip writing
        (handy in tests; production always wants it on).
    db_session_factory :
        Optional async SQLAlchemy session factory (typically the result of
        ``create_session_factory(engine)``). When provided, a row is
        inserted into the ``replay_runs`` table mirroring the JSONL line.
        Best-effort — DB unreachable / migration not applied / connection
        blip is logged but never raised. The JSONL stays as the durable
        record either way.
    skip_degraded, accept_partial :
        Forwarded to :class:`ReplayBacktestEngine` — see its docstring.

    Returns
    -------
    DayReplayResult
        Always returned, even on engine error (with ``error`` populated).
        This means the audit-trail line is written for failed runs too,
        which is what we want — silent failures are the bug we're trying
        to make impossible.
    """
    snapshot_root = Path(snapshot_root)
    chain_dir = Path(chain_dir)
    spot_csv = Path(spot_csv)
    vix_csv_path = Path(vix_csv) if vix_csv is not None else None
    runs_log_path = Path(runs_log) if runs_log is not None else None

    _pin_seeds(seed)

    # ── Resolve params (frozen-day-of preferred) ──────────────────
    params, params_source, snapshot_dir = _resolve_params(
        target_date, strategy_name, snapshot_root, extra_params,
    )
    # Underlying is part of params for every strategy; the kwarg is the
    # source of truth for the replay loader. Keep them in sync.
    params["underlying"] = underlying
    params_sha = _params_sha(params)

    code_sha = _git_sha()

    logger.info(
        "replay starting",
        extra={
            "tag": Tag.SUMMARY, "phase": "replay",
            "date": target_date.isoformat(), "strategy": strategy_name,
            "underlying": underlying, "seed": seed,
            "code_sha": code_sha, "params_sha": params_sha,
            "params_source": params_source,
        },
    )

    # ── Run the engine for this single day ────────────────────────
    engine = ReplayBacktestEngine()
    try:
        engine_result = await engine.run(
            strategy_name=strategy_name,
            snapshot_dir=chain_dir,
            spot_csv=spot_csv,
            vix_csv=vix_csv_path,
            strategy_params=params,
            num_days=1,
            start_date=target_date,
            initial_capital=initial_capital,
            skip_degraded=skip_degraded,
            accept_partial=accept_partial,
        )
    except Exception as e:
        logger.exception("replay engine raised")
        engine_result = {"error": f"{type(e).__name__}: {e}"}

    if "error" in engine_result:
        result = DayReplayResult(
            date=target_date.isoformat(),
            strategy=strategy_name,
            underlying=underlying,
            seed=seed,
            code_sha=code_sha,
            params_sha=params_sha,
            params_source=params_source,
            snapshot_dir=snapshot_dir,
            total_pnl=0.0,
            num_days=0,
            snapshot_coverage_pct=0.0,
            fills_via_bid_ask=0,
            fills_via_ltp_slip=0,
            error=engine_result["error"],
        )
    else:
        metrics = engine_result.get("metrics", {})
        # Round floats in the per-day rows + metrics so hashing is stable.
        daily = _round_floats(engine_result.get("daily_results", []))
        rounded_metrics = _round_floats(metrics)
        result = DayReplayResult(
            date=target_date.isoformat(),
            strategy=strategy_name,
            underlying=underlying,
            seed=seed,
            code_sha=code_sha,
            params_sha=params_sha,
            params_source=params_source,
            snapshot_dir=snapshot_dir,
            total_pnl=round(float(engine_result.get("final_pnl", 0.0)), 4),
            num_days=int(engine_result.get("num_days", 0)),
            snapshot_coverage_pct=float(metrics.get("snapshot_coverage_pct", 0.0)),
            fills_via_bid_ask=int(metrics.get("fills_via_bid_ask", 0)),
            fills_via_ltp_slip=int(metrics.get("fills_via_ltp_slip", 0)),
            daily_results=daily,
            metrics=rounded_metrics,
            chain_quality=engine_result.get("chain_quality", {}),
            skipped_days=engine_result.get("skipped_days", []),
        )

    result.replay_hash = _hash_result_body(result)
    result.run_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    # ── Audit trail (best-effort; never crash a replay over IO) ───
    if runs_log_path is not None:
        try:
            _append_run_log(runs_log_path, result)
        except OSError as e:
            logger.warning(
                "replay runs log append failed",
                extra={
                    "tag": Tag.SUMMARY, "phase": "replay",
                    "path": str(runs_log_path), "error": str(e),
                },
            )

    # ── DB mirror (best-effort; JSONL is the durable record) ─────
    # The DB row is the queryable view of the same data. If the DB is down
    # or the migration hasn't been applied, log and move on — never let it
    # poison the replay. Catches every Exception (not just OSError) because
    # SQLAlchemy can raise a wide variety: connection, schema, type errors.
    if db_session_factory is not None:
        try:
            await _persist_to_db(db_session_factory, result)
        except Exception as e:
            logger.warning(
                "replay runs DB insert failed (JSONL still written)",
                extra={
                    "tag": Tag.SUMMARY, "phase": "replay",
                    "error": f"{type(e).__name__}: {e}",
                    "replay_hash": result.replay_hash[:12] if result.replay_hash else None,
                },
            )

    logger.info(
        "replay complete",
        extra={
            "tag": Tag.SUMMARY, "phase": "replay",
            "date": result.date, "strategy": result.strategy,
            "total_pnl": result.total_pnl,
            "snapshot_coverage_pct": result.snapshot_coverage_pct,
            "replay_hash": result.replay_hash[:12],
            "error": result.error,
        },
    )

    return result


def replay_day_sync(*args: Any, **kwargs: Any) -> DayReplayResult:
    """Synchronous wrapper for the CLI / scripts that don't want an event loop."""
    return asyncio.run(replay_day(*args, **kwargs))
