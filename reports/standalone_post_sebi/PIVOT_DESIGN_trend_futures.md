# Pivot Design — Trend-Following on NIFTY Index Futures

**Status:** Design proposal, May 1 2026.

**Why this pivot:** the cross-strategy validation
([SUMMARY.md](./SUMMARY.md)) shows premium-selling on Indian retail
F&O has no edge post-SEBI. Trend-following is **structurally inverse**
to what failed: it profits on the breakouts that destroyed our IC, and
on the trending regimes where premium-sellers bled most.

This document is the *minimum-viable design*. It deliberately reuses
existing infrastructure (BaseStrategy, RegimeDetector, BacktestEngine,
validate_strategy harness) so we can validate in 1-2 weeks, not
re-architect from scratch.

## What we're trading

**Instrument:** NIFTY index futures (current-month, rollover at T-3 days
to expiry). NOT options. NOT a basket.

**Why futures over options for trend-following:**
- One position per signal vs 4 legs per IC entry
- ~1 bp slippage round-trip vs ~5-15 bp on options legs
- No theta decay (the silent killer of long-vol strategies)
- No SEBI Nov-2024 cost wall (STT-on-sell doubling was specifically on
  options sells; futures STT structure is unchanged)
- No 4-leg synchronisation risk (each option leg can fill at a
  different price; futures are atomic)

**Why current-month futures:**
- Tightest liquidity / spreads in the chain
- Natural rollover discipline (no perpetual position)
- Matches the GDFL parquet's existing futures coverage

## Signal — donchian channel break with vol filter

This is the textbook trend-following primitive, parameter-tunable but
deliberately simple at v1. Reasonable starting heuristic; the validation
harness will tell us if it has edge before we tune.

**Entry long:**
- Close > 20-bar high of last 21 closes, AND
- VIX between 12 and 22 (avoid extreme complacency *and* extreme stress
  — both regimes mean-revert), AND
- ATR(14) > 0.5% of spot (require minimum tradeable range), AND
- Time between 09:30 and 14:30 IST (avoid open-auction noise + close
  squaring-off)

**Entry short:** symmetric, on close < 20-bar low.

**Exit:**
- ATR(14) trailing stop at 2× ATR from peak favorable price
- Hard time stop at 14:45 IST (square off intraday — no overnight gap risk
  for v1)
- Reverse on opposite breakout (rare with the time-stop, but kept for
  multi-day extension if we lift the time-stop in v2)

**Position sizing:** fixed lot count (start at 1 lot = 75 shares = ~₹17
lakh notional at NIFTY 22500). Sizing-by-volatility comes in v2 once we
know the strategy has edge.

## Why this signal might have edge (and the honest counter)

The signal works if NIFTY exhibits **momentum continuation** at the
20-bar (~1-day intraday) timeframe. Empirical evidence from US/EM
indices says yes — the price-trend factor (e.g., AQR / Asness) has
~0.5-0.8 Sharpe pre-cost across decades.

The honest counter: the 20-bar Donchian primitive has been picked over
since the 1980s. Any edge it had on liquid index futures is competed
down. Our v1 might land at Sharpe 0.2-0.5 pre-cost, which becomes
−0.3 to +0.2 net of costs. That's not a profit; it's a coin flip.

**This is exactly what we need to find out.** The cost validation here
should be much simpler than for options (slippage modelling on a single
instrument is cleaner than 4-leg cross-spread crossing), so the answer
should be clear in one CPCV run.

## Implementation plan (~1-2 weeks)

### Day 1-2: New strategy module

`src/strategy/implementations/trend_futures.py` — subclass of
`BaseStrategy`. Reuse:
- `_bid_ask_for(token)` for realistic-fill on entry/exit
- `_check_vix_filter()` for VIX-band gate (already fail-closed)
- `_log_decision` / `_log_skip_throttled` for entry-skip throttling
- `_charges_at_entry` / `_current_trade_id` for charge attribution

New code:
- `TrendFuturesParams` (donchian_lookback, atr_period, atr_stop_mult,
  vix_min, vix_max, time_entry_start, time_entry_end, time_exit_hard)
- `_compute_donchian()` over the rolling 21-bar OHLC window
- `_compute_atr()` (Wilder's smoothing)
- `on_tick`: maintain a 1-min OHLC ring buffer (~21 bars = 21 min of
  history; cheap)
- `_try_entry`: gate (VIX, time, ATR floor) → check breakout → entry
  signal
- `_check_exit_conditions`: trailing-stop + hard-time-stop + reverse-on-
  opposite-break

### Day 3: Engine plumbing for futures

The current `BacktestEngine` is options-centric. Two adjustments:
- `chain_builder` already maintains futures rows in the GDFL parquet;
  needs an `instrument_type=FUT` filter path
- Quote provider / depth provider already work on tradingsymbols; needs
  the futures symbol resolved
- Slippage model (1 bp on futures vs the option-specific bps model) —
  add a futures branch in `src/broker/paper/slippage.py`

### Day 4: Unit tests

Mirror the structure of `test_realistic_fill_pt_sl.py` and
`test_iron_condor_liquidity_filter.py`:
- Entry-gate behaviour (VIX, time, ATR) under all branches
- Donchian computation correctness (off-by-one is the classic bug)
- ATR computation matches Wilder's reference numbers
- Trailing stop arithmetic
- Reverse-on-opposite-break path

Target: 30+ tests, all green, before any backtest.

### Day 5-7: Validation on post-SEBI corpus

Same harness as IC:
```
.venv/bin/python scripts/validate_strategy.py \
  --strategy trend_futures \
  --train-end 2025-05-30 --val-end 2025-07-31 --holdout-end 2026-02-27 \
  --parquet-dir data/gdfl_v2 --underlying NIFTY \
  --corpus-from 2024-11-20 \
  --workers 4 --cpcv-folds 10 --cpcv-max-paths 50 \
  --wf-train-days 90 --wf-test-days 30 --wf-step-days 15 \
  --out reports/standalone_post_sebi/trend_futures_validation.md
```

Same gates apply: CPCV median Sharpe, MC permutation, cost sensitivity,
quote-quality probe.

### Day 8-14: Decide

Three outcomes:
- **Sharpe > 0.5 OOS:** real edge, move to live paper trading
- **Sharpe in [0, 0.5]:** marginal — try v2 (vol-targeting position
  sizing, multi-timeframe filter, ML-based regime hint)
- **Sharpe < 0:** trend-following primitive has no edge here either —
  pivot to equity stat-arb (next document).

## What v1 deliberately *does not* do

- No AI/ML signal. The point is to validate the simplest tractable
  trend primitive cleanly. ML on top of a non-existent edge produces
  overfit nonsense.
- No multi-timeframe confirmation. One signal source = one set of
  hyperparameters to validate.
- No portfolio overlay. Single instrument, single signal.
- No options hedging. We're trading the underlying futures only.
- No overnight positions. Hard time-stop intraday. Removes gap risk
  from the v1 evaluation, can lift in v2.

## What success looks like

The decision criterion for "v1 trend_futures has edge" is the same as
the IC validation:
- Median CPCV Sharpe ≥ 0.3 on `train_in_sample` mode (10-fold, 50p)
- Median CPCV Sharpe ≥ 0.1 on `test_oos` mode
- MC skill p-value ≤ 0.1
- Bootstrap-CI Sharpe lower bound > −0.1
- Cost sensitivity: Sharpe ≥ 0 at +0.5 shift (tighter than IC failed at)

If all five pass, we have a tradeable edge. If any fail, the design is
falsified and we move to the next pivot.

## Out of scope for this document

- Equity stat-arb (separate design doc — only if trend-futures fails)
- Long-vol vol-targeting (separate design — orthogonal pivot direction)
- Live paper trading wiring (separate doc — only after validation
  passes)
