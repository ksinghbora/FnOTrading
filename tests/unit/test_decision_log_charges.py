"""Tests for the Apr 29 2026 audit fix: charges column on DecisionSnapshot.

The bookkeeping audit found that ``decision_log.outcome_pnl`` was gross
of charges (STT/brokerage/GST/SEBI fees), while the engine's realised
P&L was net. Downstream stratifiers consume ``outcome_pnl`` directly,
so per-bucket Sharpe was systematically over-stated by ~₹100-300 per
round trip.

The fix:
  - ``DecisionSnapshot.charges`` field (defaults to 0).
  - ``BaseStrategy._log_decision`` snapshots cumulative charges at
    ENTER and writes the entry→exit delta on EXIT rows.
  - ENTER rows carry charges=0 (no round-trip yet realised).

These tests pin the contract so a future refactor can't quietly
remove the charges plumbing again.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock


# ── DecisionSnapshot schema contract ────────────────────────────────


def test_decision_snapshot_has_charges_field():
    from src.strategy.decision_logger import DecisionSnapshot
    fields = {f.name: f for f in dataclasses.fields(DecisionSnapshot)}
    assert "charges" in fields, "charges field missing from DecisionSnapshot"
    f = fields["charges"]
    # Default 0.0 — ENTER rows must not carry phantom charges
    assert f.default == 0.0


def test_charges_in_csv_columns():
    """The CSV writer iterates COLUMNS; if charges isn't there, it
    silently drops to empty-string regardless of what the snapshot
    holds. Pin its presence."""
    from src.strategy.decision_logger import COLUMNS
    assert "charges" in COLUMNS


# ── _log_decision charges-delta logic ───────────────────────────────


def _strategy_with_mock_ctx(charges_sequence: list[float]):
    """Construct an iron_condor with a context whose `get_pnl().charges`
    yields the values in order."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("iron_condor", strategy_id="ic_charges_test")
    ctx = MagicMock()
    pnl_mocks = []
    for c in charges_sequence:
        m = MagicMock()
        m.charges = Decimal(str(c))
        pnl_mocks.append(m)
    ctx.get_pnl.side_effect = pnl_mocks
    # Wire the rest of the ctx surface _log_decision touches.
    ctx.clock.now.return_value = datetime(2025, 9, 9, 9, 30)
    ctx.get_spot_price.return_value = Decimal("23950")
    ctx.get_vix.return_value = 17.4
    s._context = ctx
    # Stub the decision logger so we capture written snapshots in memory.
    written: list = []
    s._decision_logger = MagicMock()
    s._decision_logger.log = lambda snap: written.append(snap)
    return s, written


def test_enter_row_writes_zero_charges():
    """ENTER rows must carry charges=0 — no round-trip has closed yet."""
    s, written = _strategy_with_mock_ctx([100.0])  # charges at ENTER
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    assert len(written) == 1
    assert written[0].decision == "ENTER"
    assert written[0].charges == 0.0


def test_exit_row_writes_charges_delta_since_enter():
    """EXIT charges = current cumulative - cumulative at last ENTER."""
    s, written = _strategy_with_mock_ctx([100.0, 275.0])  # 100 at enter, 275 at exit
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", exit_reason="TP", outcome_pnl=2250.0)
    assert len(written) == 2
    enter, exit_ = written
    assert enter.charges == 0.0
    # Round-trip charge = 275 - 100 = 175
    assert exit_.charges == 175.0


def test_consecutive_trades_dont_leak_charges():
    """After EXIT, the next ENTER starts a fresh charges baseline so
    trade #2's EXIT records ONLY trade #2's charges."""
    s, written = _strategy_with_mock_ctx([100.0, 275.0, 275.0, 410.0])
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2250.0)
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=28.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2100.0)
    assert len(written) == 4
    # First trade
    assert written[0].charges == 0.0
    assert written[1].charges == 175.0
    # Second trade — fresh baseline
    assert written[2].charges == 0.0
    assert written[3].charges == 135.0  # 410 - 275


def test_negative_delta_clamped_to_zero():
    """If the portfolio resets charges mid-trade (e.g., daily reset
    between ENTER and EXIT), the delta could go negative. Clamp to 0
    rather than reporting nonsense like 'this trade earned -₹50 in
    charges'."""
    s, written = _strategy_with_mock_ctx([200.0, 50.0])  # daily reset between
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2250.0)
    assert written[1].charges == 0.0


def test_charges_lookup_failure_doesnt_block_logging():
    """get_pnl() raising shouldn't kill the decision row. Logging is
    observational — never break trading correctness for a metrics issue."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("iron_condor", strategy_id="ic_charges_fail")
    ctx = MagicMock()
    ctx.get_pnl.side_effect = RuntimeError("portfolio not wired")
    ctx.clock.now.return_value = datetime(2025, 9, 9, 9, 30)
    ctx.get_spot_price.return_value = Decimal("23950")
    ctx.get_vix.return_value = 17.4
    s._context = ctx
    written: list = []
    s._decision_logger = MagicMock()
    s._decision_logger.log = lambda snap: written.append(snap)
    # Should not raise
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2250.0)
    assert len(written) == 2
    # Both fall back to 0 charges (acceptable when portfolio path fails)
    assert written[0].charges == 0.0
    assert written[1].charges == 0.0
