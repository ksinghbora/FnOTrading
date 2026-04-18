"""Tests for the structural bug fixes that survived the Apr 18 partial revert.

The original Apr 18 Tier 1 commit (de5853a) bundled 7 changes. A 23-day
chain-replay A/B showed the bundle made P&L *worse* (-₹3,108.77 vs the
parent's -₹512.08), so the statistical pieces (VIX gap, IC DTE gate,
PT/SL re-tunes, trend hour gate) were reverted. Only the two pure
structural fixes were kept — those don't depend on any statistical claim
and patch real plumbing bugs that would silently corrupt state:

  1. IC min_credit rollback — when entry credit is below the floor, we
     now reset all 4 leg tokens/symbols/premium so the next entry isn't
     polluted by half-set state. Strangle had this; IC didn't.
     (Mirrors strangle's premium_min_entry_credit guard.)

  2. on_tick expiry-day force-exit at 14:30 — `expiry_day_force_exit_at`
     was defined on BaseStrategyParams but never wired. Anything carried
     into expiry day ran into the regular 15:15 exit, eating the gamma-
     vertical window. Hard exit at 14:30 avoids 0DTE STT and gamma
     blow-ups.

Trend fill-pending reconciliation is also part of the partial revert
keep-set, but it's covered by the existing premium-leg fill_pending
tests (same pattern, mirror code path) — no new test needed.
"""

from __future__ import annotations

from datetime import date, datetime
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


# ── Structural #1: IC min_credit rollback ─────────────────────────


class TestIcMinCreditRollback:
    """Below-floor IC credit must clear ALL 4 leg slots, not just refuse entry.

    Bug: prior implementation just `return None`d on credit < 10, leaving
    `_short_ce_token` / `_short_pe_token` / wing tokens / symbols populated
    from the failed entry. Subsequent entries skipped overwrite in some
    paths and ran with the stale tokens.
    """

    def _build_ic_path_strategy(self) -> PortfolioStrategy:
        """Strategy with chain + strikes wired so _enter_iron_condor reaches
        the credit check. We inject stale leg LTPs to force a tiny credit."""
        s = _make_strategy()
        s._expiry = date(2026, 4, 24)
        s.ctx.clock.now = MagicMock(return_value=datetime(2026, 4, 20, 10, 0))

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


# ── Structural #2: Expiry-day force-exit at 14:30 ──────────────────


class TestExpiryDayForceExit:
    """`expiry_day_force_exit_at` was defined but unwired before this fix.

    Without it, a position carried into expiry day kept running until the
    15:15 normal exit_time, eating the 14:30→15:15 gamma-vertical window.
    """

    @pytest.mark.asyncio
    async def test_force_exit_premium_at_1430_on_expiry(self):
        """Premium leg open + today is expiry + clock ≥ 14:30 → force exit."""
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
