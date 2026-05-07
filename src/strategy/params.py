"""Strategy parameter schemas.

This file is now:

  - The home of ``BaseStrategyParams`` (shared across all strategies)
  - A backward-compatibility shim: re-exports the migrated classes so
    legacy ``from src.strategy.params import IronCondorParams`` keeps
    working without touching every callsite

NEW CODE should import directly from the calibration module:

    from src.strategy.calibrations.iron_condor import IronCondorParams

This makes the per-strategy isolation contract explicit at the
import site — recalibrating IC means editing exactly one file.
"""

from datetime import time
from decimal import Decimal

from pydantic import BaseModel


class BaseStrategyParams(BaseModel):
    """Base parameters common to all strategies."""

    underlying: str = "NIFTY"
    quantity_lots: int = 1
    entry_time: time = time(9, 20)
    exit_time: time = time(15, 15)
    max_loss: Decimal = Decimal("5000")
    product: str = "NRML"
    use_weekly_expiry: bool = True

    # Shadow-only mode — strategy generates signals (full decision pipeline
    # runs, decisions logged to CSV) but the runner short-circuits before
    # calling the OMS. Used to A/B challenger strategies against a single
    # live "champion" strategy on the same paper-trading capital pool, so
    # P&L attribution stays clean. The decision-log captures
    # entry/exit/score so the challenger's hypothetical performance can be
    # reconstructed offline. Default False — strategies trade as usual.
    shadow_only: bool = False
    # VIX filter — Indian-calibrated bands (see src/core/constants.py).
    # Strategy subclasses override these with band-appropriate values.
    vix_entry_min: float = 0.0        # Skip entry if VIX < this (e.g., 13 = no premium below complacency)
    vix_entry_max: float = 22.0       # Skip entry if VIX > this (default = IC stressed boundary)
    vix_reduce_above: float = 18.0    # Halve position size if VIX > this

    # PCR filter — skip entry when PCR_OI is outside healthy range
    pcr_filter_enabled: bool = True    # Enabled — blocks entries in dangerous OI regimes
    pcr_oi_min: float = 0.7           # Skip if PCR_OI < this (call-heavy, bearish/volatile)
    pcr_oi_max: float = 1.5           # Skip if PCR_OI > this (extreme put hedging)

    # Max pain filter — skip entry when spot is far from max pain
    max_pain_filter_enabled: bool = True   # Enabled — skip when spot drifts from max pain
    max_pain_proximity_pct: float = 3.0    # Skip if spot > X% from max pain

    # Expiry-day 0DTE safety (Tuesday on NIFTY weekly per SEBI Nov 2024).
    # Entering naked premium with same-day expiry = 0DTE gamma trap (14:30-15:15
    # gamma vertical can move ATM 100% in minutes; STT on auto-exercise eats wins).
    skip_entry_on_expiry_day: bool = True   # Block new entries when today == expiry
    expiry_day_force_exit_at: time = time(14, 30)  # Exit ALL legs before gamma vertical

    # Phase 3b Gate B — intraday VIX-spike filter (PRE-REGISTERED, default off).
    # Blocks new entries on days where VIX has risen >= threshold% from morning
    # open after a configurable activation time. Designed for iron_condor based
    # on the May 8 2025 spike (VIX 15.6 → 22.8 in last 90 min) which produced
    # the wf_coverage failure. See reports/phase3b_research/regime_gate_proposal.md.
    # Default disabled — operator must opt-in to test on holdout. Per discipline
    # §VII.7, this gate has not been calibrated on validation data.
    intraday_vix_spike_enabled: bool = False
    intraday_vix_spike_threshold_pct: float = 15.0    # +15% from morning open
    intraday_vix_spike_activate_after: time = time(11, 30)  # IST, gate active after this

    # Trail-stop activation gates (Apr 17 trader-analysis fix).
    # First 30 min of session is auction-imbalance noise — premium can swing
    # 10-20% on a directionless day. Trailing during that window locks losses
    # on whipsaws. Two gates must both pass before trail-stop can fire:
    #   1. Time gate: now >= trail_stop_activate_after_time
    #   2. Move gate: premium has decayed at least trail_stop_min_decay_pct
    #      from entry (proxy for "real move > N x ATR" until intraday ATR
    #      tracker lands).
    trail_stop_activate_after_time: time = time(10, 15)
    trail_stop_min_decay_pct: float = 5.0

    # ─── Vol-scaled exits (opt-in, OFF by default) ─────────────────────
    # Problem: hardcoded SL/PT/trail percentages are tuned to one VIX
    # regime. At VIX=11 a 25% SL = ~2 ticks of normal noise; at VIX=22 it
    # = ~25 ticks of normal noise. Same parameter, contradictory behavior
    # across regimes — a known curve-fit seed.
    #
    # Fix: scale the effective % by expected one-sigma premium move over
    # DTE, using `sl_vol_k × (vix/100) × sqrt(dte/365)` where `k` is the
    # multiplier calibrated to reproduce current behavior at VIX=15 weekly.
    #
    # Calibration (VIX=15, weekly expiry mid T=7/365):
    #   sigma * sqrt(T) = 0.15 * sqrt(7/365)
    #                   = 0.15 * 0.1384
    #                   = 0.02077
    # Current 25% SL => k = 0.25 / 0.02077 ~= 12.0
    # Current 12% PT => k_pt ~= 5.8
    # Current 10% trail => k_trail ~= 4.8
    # These defaults reproduce the existing behavior at VIX=15 weekly.
    #
    # Effective % is clamped to [0.10, 0.60] to prevent absurd values on
    # expiry-day VIX spikes or near-zero DTE denominators.
    #
    # Opt-in via `vol_scaled_exits=True` on the concrete params instance.
    # A/B in shadow mode BEFORE flipping the default — this is a behavior
    # change on every existing backtest result in memory files.
    vol_scaled_exits: bool = False
    sl_vol_k: float = 12.0
    pt_vol_k: float = 5.8
    trail_vol_k: float = 4.8

    # ─── Apr 29 2026 Phase 2 — uniform cross-strategy gates ──────────
    # Promoted from IronCondorParams so strangle/straddle/calendar share
    # the same liquidity filter and score threshold. Subclasses can
    # override via their own field definitions if a strategy needs a
    # tighter or looser default (none currently do — calibrated values
    # were the same constant repeated in IC's _try_entry).

    # Reject any candidate strike whose bid-ask spread exceeds this
    # fraction of mid. The chain-gap diagnostic showed spreads on
    # deep-OTM wings can easily eat the IC's edge; the same risk
    # applies to far-OTM strangle / straddle / calendar legs. 0
    # disables the filter (kept for bisection / regression-test use).
    max_spread_pct: float = 5.0
    # Minimum signal score (0-100) required to fire entry. Strategies
    # that compute their own multi-factor score gate against this.
    # Default 60 reproduces the prior hardcoded literal at three
    # different sites (iron_condor.py:244, short_strangle.py:130,
    # short_straddle.py:137) which were never sweepable until now.
    entry_score_threshold: int = 60

    # ─── P1.5 regime gate ─────────────────────────────────────────────
    # Block new entries when the current tick classifies into any of these
    # regime labels. Labels use the EXACT thresholds from the harness
    # stratifier (src/backtest/validation/regime.py `bucket_row`):
    #   high_vix  : VIX > 15
    #   mid_vix   : 13 <= VIX <= 15
    #   low_vix   : VIX < 13
    #   expiry_week : dte <= 2 or is_expiry
    #   event_day : today ∈ data/event_days.csv (HARD_BLOCK|SOFT_CAUTION)
    #   trending  : |move_from_open_pct| > 1.0
    #   range_bound : |move_from_open_pct| <= 0.5
    # Empty list (default) = no regime gating, backward-compatible.
    # Set to e.g. ["high_vix", "trending"] to have the strategy skip
    # entries when either label is active — mirrors the harness regime
    # gate 1:1 so a blocked-at-runtime bucket cannot appear in the
    # stratified report.
    blocked_regimes: list[str] = []

    # ─── V5 (May 7 2026): margin-per-lot estimate ──────────────────
    # Used by OrchestratorStrategy V5 when ``margin_aware_selection``
    # is enabled. Rough SPAN+ELM margin per 1 lot, expressed in lakhs
    # of rupees (₹ × 1e5). Strategies override this per their structural
    # margin profile — see each strategy's calibration module.
    expected_margin_per_lot_lakhs: float = 2.0


# ─── Backward-compat re-exports (lazy via PEP 562 __getattr__) ─────
# Active strategies' params classes live in their per-strategy
# calibration modules. We expose them as attributes of this module
# via a lazy __getattr__ to avoid circular imports — every calibration
# module imports ``BaseStrategyParams`` from here at module-init time,
# so a top-level re-export from calibration modules would deadlock.
#
# Existing call sites like ``from src.strategy.params import
# IronCondorParams`` keep working unchanged — Python's import machinery
# falls through to ``__getattr__`` for module-level attribute lookups
# (PEP 562). New code should import directly from the calibration
# module to make the per-strategy isolation contract visible at the
# import site:
#
#     from src.strategy.calibrations.iron_condor import IronCondorParams

_PARAMS_REEXPORTS = {
    "IronCondorParams": ("src.strategy.calibrations.iron_condor", "IronCondorParams"),
    "IronButterflyParams": ("src.strategy.calibrations.iron_butterfly", "IronButterflyParams"),
    "ShortStrangleParams": ("src.strategy.calibrations.short_strangle", "ShortStrangleParams"),
    "ShortStraddleParams": ("src.strategy.calibrations.short_straddle", "ShortStraddleParams"),
    "LongCalendarParams": ("src.strategy.calibrations.long_calendar", "LongCalendarParams"),
    "LongStraddleParams": ("src.strategy.calibrations.long_straddle", "LongStraddleParams"),
    "TrendDailyParams": ("src.strategy.calibrations.trend_daily", "TrendDailyParams"),
    "TrendITMParams": ("src.strategy.calibrations.trend_itm", "TrendITMParams"),
    "TrendDebitSpreadParams": ("src.strategy.calibrations.trend_debit_spread", "TrendDebitSpreadParams"),
    "OrchestratorParams": ("src.strategy.calibrations.orchestrator", "OrchestratorParams"),
}


def __getattr__(name: str):
    if name in _PARAMS_REEXPORTS:
        import importlib
        module_path, attr = _PARAMS_REEXPORTS[name]
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseStrategyParams",
    # Active strategies — params class lives in src/strategy/calibrations/<name>.py
    "IronCondorParams",
    "IronButterflyParams",
    "ShortStrangleParams",
    "ShortStraddleParams",
    "LongCalendarParams",
    "LongStraddleParams",
    "TrendDailyParams",
    "TrendITMParams",
    "TrendDebitSpreadParams",
    "OrchestratorParams",
]
