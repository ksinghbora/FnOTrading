"""Parity between runtime regime labels and harness stratifier labels.

The P1.5 regime gate relies on ``BaseStrategy._current_regime_labels``
returning the **same** label set that ``bucket_row`` in
``src/backtest/validation/regime.py`` would assign to the equivalent
post-hoc decision row. If the two classifiers drift — e.g. someone
shifts the stratifier's VIX band from >15 to >16 without updating the
runtime helper — then blocking a regime at runtime no longer maps to
the measured bucket, and the validation gate stops being a meaningful
instrument.

These tests are the contract between the two sites. Any future change
to either classifier must keep this test green.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.validation.regime import bucket_row
# After PortfolioStrategy was retired (May 7 2026), this parity test
# uses IronCondorStrategy as the concrete BaseStrategy subclass —
# ``_current_regime_labels`` and ``_check_blocked_regime`` are inherited
# from BaseStrategy and behave identically across all strategy classes.
from src.strategy.calibrations.iron_condor import IronCondorParams
from src.strategy.implementations.iron_condor import IronCondorStrategy


# ── Scenario grid ───────────────────────────────────────────────────
#
# Enumerate VIX × move_from_open_pct × expiry-proximity so we exercise
# every branch of the stratifier: low/mid/high VIX, trending / chop /
# range-bound move, near/far expiry.

SCENARIOS = [
    # (vix, move_pct, dte_days, is_event, description)
    (12.0,  0.2, 5, False, "low_vix + range_bound, weekly"),
    (14.0,  0.3, 3, False, "mid_vix + range_bound, mid-week"),
    (16.0,  0.4, 1, False, "high_vix + range_bound, expiry-week"),
    (14.0,  0.8, 4, False, "mid_vix + chop (0.5<m<=1.0), no label on move"),
    (14.0,  1.5, 4, False, "mid_vix + trending up"),
    (14.0, -1.5, 4, False, "mid_vix + trending down"),
    (22.0,  2.0, 0, False, "high_vix + trending + expiry day"),
    (10.0,  0.0, 2, True,  "low_vix + event_day + expiry_week boundary"),
    (15.0,  0.5, 7, False, "mid_vix boundary (==15), range_bound boundary"),
    (15.1,  1.0, 7, False, "high_vix just over, trending boundary (==1.0 NOT >1.0)"),
    (13.0,  0.51, 7, False, "mid_vix boundary, move just over 0.5 (no range_bound)"),
]


def _make_runtime_strategy(
    vix: float,
    spot: float,
    session_open: float,
    now: datetime,
    expiry: date | None,
    event_dates: dict[date, str],
) -> IronCondorStrategy:
    """Build a mocked IronCondorStrategy that feeds the runtime classifier.

    Stubs cover exactly the context surface ``_current_regime_labels`` +
    ``_move_from_open_pct`` read: get_vix, get_spot_price, clock.now,
    get_candles, and the chain_builder's _spot_tokens dict.
    """
    s = IronCondorStrategy("test_parity", IronCondorParams())

    ctx = MagicMock()
    ctx.get_vix = MagicMock(return_value=vix)
    ctx.get_spot_price = MagicMock(return_value=Decimal(str(spot)))

    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=now)

    # Candle list — first candle's open is the session reference.
    first_candle = MagicMock()
    first_candle.open = session_open
    ctx.get_candles = MagicMock(return_value=[first_candle])

    # chain_builder._spot_tokens is iterated to find a token for the
    # underlying. NIFTY → any integer token will do.
    ctx._chain_builder = MagicMock()
    ctx._chain_builder._spot_tokens = {123: "NIFTY"}

    s.set_context(ctx)
    s._expiry = expiry
    # Pre-seed the cached event-dates dict so the runtime helper doesn't
    # try to load a CSV during the test.
    s._regime_event_dates = event_dates
    return s


def _stratifier_row(
    vix: float,
    move_pct: float,
    dte: int,
    is_expiry: bool,
    date_: date,
) -> pd.Series:
    """Build the minimum pandas row bucket_row reads."""
    return pd.Series(
        {
            "vix": vix,
            "move_from_open_pct": move_pct,
            "dte": dte,
            "is_expiry": is_expiry,
            "date": date_,
        }
    )


@pytest.mark.parametrize(
    "vix,move_pct,dte_days,is_event,description",
    SCENARIOS,
    ids=[s[4] for s in SCENARIOS],
)
def test_runtime_labels_match_stratifier(
    vix: float,
    move_pct: float,
    dte_days: int,
    is_event: bool,
    description: str,
) -> None:
    """The runtime classifier must return the same label set as ``bucket_row``."""
    today = date(2026, 4, 24)
    expiry = today + timedelta(days=dte_days)
    spot = 23000.0
    # Derive session_open so (spot - open) / open * 100 == move_pct.
    # Fair parity requires feeding the stratifier the ACTUAL value the
    # runtime computes back from (spot, open) — not the idealized scenario
    # number — because IEEE-754 rounding can push an intended-1.0 move
    # across the >1.0 trending boundary. In production the same rounding
    # happens on both sides (strategy computes it, writes to CSV,
    # stratifier reads it back), so agreement is the contract.
    session_open = spot / (1.0 + move_pct / 100.0)
    actual_move_pct = (spot - session_open) / session_open * 100.0
    now = datetime(2026, 4, 24, 11, 0)

    event_dates = {today: "RBI_POLICY"} if is_event else {}

    s = _make_runtime_strategy(
        vix=vix,
        spot=spot,
        session_open=session_open,
        now=now,
        expiry=expiry,
        event_dates=event_dates,
    )

    runtime_labels = set(s._current_regime_labels("NIFTY", expiry))

    row = _stratifier_row(
        vix=vix,
        move_pct=actual_move_pct,
        dte=dte_days,
        # bucket_row checks is_expiry OR dte<=2; our strategy side mirrors
        # that via (expiry - today).days <= 2.
        is_expiry=(dte_days == 0),
        date_=today,
    )
    stratifier_labels = set(bucket_row(row, event_dates))

    assert runtime_labels == stratifier_labels, (
        f"{description}: runtime={runtime_labels} stratifier={stratifier_labels}"
    )


def test_blocked_regime_returns_none_when_blocklist_empty() -> None:
    """Empty blocklist must be a no-op regardless of current labels."""
    today = date(2026, 4, 24)
    s = _make_runtime_strategy(
        vix=22.0,
        spot=23000.0,
        session_open=22500.0,  # +2.2% → trending
        now=datetime(2026, 4, 24, 11, 0),
        expiry=date(2026, 4, 25),
        event_dates={},
    )
    # Pass an explicit empty list — should not block even though vix is
    # high and move is trending.
    assert s._check_blocked_regime("NIFTY", s._expiry, blocked=[]) is None


def test_blocked_regime_blocks_high_vix() -> None:
    """Concrete sanity: gate fires when current tick maps to high_vix."""
    s = _make_runtime_strategy(
        vix=20.0,                # > 15 → high_vix
        spot=23000.0,
        session_open=23000.0,    # 0% move → range_bound
        now=datetime(2026, 4, 24, 11, 0),
        expiry=date(2026, 4, 30),
        event_dates={},
    )
    reason = s._check_blocked_regime(
        "NIFTY", s._expiry, blocked=["high_vix", "trending"]
    )
    assert reason is not None
    assert "high_vix" in reason


def test_blocked_regime_respects_params_default() -> None:
    """When ``blocked`` is None the helper reads ``params.blocked_regimes``."""
    s = _make_runtime_strategy(
        vix=20.0,
        spot=23000.0,
        session_open=23000.0,
        now=datetime(2026, 4, 24, 11, 0),
        expiry=date(2026, 4, 30),
        event_dates={},
    )
    # IronCondorParams inherits BaseStrategyParams.blocked_regimes=[]
    # by default. A call without an explicit list falls through to []:
    # no block.
    assert s._check_blocked_regime("NIFTY", s._expiry) is None
