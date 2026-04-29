"""Tests for the Apr 29 2026 Phase 1C audit fix:

  - DecisionSnapshot.trade_id field + COLUMNS entry
  - BaseStrategy._log_decision mints UUID on ENTER, propagates through
    ADJUST rows, re-stamps on EXIT, then clears
  - BaseStrategy._log_adjust_decision helper writes an ADJUST row with
    the active trade_id
  - regime.py._pair_enter_exit prefers trade_id pairing when present and
    sums ADJUST + EXIT outcome_pnl into one lifecycle figure
  - Legacy decision logs without trade_id still work via the cumcount
    fallback (ENTER/EXIT only — ADJUST rows weren't emitted before)
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd


# ── Schema contract ────────────────────────────────────────────────


def test_decision_snapshot_has_trade_id_field():
    from src.strategy.decision_logger import DecisionSnapshot
    fields = {f.name: f for f in dataclasses.fields(DecisionSnapshot)}
    assert "trade_id" in fields
    assert fields["trade_id"].default == ""


def test_trade_id_in_csv_columns():
    from src.strategy.decision_logger import COLUMNS
    assert "trade_id" in COLUMNS


# ── Trade-id lifecycle (mint on ENTER, propagate, clear after EXIT) ──


def _strategy_for_lifecycle(charges_sequence: list[float] | None = None):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("iron_condor", strategy_id="ic_lifecycle_test")
    ctx = MagicMock()
    if charges_sequence is None:
        charges_sequence = [0.0] * 20  # plenty of head-room
    pnl_mocks = [MagicMock(charges=Decimal(str(c))) for c in charges_sequence]
    ctx.get_pnl.side_effect = pnl_mocks
    ctx.clock.now.return_value = datetime(2025, 9, 9, 9, 30)
    ctx.get_spot_price.return_value = Decimal("23950")
    ctx.get_vix.return_value = 17.4
    s._context = ctx
    written: list = []
    s._decision_logger = MagicMock()
    s._decision_logger.log = lambda snap: written.append(snap)
    return s, written


def test_trade_id_minted_on_enter_and_propagated_to_exit():
    s, written = _strategy_for_lifecycle()
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2250.0)
    assert len(written) == 2
    enter, exit_ = written
    # ENTER row carries a non-empty UUID-derived id
    assert len(enter.trade_id) == 8
    # EXIT row carries the SAME id
    assert exit_.trade_id == enter.trade_id


def test_trade_id_cleared_after_exit_so_next_trade_gets_fresh_id():
    s, written = _strategy_for_lifecycle()
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2250.0)
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=28.0)
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=2100.0)
    ids = [r.trade_id for r in written]
    # First trade pair share an id; second pair share a DIFFERENT id
    assert ids[0] == ids[1]
    assert ids[2] == ids[3]
    assert ids[0] != ids[2]


def test_adjust_row_carries_active_trade_id():
    """An ADJUST emitted between ENTER and EXIT must keep the same id."""
    s, written = _strategy_for_lifecycle()
    s._log_decision("ENTER", leg="PREMIUM", mode="iron_condor", entry_premium=30.0)
    s._log_adjust_decision(
        leg="PREMIUM", mode="iron_condor",
        side_label="CE", side_realized_pnl=-150.0, new_entry_premium=28.0,
    )
    s._log_decision("EXIT", leg="PREMIUM", mode="iron_condor", outcome_pnl=1900.0)
    assert len(written) == 3
    enter, adjust, exit_ = written
    assert adjust.decision == "ADJUST"
    assert enter.trade_id == adjust.trade_id == exit_.trade_id
    # The ADJUST row records the side's realised P&L as outcome_pnl
    assert adjust.outcome_pnl == -150.0
    # Side label preserved into exit_reason for parseability
    assert "CE" in adjust.exit_reason


# ── regime.py: trade_id pairing sums ADJUST+EXIT into lifecycle PnL ──


def _decisions_df(rows: list[dict]) -> pd.DataFrame:
    """Build a small decision dataframe from dict rows."""
    return pd.DataFrame(rows)


def test_pair_enter_exit_sums_lifecycle_pnl_when_trade_ids_present():
    """For a trade with ENTER → ADJUST(-100) → ADJUST(-50) → EXIT(+800),
    the merged row should have outcome_pnl = -100 + -50 + 800 = +650."""
    from src.backtest.validation.regime import _pair_enter_exit
    df = _decisions_df([
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER",
         "trade_id": "abc12345", "outcome_pnl": None, "vix": 18.0, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ADJUST",
         "trade_id": "abc12345", "outcome_pnl": -100.0, "vix": 19.0, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ADJUST",
         "trade_id": "abc12345", "outcome_pnl": -50.0, "vix": 19.5, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT",
         "trade_id": "abc12345", "outcome_pnl": 800.0, "vix": 17.0, "regime": "high_vix"},
    ])
    paired = _pair_enter_exit(df)
    assert len(paired) == 1
    # outcome_pnl on the merged row = sum across the trade lifecycle
    assert paired.iloc[0]["outcome_pnl"] == 650.0
    # The merged row carries entry-time features (decision == ENTER source)
    assert paired.iloc[0]["decision"] == "ENTER"


def test_pair_enter_exit_separates_concurrent_trades_by_trade_id():
    """Two interleaved trades on the same strategy_id must not cross-pair."""
    from src.backtest.validation.regime import _pair_enter_exit
    df = _decisions_df([
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER",
         "trade_id": "trade_aa", "outcome_pnl": None, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER",
         "trade_id": "trade_bb", "outcome_pnl": None, "regime": "low_vix"},
        # trade_bb exits first
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT",
         "trade_id": "trade_bb", "outcome_pnl": 100.0, "regime": "low_vix"},
        # then trade_aa
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT",
         "trade_id": "trade_aa", "outcome_pnl": 500.0, "regime": "high_vix"},
    ])
    paired = _pair_enter_exit(df)
    assert len(paired) == 2
    # Each enter row got paired with ITS OWN exit
    by_id = {row["trade_id"]: row["outcome_pnl"] for _, row in paired.iterrows()}
    assert by_id["trade_aa"] == 500.0
    assert by_id["trade_bb"] == 100.0


def test_pair_enter_exit_falls_back_to_cumcount_when_no_trade_ids():
    """Legacy CSV without trade_id column → cumcount-based pairing.
    Verifies the back-compat path works when consumers feed pre-Phase-1C
    decision logs."""
    from src.backtest.validation.regime import _pair_enter_exit
    df = _decisions_df([
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER", "outcome_pnl": None, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER", "outcome_pnl": None, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT", "outcome_pnl": 200.0, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT", "outcome_pnl": 400.0, "regime": "high_vix"},
    ])
    paired = _pair_enter_exit(df)
    assert len(paired) == 2
    # Cumcount pairs ENTER#0 ↔ EXIT#0 and ENTER#1 ↔ EXIT#1
    pnls = sorted(paired["outcome_pnl"].tolist())
    assert pnls == [200.0, 400.0]


def test_pair_enter_exit_handles_empty_trade_id_strings():
    """If trade_id column exists but every value is empty (e.g. SKIP-only
    log rows), fall through to the cumcount path. Verifies the
    has-column-but-no-values edge case."""
    from src.backtest.validation.regime import _pair_enter_exit
    df = _decisions_df([
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "ENTER", "trade_id": "", "outcome_pnl": None, "regime": "high_vix"},
        {"strategy_id": "ic_1", "leg": "PREMIUM", "decision": "EXIT", "trade_id": "", "outcome_pnl": 300.0, "regime": "high_vix"},
    ])
    paired = _pair_enter_exit(df)
    assert len(paired) == 1
    assert paired.iloc[0]["outcome_pnl"] == 300.0
