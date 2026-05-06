# Trend on NIFTY (futures-equivalent) — Verdict

**Status:** Signal has thin gross edge at 5-min timeframe; cost wall
eats net edge regardless. Trend-following primitive on NIFTY 1-min
spot does NOT clear the +0.5 Sharpe threshold required for tradeable
edge (per `PIVOT_DESIGN_trend_futures.md` decision criteria).

## TL;DR — timeframe sweep on 360-day post-SEBI corpus

| Timeframe | Trades | Trading days | Gross PnL | **Gross Sharpe** | Net PnL | Net Sharpe |
|---|---|---|---|---|---|---|
| 1-min  | 753 | 216 | -₹41,895 | **-0.52** | -₹312,171 | -3.84 |
| **5-min** | **246** | **159** | **+₹12,420** | **+0.17** | **-₹76,074** | **-1.01** |
| 15-min | 118 | 110 | +₹3,191  | +0.05 | -₹39,344 | -0.63 |
| 30-min | 94  | 90  | -₹61,335 | -0.96 | -₹95,122 | -1.49 |

5-min is the local optimum — both shorter (1-min noise dominates) and
longer (15-min and 30-min lose signal density) underperform. Even the
optimum at +0.17 gross Sharpe is far below the pivot doc's +0.5
threshold.

## Signal mechanic tested

Per `PIVOT_DESIGN_trend_futures.md` defaults:
- **Entry long:** close > 20-bar high of last 21 closes
- **Entry short:** symmetric
- **Filters:** VIX in [12, 22], ATR(14)/spot above floor, time in [09:30, 14:30]
- **Exit:** ATR(14) trailing stop at 2× ATR, hard time-stop at 14:45,
  reverse on opposite breakout

ATR floor was empirically recalibrated for 1-min bars
(0.05% vs the doc's 0.5% which fits daily bars). All other parameters
are literature defaults — no tuning.

## Cost model

- **1 basis point round-trip slippage** on entry+exit (futures-realistic)
- 75-share lot at NIFTY ~24000 = ~₹360 per trade in cost basis
- No STT/GST modelled (futures STT is unchanged post-SEBI; small
  relative to slippage at 1bp)

## Why this matters: cumulative options + futures verdict

Combining all post-SEBI strategy experiments to date:

| Strategy class | Best variant | Verdict |
|---|---|---|
| Premium-selling IC | IC v2 (CI+VRP gate) | **Marginal +PF 1.05 OOS** ✓ |
| Premium-selling defaults | IC, IB, SS, ST | All -3 to -8 Sharpe ✗ |
| Long-vol calendar | LC v2, LC v2b | At cost wall to catastrophic ✗ |
| Long-vol straddle | LS v2b | At cost wall ✗ |
| Trend on options | trend_itm v1 | At cost wall (May 1 finding) ✗ |
| Trend on futures-equivalent | this doc, 5-min | **Below cost wall** ✗ |

**IC v2 remains the only option-or-spot strategy with positive holdout
on the 173-day post-SEBI window.** Every other primitive — premium-
selling, long-vol, trend on options, trend on futures-equivalent —
sits at or below the cost wall.

## What this rules out

- **Naive Donchian breakout on intraday NIFTY** has insufficient edge
  to overcome ANY realistic cost basis (1bp on futures, 5-15bp on
  options). The signal is noise-dominated at the 1-min timeframe and
  too sparse at higher timeframes.
- The "trend-following is structurally inverse to premium-selling"
  pivot rationale (PIVOT_DESIGN_trend_futures.md) is correct in
  theory but doesn't survive empirical test. Indian post-SEBI 1-min
  spot is too efficient for naive momentum capture.

## What this does NOT rule out

- **Multi-day trend** (daily bars, hold 1-N days): different signal
  entirely, less microstructure noise. Worth testing as a separate
  experiment if appetite remains.
- **Multi-feature ML** (regime + volume + breadth + time-of-day +
  options skew): could potentially extract edge from the 5-min sweet
  spot's +0.17 Sharpe baseline. But ML on top of a thin baseline is
  prone to overfit.
- **Cross-instrument** (equity stat-arb on individual constituents):
  fundamentally different instrument class, different cost structure
  (no F&O lot constraint, lower STT, cheaper borrow for shorts via
  cash settlement).
- **Different alpha source** entirely (event-driven, term-structure
  arb, cash-futures basis trades).

## Files

- `scripts/smoke_trend_v1.py` — minimal pandas backtest harness
- `data/nifty_spot_minute.csv` — 360 days NIFTY spot 1-min bars
  (Kite-downloaded, gitignored due to size)
- `data/india_vix_minute.csv` — 360 days India VIX 1-min bars
- (this file) — trend-futures pivot verdict

## Recommendation

Three options ordered by directness:

### Option A — Accept IC v2 as the only edge; commit to live paper

The disciplined honest read: after exhausting premium-selling, long-
vol, and trend on both options + spot/futures-equivalent, IC v2 is
the only strategy with a positive holdout on the 173-day post-SEBI
window. ~₹4/day expected at 1 lot is small but real. Live paper
trading (already deployed PID 24377 with v2 config and warmup) is
the next legitimate test.

Estimated time: zero additional research; just monitor live paper for
1-3 months as planned.

### Option B — Multi-day trend on NIFTY daily bars

Different signal entirely. Daily bars eliminate intraday microstructure
noise. Trade horizons of 3-30 days. Reuses the same data already
downloaded (just resample to daily).

Estimated time: 1-2 days for smoke + decision.

### Option C — Pivot to equity stat-arb on NIFTY constituents

Different instrument class. Cash equity has different cost structure
(no F&O lot, cheaper STT, no expiry roll). Pair-trading or
cointegration on the 50 NIFTY constituents.

Estimated time: 3-4 weeks (data ingestion + strategy).

## Final word

The trend-futures pivot was theoretically motivated and tested
honestly. The verdict is that the naive Donchian primitive doesn't
have edge wide enough to bridge ANY transaction-cost wall on Indian
post-SEBI intraday data. The methodology continues to produce clean
verdicts; the empirical truth is that this market regime is harsher
on simple primitives than the textbooks assume.

The disciplined next step is **A — accept IC v2 as the marginal edge
we have and commit to live paper trading**. If after 1-3 months of
live data the strategy holds at PF ~1.05, we have a small structural
edge to scale modestly. If it underperforms, we run the numbers
honestly and reassess. Either way, the methodology toolkit (WF +
holdout discipline, theory-grounded gates, regime detectors,
cost-sensitivity gates) is the durable artifact from this arc.
