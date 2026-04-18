"""Counterfactual replay — does the backtest match what we actually did today?

Implementation of ``docs/DATA_RELIABILITY_PLAN.md`` §7.5. After every live
trading day, replay the day with the live-active strategy params (frozen-of-
the-day if the snapshot exists), then compare:

  divergence = abs(replay_pnl - live_pnl)

If ``divergence`` exceeds the SLO tolerance (₹500 per plan §8.7), surface a
Telegram-ready alert string. The alert is the *what* — root-causing the
divergence is up to the operator. Common causes:

* **Param desync** — live used different thresholds than the snapshot we
  replayed against. Check ``params_source`` on the result.
* **Slippage / fill-model gap** — replay assumes mid + bid/ask spread; live
  got worse fills (low-volume strikes, fast markets).
* **Filter bug** — a filter that triggered live didn't trigger in replay
  (or vice versa). Check ``[FILTER]`` events in ``data/logs/trader.jsonl``.
* **Race condition** — the live process saw a tick the recorder didn't,
  or vice versa.

Used as both a CLI and a function called by ``scripts/nightly_audit.py``
(§4 of the audit). Returning a dataclass instead of just a message string
lets the audit caller incorporate the divergence into its DB write later.

This module is intentionally engine-agnostic at the type level — anything
that returns a ``DayReplayResult``-shaped object satisfies the contract.
That makes the unit tests trivial to mock without faking the chain CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Ensure the project root is importable when run as `uv run python scripts/...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same shadow-mode safety as the rest of the replay tooling. Without this,
# PAPER_TRADING=true makes the strategy enter on every signal regardless of
# score, which guarantees a divergence vs the production-gated live behaviour.
os.environ.setdefault("PAPER_TRADING", "false")

from src.advisor.collector import collect_today_data  # noqa: E402
from src.backtest.day_replay import DayReplayResult, replay_day  # noqa: E402
from src.config import Settings  # noqa: E402
from src.utils.log_tags import Tag  # noqa: E402

logger = logging.getLogger(__name__)

#: Default SLO tolerance from plan §8.7. ``|Δ| < ₹500`` per day on > 95% of
#: days. Tighter than this would alert on ordinary slippage; looser would
#: hide real bugs.
DEFAULT_TOLERANCE_INR = 500.0


@dataclass
class CounterfactualResult:
    """Outcome of a single live-vs-replay comparison."""

    date: str
    strategy: str
    live_pnl: float
    replay_pnl: float
    divergence: float           # absolute value
    tolerance: float
    breached: bool              # divergence > tolerance
    replay_hash: str
    replay_error: str | None
    params_source: str
    snapshot_coverage_pct: float
    # True when the replay loader couldn't find a frozen-day-of snapshot
    # and silently fell back to live params. Comparison number is then
    # only meaningful if params haven't changed since the trading day —
    # which we can't verify from inside this process. Surface it instead.
    params_source_is_fallback: bool
    telegram_message: str | None  # set when breached, replay errored, or fallback

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "strategy": self.strategy,
            "live_pnl": round(self.live_pnl, 2),
            "replay_pnl": round(self.replay_pnl, 2),
            "divergence": round(self.divergence, 2),
            "tolerance": self.tolerance,
            "breached": self.breached,
            "replay_hash": self.replay_hash,
            "replay_error": self.replay_error,
            "params_source": self.params_source,
            "params_source_is_fallback": self.params_source_is_fallback,
            "snapshot_coverage_pct": self.snapshot_coverage_pct,
        }


def _params_source_is_fallback(params_source: str | None) -> bool:
    """A snapshot-backed source is labelled ``snapshot:<path>`` by the loader.
    Anything else (``live``, ``live (no mapping)``, empty/None) means we did
    NOT replay against frozen-day-of params."""
    if not params_source:
        return True
    return not params_source.startswith("snapshot:")


# ────────────────────────────────────────────────────────────────────
# Pure helpers — easy to unit-test without an event loop
# ────────────────────────────────────────────────────────────────────

_FALLBACK_BANNER = (
    "⚠️ *Snapshot-fallback warning* — this replay used LIVE params, not "
    "frozen-day-of params. The divergence number below is meaningful ONLY if "
    "params didn't change since the trading day. Check "
    "``config/snapshots/{date}/`` and ``scripts/snapshot_config.py`` cron."
)


def _format_alert(
    target_date: date,
    strategy: str,
    live_pnl: float,
    replay: DayReplayResult,
    tolerance: float,
) -> str | None:
    """Build the Telegram alert string. Returns ``None`` when no alert is warranted.

    Three distinct alert flavours:
      * **Replay errored** — we couldn't compute a counterfactual at all,
        which is itself a signal worth flagging (the data, not the
        divergence, is the problem).
      * **Divergence breach** — we have a number, and it's outside SLO.
      * **Snapshot fallback** — replay ran against live params instead of
        frozen-day-of, so any "no divergence" reply could be a false negative.

    The fallback warning is *prepended* to the divergence/error alerts when
    both apply, and stands alone when neither would otherwise fire. This way
    a quiet day with a missing snapshot still surfaces — silence is the
    failure mode we're guarding against.
    """
    fallback = _params_source_is_fallback(replay.params_source)
    fallback_prefix = ""
    if fallback:
        fallback_prefix = _FALLBACK_BANNER.format(date=target_date.isoformat()) + "\n\n"

    if replay.error:
        return (
            f"{fallback_prefix}"
            f"🔁 *Counterfactual replay failed* for {target_date.isoformat()}\n\n"
            f"Strategy: `{strategy}`\n"
            f"Error: `{replay.error}`\n\n"
            f"Live P&L (uncompared): `{live_pnl:+,.2f}`\n"
            f"Action: investigate ``data/replay_runs.jsonl`` and "
            f"``data/logs/trader.jsonl``."
        )

    divergence = abs(replay.total_pnl - live_pnl)
    if divergence <= tolerance:
        if fallback:
            # Standalone fallback notice — no divergence breach, but the
            # comparison was degraded so the silence is suspect.
            return (
                f"{fallback_prefix.rstrip()}\n\n"
                f"Live P&L:    `{live_pnl:+,.2f}`\n"
                f"Replay P&L:  `{replay.total_pnl:+,.2f}`\n"
                f"Divergence:  `{divergence:,.2f}` (under tolerance `{tolerance:,.0f}`)\n"
                f"Strategy: `{strategy}`  Params src: `{replay.params_source}`"
            )
        return None

    # Direction tells us what to look at first.
    if replay.total_pnl > live_pnl:
        direction = (
            f"replay says we *should have made* ₹{replay.total_pnl - live_pnl:+,.2f} more."
        )
    else:
        direction = (
            f"replay says live *outperformed* the backtest by ₹{live_pnl - replay.total_pnl:+,.2f}."
        )

    return (
        f"{fallback_prefix}"
        f"🔁 *Backtest-live divergence* for {target_date.isoformat()}\n\n"
        f"Strategy: `{strategy}`\n"
        f"Live P&L:    `{live_pnl:+,.2f}`\n"
        f"Replay P&L:  `{replay.total_pnl:+,.2f}`\n"
        f"Divergence:  `{divergence:,.2f}` (tolerance `{tolerance:,.0f}`)\n"
        f"{direction}\n\n"
        f"Replay hash: `{replay.replay_hash[:12]}`\n"
        f"Params src:  `{replay.params_source}`\n"
        f"Snap coverage: `{replay.snapshot_coverage_pct:.0f}%`\n\n"
        f"Action: check ``[FILTER]`` and ``[ENTRY_QUALITY]`` events in "
        f"``data/logs/trader.jsonl`` for this date."
    )


# ────────────────────────────────────────────────────────────────────
# Public API — the function nightly_audit calls
# ────────────────────────────────────────────────────────────────────

async def compare_live_vs_replay(
    target_date: date,
    *,
    strategy_name: str = "portfolio",
    underlying: str = "NIFTY",
    tolerance_inr: float = DEFAULT_TOLERANCE_INR,
    settings: Settings | None = None,
    db_session_factory: Any = None,
    # The two collaborators below are injected to keep the function unit-
    # testable. Production callers leave them as the defaults.
    collect_fn: Any = collect_today_data,
    replay_fn: Any = replay_day,
) -> CounterfactualResult:
    """Replay ``target_date`` and compare against today's live P&L.

    The function never raises on divergence — it always returns a
    :class:`CounterfactualResult`. ``breached`` and ``telegram_message``
    are the signals to act on.

    The replay errors are NOT swallowed by ``replay_day`` (they're captured
    into ``DayReplayResult.error``), so we treat a replay error as its own
    alert flavour rather than a hard failure here.
    """
    settings = settings or Settings()

    # ── Fetch live actual P&L from today's structured logs / DB ──
    today = await collect_fn(
        target_date=target_date,
        log_dir=Path("logs") if Path("logs").exists() else None,
        settings=settings,
    )
    live_pnl = float(today.total_pnl)

    # ── Run the replay ──
    # Forward the session factory so the replay row lands in the DB mirror
    # alongside the JSONL audit line. The replay_fn is already best-effort
    # against DB outages, so a None factory or a flaky DB never blocks the
    # comparison from completing.
    replay_kwargs: dict[str, Any] = {
        "target_date": target_date,
        "strategy_name": strategy_name,
        "underlying": underlying,
    }
    if db_session_factory is not None:
        replay_kwargs["db_session_factory"] = db_session_factory
    replay_result: DayReplayResult = await replay_fn(**replay_kwargs)

    divergence = abs(replay_result.total_pnl - live_pnl)
    breached = (replay_result.error is None) and (divergence > tolerance_inr)
    message = _format_alert(
        target_date, strategy_name, live_pnl, replay_result, tolerance_inr,
    )

    cf = CounterfactualResult(
        date=target_date.isoformat(),
        strategy=strategy_name,
        live_pnl=live_pnl,
        replay_pnl=float(replay_result.total_pnl),
        divergence=divergence,
        tolerance=tolerance_inr,
        breached=breached,
        replay_hash=replay_result.replay_hash,
        replay_error=replay_result.error,
        params_source=replay_result.params_source,
        params_source_is_fallback=_params_source_is_fallback(replay_result.params_source),
        snapshot_coverage_pct=replay_result.snapshot_coverage_pct,
        telegram_message=message,
    )

    logger.info(
        "counterfactual replay complete",
        extra={
            "tag": Tag.SUMMARY, "phase": "counterfactual",
            "date": cf.date, "strategy": cf.strategy,
            "live_pnl": cf.live_pnl, "replay_pnl": cf.replay_pnl,
            "divergence": round(cf.divergence, 2),
            "breached": cf.breached,
            "replay_error": cf.replay_error,
            "params_source": cf.params_source,
            "params_source_is_fallback": cf.params_source_is_fallback,
            "replay_hash": cf.replay_hash[:12] if cf.replay_hash else None,
        },
    )

    return cf


# ────────────────────────────────────────────────────────────────────
# CLI — same shape as the other scripts in this dir
# ────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--date", required=True, type=date.fromisoformat,
                   help="Trading day to compare (YYYY-MM-DD)")
    p.add_argument("--strategy", default="portfolio",
                   help="Registered strategy name (default: portfolio)")
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_INR,
                   help=f"Divergence alert threshold in ₹ (default: {DEFAULT_TOLERANCE_INR})")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


async def _amain(args: argparse.Namespace) -> int:
    cf = await compare_live_vs_replay(
        target_date=args.date,
        strategy_name=args.strategy,
        underlying=args.underlying,
        tolerance_inr=args.tolerance,
    )

    print(f"date:         {cf.date}")
    print(f"strategy:     {cf.strategy}")
    print(f"live_pnl:     {cf.live_pnl:+,.2f}")
    print(f"replay_pnl:   {cf.replay_pnl:+,.2f}")
    print(f"divergence:   {cf.divergence:,.2f}  (tolerance {cf.tolerance:,.0f})")
    print(f"breached:     {cf.breached}")
    print(f"params_src:   {cf.params_source}")
    print(f"replay_hash:  {cf.replay_hash[:16] if cf.replay_hash else 'n/a'}")
    if cf.replay_error:
        print(f"replay_error: {cf.replay_error}")
    if cf.telegram_message:
        print()
        print("--- Telegram alert preview ---")
        print(cf.telegram_message)

    return 1 if cf.breached or cf.replay_error else 0


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
