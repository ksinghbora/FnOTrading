"""Tests for the Apr 29 2026 Phase 2 audit fix: cross-strategy uniformity.

The multi-model audit flagged that several entry-gate primitives were
implemented inconsistently across the four active strategies:

  - ``max_spread_pct`` (the Apr 28 IC liquidity filter) lived only on
    IronCondorParams; strangle/straddle/calendar had no equivalent.
  - The ``score < 60`` entry-quality threshold was a hardcoded literal
    in three different strategy files, not sweepable.
  - IC + IB never called ``_check_trend_filter`` while strangle and
    straddle did — same regime exposure, different gate.

Phase 2 promotes ``max_spread_pct`` and ``entry_score_threshold`` to
``BaseStrategyParams``, moves ``_check_strike_liquidity`` to
``BaseStrategy``, and wires the trend filter into IC's entry path.
These tests pin the contract.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── Param promotion ────────────────────────────────────────────────


def test_max_spread_pct_lives_on_base_params():
    """All four params classes inherit max_spread_pct=5.0 from
    BaseStrategyParams. If a future change re-introduces a per-strategy
    override or removes the field, this surfaces it."""
    from src.strategy.params import (
        BaseStrategyParams,
        IronCondorParams,
        IronButterflyParams,
        ShortStrangleParams,
        ShortStraddleParams,
        LongCalendarParams,
    )
    assert BaseStrategyParams().max_spread_pct == 5.0
    assert IronCondorParams().max_spread_pct == 5.0
    assert IronButterflyParams().max_spread_pct == 5.0
    assert ShortStrangleParams().max_spread_pct == 5.0
    assert ShortStraddleParams().max_spread_pct == 5.0
    assert LongCalendarParams().max_spread_pct == 5.0


def test_entry_score_threshold_lives_on_base_params():
    """Default 60 reproduces the prior hardcoded literal at three
    different sites in IC / strangle / straddle. Moving it to
    BaseStrategyParams makes it sweepable."""
    from src.strategy.params import (
        BaseStrategyParams,
        IronCondorParams,
        ShortStrangleParams,
        ShortStraddleParams,
    )
    assert BaseStrategyParams().entry_score_threshold == 60
    assert IronCondorParams().entry_score_threshold == 60
    assert ShortStrangleParams().entry_score_threshold == 60
    assert ShortStraddleParams().entry_score_threshold == 60


def test_entry_score_threshold_overridable_via_constructor():
    """Sweep harness needs to be able to override per config."""
    from src.strategy.params import IronCondorParams
    p = IronCondorParams(entry_score_threshold=70)
    assert p.entry_score_threshold == 70


# ── Liquidity helper on BaseStrategy ───────────────────────────────


def _strategy_with_max_spread(strategy_name: str, max_spread_pct: float = 5.0):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy(strategy_name, strategy_id=f"{strategy_name}_test",
                        params={"max_spread_pct": max_spread_pct})
    s._context = MagicMock()
    return s


def _opt(bid: float, ask: float, *, symbol: str = "X"):
    from decimal import Decimal
    o = MagicMock()
    o.bid_price = Decimal(str(bid))
    o.ask_price = Decimal(str(ask))
    o.tradingsymbol = symbol
    return o


def test_check_strike_liquidity_works_on_base_strategy():
    """The helper must be callable on ANY BaseStrategy subclass — not
    just IC. Verify it works for strangle/straddle/calendar."""
    for name in ("short_strangle", "short_straddle", "long_calendar"):
        s = _strategy_with_max_spread(name)
        # Tight quote passes
        assert s._check_strike_liquidity(_opt(50.0, 50.5), "ce") is None
        # Wide quote blocks
        reason = s._check_strike_liquidity(_opt(1.0, 1.4), "ce")
        assert reason is not None and "spread" in reason


def test_check_strike_liquidity_disabled_at_zero_threshold():
    """max_spread_pct=0 disables the filter for every strategy."""
    for name in ("iron_condor", "short_strangle", "short_straddle", "long_calendar"):
        s = _strategy_with_max_spread(name, max_spread_pct=0.0)
        # Even an absurd 100% spread passes
        assert s._check_strike_liquidity(_opt(1.0, 100.0), "ce") is None


# ── IC trend filter ────────────────────────────────────────────────


def test_iron_condor_calls_trend_filter_in_try_entry():
    """Phase 2 added _check_trend_filter to IC's entry path. Verify the
    call appears in the source — a regex-level pin, since spinning up a
    full IC entry simulation is heavyweight. If a future refactor moves
    the call without an equivalent, this surfaces it."""
    from pathlib import Path
    src = Path("src/strategy/implementations/iron_condor.py").read_text()
    # The filter is called via self._check_trend_filter inside _try_entry.
    # We don't inspect *where* in _try_entry — just that it's called there.
    assert "self._check_trend_filter(self.params.underlying)" in src
    assert "ENTRY_SKIP_TREND" in src


def test_iron_butterfly_inherits_iron_condor_trend_filter():
    """IB is a pure subclass of IC with no override — Phase 2's trend
    filter therefore applies to IB for free. Pin that the inheritance
    relationship is unchanged so the assumption stays valid."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.implementations.iron_butterfly import IronButterflyStrategy
    from src.strategy.implementations.iron_condor import IronCondorStrategy
    assert issubclass(IronButterflyStrategy, IronCondorStrategy)
    # _try_entry not overridden
    assert (
        IronButterflyStrategy._try_entry is IronCondorStrategy._try_entry
    ), "IB has overridden _try_entry — the IC-trend-filter inheritance is no longer free"
