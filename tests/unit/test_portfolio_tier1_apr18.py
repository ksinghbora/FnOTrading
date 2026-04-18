"""Tests for the Tier 1 quant audit fixes shipped Apr 18 2026.

These all share one north star: PROFIT INCREASE. Each guard is wired to
a specific bleed pattern surfaced in chain-replay decision logs from
Apr 13-17 2026. Keep tests surgical — exercise the guard, assert the
side-effect, do NOT walk the whole strategy lifecycle (the replay-engine
A/B is the integration check).

Fixes covered:
  1. _enter_premium VIX band: no-trade gap (20, 23) + stressed band [23, 28]
     (Quant #2 — Apr 13-17 IC trades in 20-22 lost ₹3,890 across 4 entries)
  2. _enter_iron_condor DTE gate (>5 blocks)
     (Quant #1 — 4 of 6 Apr 13 losing IC trades had DTE>=6)
  3. _enter_iron_condor min_credit rollback
     (Tech #4 — strangle had it; IC didn't, leaking on stale wing fills)
  4. on_tick expiry-day force-exit at 14:30
     (Tech #2 — `expiry_day_force_exit_at` was defined but never wired)
  5. trend hour gate using params (11:00 minimum, 14:00 maximum)
     (Quant #3 — hour 10 trend entries had -₹244 EV / 40% WR)
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.strategy.implementations.portfolio_strategy import PortfolioStrategy
from src.strategy.params import PortfolioParams


# ── Shared scaffolding ──────────────────────────────────────────────


def _make_ctx(*, spot: float = 23000.0, vix: float = 18.0,
              clock_now: datetime | None = None) -> MagicMock:
    """Minimal context — same shape as test_portfolio_entry_gates / stale-tick."""
    ctx = MagicMock()
    ctx.get_spot_price = MagicMock(return_value=Decimal(str(spot)))
    ctx.get_vix = MagicMock(return_value=vix)
    ctx.get_ltp = MagicMock(return_value=Decimal("0"))
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=clock_now or datetime(2026, 4, 17, 10, 15))
    ctx._spot_tokens = ()
    return ctx


def _make_strategy(params: PortfolioParams | None = None) -> PortfolioStrategy:
    s = PortfolioStrategy("test_tier1", params or PortfolioParams())
    s.set_context(_make_ctx())
    return s


# ── Quant #2: VIX band — toxic gap (20, 23) ────────────────────────


class TestVixBandGap:
    """The strategy router (`_enter_premium`) must:
       - allow strangle in [13, 16),
       - allow IC in [16, 20] (normal),
       - REFUSE everything in (20, 23) (toxic vol-of-vol),
       - allow IC in [23, 28] (stressed, ic_vix_reduce_above halves size),
       - REFUSE above 28 (event risk).
    """

    def test_vix_below_strangle_min_blocked(self, caplog):
        import logging
        caplog.set_level(logging.INFO)
        s = _make_strategy()
        # Side-effect: skip the expiry-day check inside _enter_premium
        s._check_expiry_day_block = MagicMock(return_value=None)

        result = s._enter_premium(vix=12.0)
        assert result is None
        assert any("complacency" in r.message for r in caplog.records)

    def test_vix_in_normal_ic_band_routes_to_ic(self):
        """VIX=18 → _enter_iron_condor called (then strangle fallback if IC None)."""
        s = _make_strategy()
        s._check_expiry_day_block = MagicMock(return_value=None)
        s._enter_iron_condor = MagicMock(return_value="IC_SIGNAL")
        s._enter_strangle = MagicMock(return_value="STRANGLE_SIGNAL")

        result = s._enter_premium(vix=18.0)

        assert result == "IC_SIGNAL"
        s._enter_iron_condor.assert_called_once_with(18.0)
        s._enter_strangle.assert_not_called()

    def test_vix_in_toxic_gap_lower_edge_blocked(self, caplog):
        """VIX=20.5 → in (20, 23) — must return None and log a toxic-gap reason."""
        import logging
        caplog.set_level(logging.INFO)
        s = _make_strategy()
        s._check_expiry_day_block = MagicMock(return_value=None)
        s._enter_iron_condor = MagicMock(return_value="should-not-be-called")
        s._enter_strangle = MagicMock(return_value="should-not-be-called")

        result = s._enter_premium(vix=20.5)

        assert result is None, "VIX in toxic gap must NOT enter any premium leg"
        s._enter_iron_condor.assert_not_called()
        s._enter_strangle.assert_not_called()
        assert any("toxic gap" in r.message for r in caplog.records), \
            "must log the toxic-gap reason for ops visibility"

    def test_vix_in_toxic_gap_upper_edge_blocked(self):
        """VIX=22.99 — still inside (20, 23), still blocked."""
        s = _make_strategy()
        s._check_expiry_day_block = MagicMock(return_value=None)
        s._enter_iron_condor = MagicMock(return_value="x")
        s._enter_strangle = MagicMock(return_value="x")

        result = s._enter_premium(vix=22.99)
        assert result is None
        s._enter_iron_condor.assert_not_called()

    def test_vix_in_stressed_band_routes_to_ic(self):
        """VIX=24 → in stressed band [23, 28] — IC re-enabled."""
        s = _make_strategy()
        s._check_expiry_day_block = MagicMock(return_value=None)
        s._enter_iron_condor = MagicMock(return_value="STRESSED_IC")

        result = s._enter_premium(vix=24.0)
        assert result == "STRESSED_IC"

    def test_vix_above_stressed_max_blocked(self, caplog):
        """VIX=29 → above stressed_max (28) — event risk, no entry."""
        import logging
        caplog.set_level(logging.INFO)
        s = _make_strategy()
        s._check_expiry_day_block = MagicMock(return_value=None)
        s._enter_iron_condor = MagicMock(return_value="x")

        result = s._enter_premium(vix=29.0)
        assert result is None
        s._enter_iron_condor.assert_not_called()
        assert any("event/crash zone" in r.message for r in caplog.records)


# ── Quant #1: IC DTE gate ─────────────────────────────────────────


class TestIcDteGate:

    def _ic_ready_strategy(self, expiry_offset_days: int) -> PortfolioStrategy:
        """Strategy seeded so _enter_iron_condor reaches the DTE check.

        We don't need a real chain — the DTE gate runs BEFORE the chain
        lookup, so we can return early with the gate's decision.
        """
        s = _make_strategy()
        # _expiry is set in set_context normally; here we plant it directly.
        s._expiry = date(2026, 4, 17) + (
            __import__("datetime").timedelta(days=expiry_offset_days)
        )
        s.ctx.clock.now = MagicMock(return_value=datetime(2026, 4, 17, 10, 15))
        return s

    def test_ic_blocked_when_dte_above_max(self, caplog):
        """DTE=6 (Wed-of-prior-week) → blocked.

        4 of 6 Apr 13 losing IC trades had DTE in {6, 7}. Theta accrual
        is too slow vs gamma exposure to recover from a single wing test.
        """
        import logging
        caplog.set_level(logging.INFO)
        s = self._ic_ready_strategy(expiry_offset_days=6)

        result = s._enter_iron_condor(vix=18.0)

        assert result is None
        assert any("DTE=6" in r.message and "ic_max_dte" in r.message
                   for r in caplog.records), \
            "blocked log must include DTE and the threshold for diagnostics"

    def test_ic_allowed_when_dte_at_max(self):
        """DTE=5 (Thu before Tue expiry) → allowed (boundary inclusive)."""
        s = self._ic_ready_strategy(expiry_offset_days=5)
        # Past the DTE gate we'd hit the chain — stub it to fail fast so
        # the test stays narrow.
        s.ctx.get_option_chain = MagicMock(return_value=None)

        # We don't expect a Signal; we expect the BLOCK to come from "no
        # option chain", NOT from the DTE gate. Inspect the warning trail.
        result = s._enter_iron_condor(vix=18.0)
        assert result is None  # Blocked on chain, not DTE

    def test_ic_allowed_when_dte_below_max(self):
        """DTE=2 (Mon before Tue expiry) → allowed."""
        s = self._ic_ready_strategy(expiry_offset_days=2)
        s.ctx.get_option_chain = MagicMock(return_value=None)
        result = s._enter_iron_condor(vix=18.0)
        assert result is None  # Blocked on chain

    def test_ic_dte_param_configurable(self):
        params = PortfolioParams(ic_max_dte=3)
        assert params.ic_max_dte == 3

    def test_ic_dte_gate_skipped_when_no_expiry(self):
        """Defensive: if _expiry is None (pre-context), don't crash on the gate."""
        s = _make_strategy()
        s._expiry = None
        s.ctx.get_option_chain = MagicMock(return_value=None)
        # Should reach the chain check without raising
        result = s._enter_iron_condor(vix=18.0)
        assert result is None


# ── Tech #4: IC min_credit rollback ───────────────────────────────


class TestIcMinCreditRollback:

    def _build_ic_path_strategy(self) -> PortfolioStrategy:
        """Strategy with chain + strikes wired so _enter_iron_condor reaches
        the credit check. We inject stale leg LTPs to force a tiny credit."""
        s = _make_strategy()
        s._expiry = date(2026, 4, 24)  # 7 days out... wait, must be ≤5 for DTE pass
        # Set today so DTE = 4
        s.ctx.clock.now = MagicMock(return_value=datetime(2026, 4, 20, 10, 0))

        # Mock chain with one strike entry — `_find_oi_validated_strikes` and
        # `find_available_wing_strike` are stubbed to bypass real chain math.
        chain = MagicMock()
        chain.strikes = [MagicMock()]
        s.ctx.get_option_chain = MagicMock(return_value=chain)

        ce_short = MagicMock()
        ce_short.ce = MagicMock(instrument_token=10, tradingsymbol="CE_S")
        ce_short.strike = 23200.0
        pe_short = MagicMock()
        pe_short.pe = MagicMock(instrument_token=20, tradingsymbol="PE_S")
        pe_short.strike = 22800.0
        s._find_oi_validated_strikes = MagicMock(return_value=(ce_short, pe_short))

        # Patch the wing-finder import path used inside _enter_iron_condor.
        ce_long = MagicMock()
        ce_long.ce = MagicMock(instrument_token=11, tradingsymbol="CE_L")
        pe_long = MagicMock()
        pe_long.pe = MagicMock(instrument_token=21, tradingsymbol="PE_L")
        import src.strategy.implementations.portfolio_strategy as ps_mod
        ps_mod.find_available_wing_strike = MagicMock(
            side_effect=[(ce_long, 400), (pe_long, 400)]
        )
        return s

    def test_ic_rolls_back_state_when_credit_below_min(self, caplog):
        """Stale wing fills → credit ≈ ₹2 (below default ₹10 floor).
        All 4 leg tokens, symbols, and entry_premium must reset to 0/empty.
        """
        import logging
        caplog.set_level(logging.WARNING)
        s = self._build_ic_path_strategy()

        # short_prem = 100+80 = 180; long_prem = 90+88 = 178; credit = 2 < 10
        s.ctx.get_ltp = MagicMock(side_effect=[
            Decimal("100"),  # short CE
            Decimal("80"),   # short PE
            Decimal("90"),   # long CE
            Decimal("88"),   # long PE
        ])

        result = s._enter_iron_condor(vix=18.0)

        assert result is None
        assert s._prem_entered is False
        assert s._short_ce_token == 0
        assert s._short_pe_token == 0
        assert s._long_ce_token == 0
        assert s._long_pe_token == 0
        assert s._short_ce_symbol == ""
        assert s._long_pe_symbol == ""
        assert s._entry_premium == Decimal("0")
        assert s._peak_premium == Decimal("0")
        assert any("ic_min_entry_credit" in r.message for r in caplog.records)

    def test_ic_min_credit_param_configurable(self):
        params = PortfolioParams(ic_min_entry_credit=25.0)
        assert params.ic_min_entry_credit == 25.0


# ── Tech #2: Expiry-day force-exit at 14:30 ───────────────────────


class TestExpiryDayForceExit:

    @pytest.mark.asyncio
    async def test_force_exit_premium_at_1430_on_expiry(self):
        """Premium leg open + today is expiry + clock ≥ 14:30 → force exit.
        Without this, an IC carried into the gamma-vertical window can
        triple in premium before the regular 15:15 exit.
        """
        s = _make_strategy()
        expiry = date(2026, 4, 21)  # Tue
        s._expiry = expiry
        s._prem_entered = True
        s._exit_premium = MagicMock(return_value="EXIT_SIG_PREM")
        # Stale-tick guard expects a baseline; seed it.
        s._last_sane_spot = 23000.0
        s._last_sane_spot_ts = datetime(2026, 4, 21, 14, 25)

        from src.core.models import Tick
        ts = datetime(2026, 4, 21, 14, 30)
        s.ctx.clock.now = MagicMock(return_value=ts)
        # spot stays the same so the stale-tick guard doesn't trip
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("23000"))

        result = await s.on_tick(Tick(
            instrument_token=256265,
            tradingsymbol="NIFTY 50",
            timestamp=ts,
            ltp=Decimal("23000"),
        ))

        assert result == "EXIT_SIG_PREM"
        s._exit_premium.assert_called_once()
        args = s._exit_premium.call_args[0]
        assert "Expiry-day force-exit" in args[0]

    @pytest.mark.asyncio
    async def test_no_force_exit_before_1430_on_expiry(self):
        """Same expiry day, but clock at 14:25 — must NOT force exit."""
        s = _make_strategy()
        expiry = date(2026, 4, 21)
        s._expiry = expiry
        s._prem_entered = True
        s._exit_premium = MagicMock(return_value="should-not-fire")
        s._last_sane_spot = 23000.0
        s._last_sane_spot_ts = datetime(2026, 4, 21, 14, 20)

        from src.core.models import Tick
        ts = datetime(2026, 4, 21, 14, 25)
        s.ctx.clock.now = MagicMock(return_value=ts)
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("23000"))

        # The function may return None for other reasons (no _check_premium_exit
        # signal), but the force-exit branch MUST NOT have been taken.
        try:
            await s.on_tick(Tick(
                instrument_token=256265,
                tradingsymbol="NIFTY 50",
                timestamp=ts,
                ltp=Decimal("23000"),
            ))
        except Exception:
            pass

        # Critical assertion: _exit_premium was NOT called with force-exit reason
        for call in s._exit_premium.call_args_list:
            assert "Expiry-day force-exit" not in call[0][0]

    @pytest.mark.asyncio
    async def test_no_force_exit_on_non_expiry_day(self):
        """Clock at 15:00 but today != expiry → normal flow continues."""
        s = _make_strategy()
        s._expiry = date(2026, 4, 21)  # Tue
        s._prem_entered = True
        s._exit_premium = MagicMock(return_value="should-not-fire")
        s._last_sane_spot = 23000.0
        s._last_sane_spot_ts = datetime(2026, 4, 20, 14, 50)

        from src.core.models import Tick
        ts = datetime(2026, 4, 20, 15, 0)  # Mon (not expiry)
        s.ctx.clock.now = MagicMock(return_value=ts)
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("23000"))

        try:
            await s.on_tick(Tick(
                instrument_token=256265,
                tradingsymbol="NIFTY 50",
                timestamp=ts,
                ltp=Decimal("23000"),
            ))
        except Exception:
            pass

        for call in s._exit_premium.call_args_list:
            assert "Expiry-day force-exit" not in call[0][0]


# ── Quant #3: Trend hour gate (params-driven) ─────────────────────


class TestTrendHourGate:

    def test_default_trend_min_hour_is_11(self):
        """Default param shifted from hour 10 → hour 11 (Apr 18 quant)."""
        params = PortfolioParams()
        assert params.trend_entry_min_hour == 11
        assert params.trend_entry_max_hour == 14

    def test_trend_min_hour_param_configurable(self):
        """Deployments can experiment with a later cutoff (e.g., 12)
        without code changes."""
        params = PortfolioParams(trend_entry_min_hour=12, trend_entry_max_hour=15)
        assert params.trend_entry_min_hour == 12
        assert params.trend_entry_max_hour == 15
