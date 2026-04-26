# Phase 3-Pre Fee/Slippage Truth-Up — Verdict Report

- Generated: 2026-04-25T23:30:45
- Window: 2026-04-20 → 2026-04-24
- Source: 17 days of overlapping chain_snapshots + paper-trade decisions
- Strategy IDs observed: ['ic_1', 'portfolio_1', 'straddle_1', 'strangle_1']

## VERDICT

**INSUFFICIENT_DATA**

This is not a green light to proceed. It is the harness saying 'this sample cannot answer the cost-edge question.'

**Per PHASE3_MASTER §V.5 verdict vocabulary, INSUFFICIENT_DATA means Phase 3a does not start. The operator must obtain a larger, lower-correlation sample before re-running the truth-up.**

Reasons:
- effective N = 3.2 (raw trades = 13; 4 strategies firing on overlapping minutes reduce independent samples) < 30. Statistical power is zero; PASS/FAIL on this sample is meaningless.

## Summary statistics

| Metric | Value |
|---|---|
| Trades (paired ENTER+EXIT) | 13 |
| Winners (gross) | 11 (84.6%) |
| Losers (gross) | 2 |
| **Gross P&L total** | **₹7,626** |
| Gross P&L mean per trade | ₹587 |
| Gross P&L median per trade | ₹215 |
| **Total charges** | **₹395** |
| Charges as % of gross | 5.2% |
| Charges mean per trade | ₹30 |
| **Net P&L total** | **₹7,257** |
| Net P&L mean per trade | ₹558 |
| Net P&L median per trade | ₹207 |
| Net win rate | 76.9% |
| **Net Sharpe (per-trade × √N)** | **2.118** |
| Gross Sharpe (per-trade × √N) | 2.167 |

## Lot-size scaling (linear approximation)

| Lots | Net Total (₹) | Net mean per trade (₹) | Per-trade Sharpe |
|---|---|---|---|
| 1× (75 contracts) | 7,257 | 558.3 | +2.118 |
| 5× (375 contracts) | 36,287 | 2,791.3 | +2.118 |
| 10× (750 contracts) | 72,575 | 5,582.7 | +2.118 |

Note: linear scaling assumes constant slippage per lot. Real 5+ lot fills walk the order book and pay deeper into the depth, so net P&L at 5/10 lots is OPTIMISTIC vs real.

## Per-strategy breakdown

```
             n  gross_total  gross_mean  charges_total  net_total  net_mean  win_rate_net
strategy_id                                                                              
ic_1         3      1218.75      406.25          39.08    1194.67    398.22        100.00
portfolio_1  4       430.00      107.50          36.70     389.55     97.39         75.00
straddle_1   3      5205.00     1735.00         279.38    4925.62   1641.87         66.67
strangle_1   3       772.50      257.50          39.84     747.66    249.22         66.67
```

## Per-mode breakdown

```
             n  gross_total  charges_total  net_total  net_mean
mode                                                           
iron_condor  6      1436.70          70.52    1377.43    229.57
straddle     3      5205.00         279.38    4925.62   1641.87
strangle     4       984.55          45.10     954.45    238.61
```

## Period split (pre vs post Apr 1 2026 STT hike to 0.15%)

```
            n  stt_pct  gross_total  charges_total  net_total  net_mean
post_apr1                                                              
True       13     0.15      7626.25          395.0     7257.5    558.27
```

## Slippage analysis (chain bid/ask vs recorded entry_premium)

- Reconstructed 8 of 13 trades
- Mean slippage: -0.1% of entry_premium (positive = recorded > chain bid → strategy overstates collection)
- Median slippage: 0.0%
- Total ENTRY slippage in ₹: -26

**Important:** the slippage shown here is ENTRY-side only (SELL fill at chain bid vs recorded entry_premium). EXIT slippage (BUY back at chain ask vs strategy's exit LTP) is NOT measured here and would push real net P&L further down by a similar magnitude. The net_pnl figures above are therefore OPTIMISTIC; the true net is approximately ``net_pnl × (1 - 2 × |slip_pct|/100)``.

## Charge rate methodology

Date-aware STT (per Indian tax law):
- Until Mar 31 2026: STT options sell = **0.10%** (Oct 1 2024 hike)
- From Apr 1 2026: STT options sell = **0.15%** (Budget 2026)
- 19-day window has 5 days at 0.10% + 14 days at 0.15% rate.

Other charges (per `src/core/constants.py::CHARGES` after Apr 25 2026 audit fix):
- Exchange (NSE F&O options): 0.0353% on premium turnover (post-Oct-2024 NSE circular 100/2024)
- SEBI: 0.0001%
- GST: 18% on (brokerage + exchange + SEBI)
- Stamp duty: 0.003% on options buy
- Brokerage: Zerodha 0.03% or ₹20/order whichever lower

## Decision criteria (locked, per PHASE3_MASTER §V.5)

| Verdict | Criteria |
|---|---|
| PASS | 1-lot net Sharpe > 0.5 AND 5-lot net Sharpe > 0 AND median net per trade > +₹50 AND no single charge > 50% of gross edge |
| YELLOW | 1-lot Sharpe > 0.5 BUT 5-lot Sharpe < 0 |
| FAIL | Any of: 1-lot Sharpe < 0; median per trade < 0; slippage gap > 70%; single charge > 50% of gross |

## What PASS means

Phase 3a starts. Reviewer corrections (5 strategies, HMM,
vol-targeting weights, single common holdout) apply.

## Methodology caveats

- **outcome_pnl is GROSS** (premium-decay × qty); charges are
  computed on top, not deducted from outcome_pnl by the strategy.
- **Charge approximation:** entry_premium is split evenly across
  legs (2 for strangle/straddle, 4 for IC). For IC's asymmetric
  wings this is coarse but conservative for cost magnitude.
- **Lot-size scaling is linear** in this report. Real 5+ lot
  fills walk the book; net P&L at higher sizes is therefore
  OPTIMISTIC vs reality. The capacity gate already showed
  this in `wide_baseline_portfolio.md`.
- **Strike reconstruction is best-effort.** Where chain data
  was sparse around entry minute, slippage analysis reports
  'not computable' rather than guess.

## Per-trade detail (full)

```
      date strategy_id     leg        mode   vix  quantity  entry_premium  outcome_pnl  stt_rate_pct  total_charges  net_pnl                                exit_reason
2026-04-20 portfolio_1 PREMIUM iron_condor 18.20        75          44.65       133.78          0.15          10.16   127.37                                  Time exit
2026-04-22 portfolio_1 PREMIUM    strangle 18.34        75          23.60       212.05          0.15           5.26   206.79       Profit target: premium decayed 12.0%
2026-04-22  strangle_1 PREMIUM    strangle 18.34        75          23.60       266.25          0.15           5.22   261.03       Profit target: premium decayed 15.0%
2026-04-22  straddle_1 PREMIUM    straddle 18.34        75         469.20      2051.25          0.15         106.50  1944.75                          Exit time reached
2026-04-22        ic_1 PREMIUM iron_condor 18.34        75          64.95       671.25          0.15          14.44   653.06                          Exit time reached
2026-04-23 portfolio_1 PREMIUM iron_condor 18.68        75          45.40       215.18          0.15          10.28   197.40  Trail stop: bounced 20.5% after 27% decay
2026-04-23        ic_1 PREMIUM iron_condor 18.68        75          61.55       442.50          0.15          13.84   447.41                          Exit time reached
2026-04-23  strangle_1 PREMIUM    strangle 18.68        75          83.35       945.00          0.15          18.46   941.54       Profit target: premium decayed 15.1%
2026-04-23  straddle_1 PREMIUM    straddle 18.67        75         414.20      3123.75          0.15          92.98  3030.77       Profit target: premium decayed 10.1%
2026-04-24 portfolio_1 PREMIUM iron_condor 19.20        75          47.20      -131.01          0.15          11.00  -142.01 Friday 14:55 square-off (weekend gap risk)
2026-04-24        ic_1 PREMIUM iron_condor 19.20        75          47.20       105.00          0.15          10.80    94.20                          Exit time reached
2026-04-24  strangle_1 PREMIUM    strangle 19.20        75          68.55      -438.75          0.15          16.16  -454.91               Trailing stop: bounced 15.0%
2026-04-24  straddle_1 PREMIUM    straddle 19.20        75         346.75        30.00          0.15          79.90   -49.90                          Exit time reached
```
