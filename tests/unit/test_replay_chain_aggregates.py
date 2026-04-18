"""Regression test for the chain-aggregate bug in src/backtest/replay_engine.py.

Apr 18 2026 audit found that `_apply_snapshot` was overwriting per-entry OI
with real recorded values but never recomputing chain.pcr_oi / total_*_oi /
max_pain. Strategies and decision logs read the stale aggregate from the
preceding _update_market call (BS-baseline → PCR=1.0 over a chain that also
held leftover real-OI residue from prior minutes) → 805/950 portfolio_replay
decisions came out with PCR > 1.5, which would have hard-blocked nearly
every entry if the PCR filter were wired in.

The fix in `_apply_snapshot` recomputes pcr_oi / pcr_volume / total_*_oi /
max_pain strictly over the strikes present in the *current* minute's
snap_data, ignoring any residue carried over from prior minute calls.

These tests pin that contract:
  1. PCR_OI after _apply_snapshot reflects ONLY the current snap_data,
     not whatever was painted by a prior _update_market call.
  2. After two consecutive _apply_snapshot calls with different snap_data,
     the chain.pcr_oi reflects only the second minute (no leakage from the
     first minute's strikes that aren't in the second snap).
  3. The recomputed PCR matches a hand-computed pe/ce ratio of the snap_data.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.broker.paper.client import PaperBrokerClient  # noqa: E402
from src.backtest.engine import (  # noqa: E402
    BacktestClock,
    _NUM_STRIKES,
    _register_options,
)
from src.backtest.replay_engine import _apply_snapshot  # noqa: E402
from src.core.events import EventBus  # noqa: E402
from src.market_data.feed import TickFeedManager  # noqa: E402
from src.market_data.option_chain import OptionChainBuilder  # noqa: E402
from src.portfolio.manager import PortfolioManager  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def _make_snap(spot: float, ce_oi_per_strike: int, pe_oi_per_strike: int,
               num_strikes: int = 21, step: int = 50) -> dict[tuple[float, str], dict]:
    """Build a synthetic snap_data dict simulating one minute of recorded chain.

    OI values are distinct per side so the test can detect whether the
    aggregate is computed over the snap (correct) vs over a polluted mix.
    """
    atm = round(spot / step) * step
    snap: dict[tuple[float, str], dict] = {}
    for i in range(-num_strikes // 2, num_strikes // 2 + 1):
        strike = float(atm + i * step)
        for opt, oi in (("CE", ce_oi_per_strike), ("PE", pe_oi_per_strike)):
            snap[(strike, opt)] = {
                "ltp": 100.0, "iv": 0.15,
                "delta": 0.5, "gamma": 0.001, "theta": -1.0, "vega": 5.0,
                "oi": oi, "volume": oi // 3,
                "bid_price": 99.0, "ask_price": 101.0,
            }
    return snap


async def _setup_engine(spot: float, day: date) -> dict:
    """Spin up the minimal replay infrastructure needed to call _apply_snapshot."""
    event_bus = EventBus()
    clock = BacktestClock()
    broker = PaperBrokerClient(initial_capital=1_000_000)
    feed = TickFeedManager(event_bus)
    cb = OptionChainBuilder(event_bus, clock)
    portfolio = PortfolioManager(event_bus, broker, cb)
    await broker.connect()

    underlying = "NIFTY"
    step = 50
    spot_token = 256265
    expiry = date(2026, 4, 21)
    cb.register_spot(spot_token, underlying)

    next_token = [9_000_000]
    option_tokens: dict[tuple[str, float, str], int] = {}

    def alloc(ul: str, s: float, ot: str) -> int:
        k = (ul, s, ot)
        if k not in option_tokens:
            option_tokens[k] = next_token[0]
            next_token[0] += 1
        return option_tokens[k]

    clock.set_time(IST.localize(datetime.combine(day, time(10, 0))))
    _register_options(cb, underlying, spot, step, _NUM_STRIKES, expiry, alloc)

    return {
        "feed": feed, "broker": broker, "cb": cb, "portfolio": portfolio,
        "underlying": underlying, "expiry": expiry, "step": step,
        "spot_token": spot_token, "option_tokens": option_tokens,
        "alloc": alloc, "clock": clock,
    }


def test_apply_snapshot_recomputes_pcr_from_current_snap():
    """PCR after _apply_snapshot must reflect the snap, not the BS baseline."""
    async def run():
        spot = 24300.0
        day = date(2026, 4, 17)
        env = await _setup_engine(spot, day)

        # Bullish chain: CE OI > PE OI → PCR = 0.5 exactly.
        # If the aggregate leaks BS baseline (CE==PE), we'd see PCR closer to 1.
        snap = _make_snap(spot, ce_oi_per_strike=200_000, pe_oi_per_strike=100_000)

        now = env["clock"].now()
        _apply_snapshot(
            env["feed"], env["broker"], env["cb"], env["portfolio"],
            env["underlying"], env["expiry"], spot, 14.5, now,
            env["spot_token"], env["step"], _NUM_STRIKES,
            T=4 / 365, iv_base=0.15,
            option_tokens=env["option_tokens"], alloc_token=env["alloc"],
            snap_data=snap, day_open=spot,
        )

        chain = env["cb"].get_chain(env["underlying"], env["expiry"])
        # Hand-computed: 21 strikes × 200_000 CE = 4.2M; × 100_000 PE = 2.1M;
        # PCR = 2.1M / 4.2M = 0.5
        assert chain.pcr_oi == pytest.approx(0.5, abs=0.01), \
            f"PCR should equal snap-implied 0.5, got {chain.pcr_oi}. " \
            f"This usually means _apply_snapshot is reading the BS-baseline " \
            f"aggregate (PCR≈1.0) instead of recomputing over the snap."

    asyncio.run(run())


def test_apply_snapshot_does_not_leak_prior_minutes_oi():
    """A second snap with a different strike range must not inherit the
    prior snap's OI in the recomputed aggregate.

    This is the bug we shipped Apr 18: chain.strikes accumulates across
    minutes (it's a long-lived object), so without a "fresh strikes only"
    filter the PCR_OI mixes minute N's CE OI with minute N-1's leftover PE
    OI from strikes that were dropped from the recorded set.
    """
    async def run():
        spot = 24300.0
        day = date(2026, 4, 17)
        env = await _setup_engine(spot, day)

        # Minute 1: PUT-heavy chain at 21 strikes around spot.
        snap1 = _make_snap(spot, ce_oi_per_strike=100_000, pe_oi_per_strike=500_000,
                           num_strikes=21)
        now = env["clock"].now()
        _apply_snapshot(
            env["feed"], env["broker"], env["cb"], env["portfolio"],
            env["underlying"], env["expiry"], spot, 14.5, now,
            env["spot_token"], env["step"], _NUM_STRIKES,
            T=4 / 365, iv_base=0.15,
            option_tokens=env["option_tokens"], alloc_token=env["alloc"],
            snap_data=snap1, day_open=spot,
        )
        chain = env["cb"].get_chain(env["underlying"], env["expiry"])
        assert chain.pcr_oi == pytest.approx(5.0, abs=0.01)  # 500k/100k

        # Minute 2: CALL-heavy chain at a DIFFERENT, smaller strike range.
        # If the fix is correct, the leftover OI from minute 1 strikes (which
        # are still in chain.strikes but not in snap2) is NOT counted.
        snap2 = _make_snap(spot, ce_oi_per_strike=400_000, pe_oi_per_strike=100_000,
                           num_strikes=11)
        env["clock"].set_time(now + (now - now))  # same minute, fine for the test
        _apply_snapshot(
            env["feed"], env["broker"], env["cb"], env["portfolio"],
            env["underlying"], env["expiry"], spot, 14.5, now,
            env["spot_token"], env["step"], _NUM_STRIKES,
            T=4 / 365, iv_base=0.15,
            option_tokens=env["option_tokens"], alloc_token=env["alloc"],
            snap_data=snap2, day_open=spot,
        )
        chain = env["cb"].get_chain(env["underlying"], env["expiry"])
        # snap2 implies PCR = 100k/400k = 0.25 over its 11 strikes.
        # If minute 1 leaked, we'd see something between 0.25 and 5.0.
        assert chain.pcr_oi == pytest.approx(0.25, abs=0.02), \
            f"PCR should reflect ONLY snap2 (0.25), got {chain.pcr_oi}. " \
            f"Leakage from snap1 strikes would push it higher."

    asyncio.run(run())


def test_apply_snapshot_max_pain_uses_only_current_snap():
    """max_pain after _apply_snapshot must be a strike present in the snap,
    not one of the leftover BS-baseline strikes outside the recorded range."""
    async def run():
        spot = 24300.0
        day = date(2026, 4, 17)
        env = await _setup_engine(spot, day)

        # Tight 11-strike snap centered at spot. _NUM_STRIKES=30 means BS
        # baseline registered ±30 strikes (61 entries). Without the fix,
        # max_pain could land on a BS-only strike outside the recorded range.
        snap = _make_snap(spot, ce_oi_per_strike=100_000, pe_oi_per_strike=100_000,
                          num_strikes=11)
        now = env["clock"].now()
        _apply_snapshot(
            env["feed"], env["broker"], env["cb"], env["portfolio"],
            env["underlying"], env["expiry"], spot, 14.5, now,
            env["spot_token"], env["step"], _NUM_STRIKES,
            T=4 / 365, iv_base=0.15,
            option_tokens=env["option_tokens"], alloc_token=env["alloc"],
            snap_data=snap, day_open=spot,
        )
        chain = env["cb"].get_chain(env["underlying"], env["expiry"])

        # Snap covers strikes in [atm-275, atm+275] = [24025, 24575] for atm=24300.
        # Max-pain MUST land inside that range.
        snap_strikes = sorted({float(s) for s, _ in snap.keys()})
        mp = float(chain.max_pain)
        assert mp in snap_strikes, \
            f"max_pain {mp} should be a snap-recorded strike. Snap strikes: " \
            f"[{snap_strikes[0]:.0f} … {snap_strikes[-1]:.0f}]. If it's outside, " \
            f"_apply_snapshot is computing max_pain over leftover BS-baseline " \
            f"strikes instead of the current snap."

    asyncio.run(run())
