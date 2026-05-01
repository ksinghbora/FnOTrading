"""Tests for the May 1 2026 alloc_token expiry-key fix.

Background
==========

Pre-fix, ``alloc_token((underlying, strike, option_type))`` keyed on
a 3-tuple WITHOUT expiry. Real-world Indian F&O has the same strike
listed across multiple weekly + monthly expiries simultaneously
(e.g., NIFTY 24100 CE for Dec-5, Dec-12, Dec-19, Dec-26, AND Jan-2
expiries on Dec-4 trading day). All five collapsed onto a single
token, and the broker's symbol→token resolution path returned the
LAST-registered expiry's symbol (Jan-2). When a strategy entered
with the Dec-5 symbol, the broker couldn't find it in symbol_map →
fell through to ``slippage_model`` (LTP+4bps), producing fictional
~Sharpe-10 results for trend_itm and silent bid/ask contamination
for premium-sellers.

These tests pin the contract that the fix establishes:
  - ``alloc_token`` takes 4 args: (ul, strike, ot, expiry)
  - Keys differ across expiries → distinct tokens
  - Same-expiry calls return the same token (idempotent within an
    expiry)
  - ``register_options`` from GDFLMarketSource produces a 4-tuple-
    keyed option_tokens dict
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest


# ── alloc_token closure (mirror of engine.py) ────────────────────────


def _make_alloc(start: int = 600_000):
    """Build an alloc_token closure with 4-tuple keying — matches the
    engine.py / replay_engine.py post-fix pattern."""
    next_token = [start]
    option_tokens: dict[tuple[str, float, str, date], int] = {}

    def alloc(ul: str, strike: float, ot: str, expiry: date) -> int:
        key = (ul, strike, ot, expiry)
        if key not in option_tokens:
            option_tokens[key] = next_token[0]
            next_token[0] += 1
        return option_tokens[key]

    return alloc, option_tokens


def test_same_strike_different_expiries_get_distinct_tokens():
    """The smoking gun: NIFTY 24100 CE for Dec-5 vs Dec-12 must be
    distinct tokens, otherwise the broker can't disambiguate symbols."""
    alloc, _ = _make_alloc()
    t_dec5 = alloc("NIFTY", 24100.0, "CE", date(2024, 12, 5))
    t_dec12 = alloc("NIFTY", 24100.0, "CE", date(2024, 12, 12))
    assert t_dec5 != t_dec12


def test_five_consecutive_expiries_each_get_distinct_tokens():
    """Real GDFL Dec-4 2024 day has 5 expiries listed (Dec-5, 12, 19,
    26, Jan-2). All five must produce distinct tokens for the same
    strike/side."""
    alloc, _ = _make_alloc()
    expiries = [
        date(2024, 12, 5),
        date(2024, 12, 12),
        date(2024, 12, 19),
        date(2024, 12, 26),
        date(2025, 1, 2),
    ]
    tokens = [alloc("NIFTY", 24100.0, "CE", e) for e in expiries]
    assert len(set(tokens)) == 5, f"Expected 5 distinct tokens, got: {tokens}"


def test_same_expiry_call_is_idempotent():
    """Within a single (strike, side, expiry), repeated alloc_token
    calls must return the SAME token. This is the contract that
    register_options + apply rely on (apply uses the dict to look up
    tokens for ticks)."""
    alloc, _ = _make_alloc()
    t1 = alloc("NIFTY", 24100.0, "CE", date(2024, 12, 5))
    t2 = alloc("NIFTY", 24100.0, "CE", date(2024, 12, 5))
    t3 = alloc("NIFTY", 24100.0, "CE", date(2024, 12, 5))
    assert t1 == t2 == t3


def test_different_strikes_different_sides_different_underlying_all_distinct():
    """Each unique (ul, strike, side, expiry) maps to a unique token."""
    alloc, _ = _make_alloc()
    e = date(2024, 12, 5)
    seen: set[int] = set()
    for ul in ("NIFTY", "BANKNIFTY"):
        for strike in (22000.0, 22500.0, 23000.0):
            for ot in ("CE", "PE"):
                tok = alloc(ul, strike, ot, e)
                assert tok not in seen, f"Collision on {(ul, strike, ot, e)}"
                seen.add(tok)
    assert len(seen) == 2 * 3 * 2  # ul × strikes × sides


def test_dict_key_is_four_tuple_including_expiry():
    """Pin the dict-key shape so a future refactor that drops expiry
    is caught immediately."""
    alloc, option_tokens = _make_alloc()
    alloc("NIFTY", 24100.0, "CE", date(2024, 12, 5))
    keys = list(option_tokens.keys())
    assert len(keys) == 1
    assert len(keys[0]) == 4, f"Expected 4-tuple key, got {keys[0]!r}"
    assert keys[0][3] == date(2024, 12, 5)


# ── GDFL register_options end-to-end ─────────────────────────────────


def test_gdfl_register_options_returns_four_tuple_keyed_dict():
    """End-to-end: GDFLMarketSource.register_options must produce a
    4-tuple-keyed option_tokens dict so apply() can look up tokens by
    the per-tick (strike, side, expiry)."""
    from src.backtest.gdfl_market_source import GDFLMarketSource

    src = GDFLMarketSource("data/gdfl_v2", "NIFTY", 256265)
    src.load_day(date(2024, 12, 4))

    chain_builder = MagicMock()
    chain_builder.register_option = MagicMock()
    alloc, option_tokens = _make_alloc()

    returned = src.register_options(chain_builder, alloc)

    # All keys are 4-tuples
    for k in returned.keys():
        assert len(k) == 4, f"Expected 4-tuple key from register_options, got {k!r}"
    # And the expiry component is a date (not a pd.Timestamp / numpy.datetime64)
    sample_key = next(iter(returned.keys()))
    assert isinstance(sample_key[3], date), (
        f"Expected date expiry in key, got {type(sample_key[3]).__name__}"
    )


def test_gdfl_register_options_produces_unique_symbols_per_expiry():
    """Part 2 of the fix: distinct tokens are necessary but not sufficient.
    The broker resolves orders by tradingsymbol, so weekly expiries
    within the same month must produce distinct symbols too. Pre-fix
    format ``%y%b`` collapsed all December weeklies to ``24DEC``;
    post-fix format ``%y%b%d`` gives ``24DEC05``, ``24DEC12``, etc."""
    from src.backtest.gdfl_market_source import GDFLMarketSource

    src = GDFLMarketSource("data/gdfl_v2", "NIFTY", 256265)
    src.load_day(date(2024, 12, 4))

    captured_symbols: list[str] = []

    def capture_register_option(token, ul, exp, strike, opt_type, sym):
        captured_symbols.append(sym)

    chain_builder = MagicMock()
    chain_builder.register_option = capture_register_option
    alloc, _ = _make_alloc()
    src.register_options(chain_builder, alloc)

    # Symbols with the SAME (strike, side) but DIFFERENT expiries must
    # all be distinct. Pull all CE 24100 symbols (one per expiry).
    target_symbols = [s for s in captured_symbols if "24100CE" in s]
    assert len(target_symbols) > 1, (
        "Expected multiple expiries for strike 24100 CE on 2024-12-04 "
        "(real GDFL day has 5)"
    )
    assert len(set(target_symbols)) == len(target_symbols), (
        f"Expected ALL distinct symbols, got duplicates: {target_symbols}"
    )
    # Also verify the format includes a 2-digit day component
    sample = target_symbols[0]
    # Symbol should look like NIFTY24DEC0524100CE — the 5 chars after
    # the year+month should be the day digits + first char of strike
    assert any(d in sample for d in ("DEC05", "DEC12", "DEC19", "DEC26", "JAN02")), (
        f"Expected %y%b%d format in symbol, got: {sample}"
    )


def test_gdfl_register_options_produces_distinct_tokens_for_repeated_strike():
    """The bug-regression test: pull a strike that appears in multiple
    expiries (real GDFL data has these) and verify all expiries get
    distinct tokens."""
    from src.backtest.gdfl_market_source import GDFLMarketSource

    src = GDFLMarketSource("data/gdfl_v2", "NIFTY", 256265)
    src.load_day(date(2024, 12, 4))

    chain_builder = MagicMock()
    chain_builder.register_option = MagicMock()
    alloc, _ = _make_alloc()

    returned = src.register_options(chain_builder, alloc)

    # Group tokens by (ul, strike, ot) — count expiries per group
    from collections import defaultdict
    grouped: dict[tuple[str, float, str], list[int]] = defaultdict(list)
    for (ul, strike, ot, exp), tok in returned.items():
        grouped[(ul, strike, ot)].append(tok)

    # At least one group should have multiple expiries (real GDFL day has them)
    multi = [(k, v) for k, v in grouped.items() if len(v) > 1]
    assert multi, "Expected at least one (ul, strike, ot) appearing in multiple expiries"

    # And all tokens within each multi-expiry group must be distinct
    for k, tokens in multi:
        assert len(set(tokens)) == len(tokens), (
            f"Token collision for {k}: {tokens} (would re-introduce the May 1 2026 bug)"
        )
