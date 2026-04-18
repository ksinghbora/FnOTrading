"""Tests for the price-resolution and wing-clamping helpers.

These two helpers were extracted to fix two execution bugs surfaced in the
23-day chain replay (Apr 2026):

  1. resolve_option_price: ITM trend-leg LTP often comes back as 0 in the
     recorded chain when no trade printed in that minute, blocking entry
     with "debit non-positive". Mid-quote fallback is the realistic fill.

  2. find_available_wing_strike: 8-strike wings (400pts on NIFTY) often
     land outside the recorded chain's strike range, blocking IC entry.
     Walking inward to the nearest available strike preserves defined risk.

The helpers are pure functions of duck-typed inputs, so tests use lightweight
dataclasses instead of full OptionData/OptionChain objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.strategy.implementations.portfolio_pricing import (
    find_available_wing_strike,
    resolve_option_price,
)


# ── Test doubles ────────────────────────────────────────────────────


@dataclass
class _Opt:
    """Duck-typed OptionData stand-in."""
    ltp: float = 0.0
    bid_price: float = 0.0
    ask_price: float = 0.0


@dataclass
class _StrikeEntry:
    strike: float
    ce: _Opt | None = None
    pe: _Opt | None = None


@dataclass
class _Chain:
    strikes: list[_StrikeEntry] = field(default_factory=list)


# ── resolve_option_price ────────────────────────────────────────────


class TestResolveOptionPrice:
    def test_returns_none_for_none_input(self):
        assert resolve_option_price(None, "BUY") is None
        assert resolve_option_price(None, "SELL") is None

    def test_uses_ltp_when_positive(self):
        opt = _Opt(ltp=42.5, bid_price=40.0, ask_price=45.0)
        # LTP wins even when mid is available — LTP is a real transaction
        assert resolve_option_price(opt, "BUY") == Decimal("42.5")
        assert resolve_option_price(opt, "SELL") == Decimal("42.5")

    def test_falls_back_to_mid_when_ltp_zero(self):
        # The exact bug: ITM leg shows ltp=0 in recorded chain
        opt = _Opt(ltp=0.0, bid_price=100.0, ask_price=110.0)
        assert resolve_option_price(opt, "BUY") == Decimal("105")
        assert resolve_option_price(opt, "SELL") == Decimal("105")

    def test_mid_requires_bid_lt_ask(self):
        # Crossed/locked book (bid >= ask) is junk data → fall through
        opt = _Opt(ltp=0.0, bid_price=110.0, ask_price=100.0)
        # ask > 0 so BUY-side falls through to ask
        assert resolve_option_price(opt, "BUY") == Decimal("100")
        assert resolve_option_price(opt, "SELL") == Decimal("110")

    def test_buy_falls_back_to_ask_when_no_bid(self):
        opt = _Opt(ltp=0.0, bid_price=0.0, ask_price=50.0)
        assert resolve_option_price(opt, "BUY") == Decimal("50")
        # SELL has nothing to hit → None
        assert resolve_option_price(opt, "SELL") is None

    def test_sell_falls_back_to_bid_when_no_ask(self):
        opt = _Opt(ltp=0.0, bid_price=48.0, ask_price=0.0)
        assert resolve_option_price(opt, "SELL") == Decimal("48")
        # BUY has nothing to lift → None
        assert resolve_option_price(opt, "BUY") is None

    def test_returns_none_when_no_pricing_signal(self):
        opt = _Opt(ltp=0.0, bid_price=0.0, ask_price=0.0)
        assert resolve_option_price(opt, "BUY") is None
        assert resolve_option_price(opt, "SELL") is None

    def test_handles_none_ltp_attribute(self):
        # Some chain payloads carry None instead of 0 for missing fields
        opt = _Opt(ltp=None, bid_price=10.0, ask_price=12.0)  # type: ignore[arg-type]
        assert resolve_option_price(opt, "BUY") == Decimal("11")

    def test_decimal_string_round_trip_avoids_float_drift(self):
        # 0.1 + 0.2 in float is 0.30000000000000004; via Decimal(str(...)) it's exact
        opt = _Opt(ltp=0.0, bid_price=0.1, ask_price=0.3)
        result = resolve_option_price(opt, "BUY")
        assert result == Decimal("0.2")


# ── find_available_wing_strike ──────────────────────────────────────


def _make_chain(strikes_with_legs: list[tuple[float, bool, bool]]) -> _Chain:
    """Helper: build a chain from (strike, has_ce, has_pe) tuples.

    Legs default to ltp=10 (priceable). Pass ltp=0 separately if needed.
    """
    entries = []
    for strike, has_ce, has_pe in strikes_with_legs:
        entries.append(_StrikeEntry(
            strike=strike,
            ce=_Opt(ltp=10.0) if has_ce else None,
            pe=_Opt(ltp=10.0) if has_pe else None,
        ))
    return _Chain(strikes=entries)


class TestFindAvailableWingStrike:
    def test_returns_desired_when_strike_present(self):
        # Chain has strikes 21000, 21050, ..., 21800
        chain = _make_chain([(21000 + 50 * i, True, True) for i in range(17)])
        # Short CE at 21400, want wing 400pts above (21800)
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        assert entry is not None and entry.strike == 21800
        assert offset == 400

    def test_clamps_inward_when_desired_strike_missing(self):
        # The IC bug: desired wing is outside chain range
        # Chain only goes 21000..21600 (max strike 21600)
        chain = _make_chain([(21000 + 50 * i, True, True) for i in range(13)])
        # Short CE at 21400, want wing at 21800 (not in chain)
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        # Should clamp to 21600 (200pts wing)
        assert entry is not None and entry.strike == 21600
        assert offset == 200

    def test_clamps_inward_for_pe_wing(self):
        # The exact bug from the replay: long_pe@20450 found=False
        # Chain starts at 20800 — short PE at 20850, want wing 400pts below (20450)
        chain = _make_chain([(20800 + 50 * i, True, True) for i in range(20)])
        entry, offset = find_available_wing_strike(
            chain, base_strike=20850, desired_offset_pts=400, direction=-1, opt_attr="pe"
        )
        # 20450 missing, 20500 missing, 20550 missing, 20600 missing,
        # 20650 missing, 20700 missing, 20750 missing, 20800 present (50pts wing)
        assert entry is not None and entry.strike == 20800
        assert offset == 50

    def test_returns_none_when_no_strike_in_window(self):
        # Chain has only base strike — no room for any wing
        chain = _make_chain([(21400, True, True)])
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        assert entry is None and offset == 0

    def test_skips_strikes_with_missing_leg(self):
        # 21800 has CE but 21750 only has PE — should pick 21800
        chain = _Chain(strikes=[
            _StrikeEntry(21400, ce=_Opt(ltp=50.0), pe=_Opt(ltp=50.0)),
            _StrikeEntry(21500, ce=_Opt(ltp=10.0), pe=_Opt(ltp=10.0)),
            _StrikeEntry(21600, ce=_Opt(ltp=5.0), pe=_Opt(ltp=5.0)),
            _StrikeEntry(21700, ce=_Opt(ltp=2.0), pe=_Opt(ltp=2.0)),
            _StrikeEntry(21750, ce=None, pe=_Opt(ltp=1.0)),  # CE missing
            _StrikeEntry(21800, ce=_Opt(ltp=1.0), pe=_Opt(ltp=1.0)),
        ])
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        assert entry is not None and entry.strike == 21800
        assert offset == 400

    def test_skips_strikes_with_no_pricing_signal(self):
        # 21800 CE has ltp=0 and no quotes → skip and try 21750
        chain = _Chain(strikes=[
            _StrikeEntry(21400, ce=_Opt(ltp=50.0)),
            _StrikeEntry(21750, ce=_Opt(ltp=2.0)),
            _StrikeEntry(21800, ce=_Opt(ltp=0.0, bid_price=0.0, ask_price=0.0)),
        ])
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        assert entry is not None and entry.strike == 21750
        assert offset == 350

    def test_accepts_strike_with_quote_only_no_ltp(self):
        # ltp=0 but bid+ask present is acceptable (real quote signal)
        chain = _Chain(strikes=[
            _StrikeEntry(21400, ce=_Opt(ltp=50.0)),
            _StrikeEntry(21800, ce=_Opt(ltp=0.0, bid_price=1.5, ask_price=2.5)),
        ])
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=400, direction=+1, opt_attr="ce"
        )
        assert entry is not None and entry.strike == 21800
        assert offset == 400

    def test_rejects_offset_below_strike_step(self):
        # Asking for a 25pt wing on a 50pt-step chain is a degenerate call
        chain = _make_chain([(21400, True, True), (21450, True, True)])
        entry, offset = find_available_wing_strike(
            chain, base_strike=21400, desired_offset_pts=25, direction=+1, opt_attr="ce", strike_step=50
        )
        assert entry is None and offset == 0

    def test_banknifty_100pt_step(self):
        # BANKNIFTY uses 100pt strike step
        chain = _make_chain([(50000 + 100 * i, True, True) for i in range(10)])
        entry, offset = find_available_wing_strike(
            chain, base_strike=50500, desired_offset_pts=400, direction=+1,
            opt_attr="ce", strike_step=100,
        )
        assert entry is not None and entry.strike == 50900
        assert offset == 400

    def test_banknifty_clamps_in_100pt_steps(self):
        # BANKNIFTY chain ends at 50700; short at 50500, want 400pt wing → 50900
        # Should clamp: 50900 missing → 50800 missing → 50700 present (200pt wing)
        chain = _make_chain([(50000 + 100 * i, True, True) for i in range(8)])
        entry, offset = find_available_wing_strike(
            chain, base_strike=50500, desired_offset_pts=400, direction=+1,
            opt_attr="ce", strike_step=100,
        )
        assert entry is not None and entry.strike == 50700
        assert offset == 200
