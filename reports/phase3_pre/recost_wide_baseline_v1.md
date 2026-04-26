# Phase 3-Pre Truth-Up — Wide-Baseline Re-Cost (v1)

- Generated: 2026-04-25T23:33:35
- Window: 2024-09-02 → 2025-06-25 (200-day train+val)
- Source: 211 paired ENTER+EXIT from `portfolio_bt` backtest (post-Bug-4 dedup; the wide_baseline run)

## Why this analysis exists

The chain-window truth-up (`reports/phase3_pre/fee_truthup_v1.md`) hit **INSUFFICIENT_DATA** — 13 trades over 4 days with effective N=3.2 cannot answer the cost-edge question. The independent reviewer recommended re-costing the much larger sample we already have: the 200-day wide_baseline validation.

The wide_baseline run used the now-stale `CHARGES` constants (STT options sell 0.0625% pre-Oct-2024 rate; Exchange 0.05% pre-Oct-2024 rate). The Apr 25 2026 audit fix updated these to the correct post-Oct-2024 / post-Apr-1-2026 rates. Re-costing the recorded `outcome_pnl` (which is gross — before any charges) with realistic charges produces an honest net P&L verdict.

## VERDICT

**FAIL**

Reasons:
- net Sharpe -0.179 < 0.5
- charges 794.5% > 50% of gross

## Summary

| Metric | Value |
|---|---|
| Trades | 211 paired ENTER+EXIT |
| Days | 150 (out of ~200 trading days) |
| Winners (gross) | 126 (59.7%) |
| Losers (gross) | 85 |
| **Gross P&L total** | **₹514** |
| Gross mean per trade | ₹2 |
| Gross median per trade | ₹180 |
| **Total charges (date-aware STT)** | **₹4,082** |
| Charges as % of gross | **794.5%** |
| Charges mean per trade | ₹19 |
| **Net P&L total** | **₹-3,568** |
| Net mean per trade | ₹-17 |
| Net median per trade | ₹161 |
| Net stdev per trade | ₹1,373 |
| Net win rate | 58.8% |
| **Net Sharpe (per-trade × √N)** | **-0.179** |
| Gross Sharpe (per-trade × √N) | +0.026 |
| Net Sharpe (daily, annualized × √252) | -0.239 |

## Period split — pre vs post Oct 1 2024 STT hike (0.0625% → 0.10%)

```
                n  days  stt_pct  gross_total  charges_total  net_total  net_mean  net_sharpe
post_oct2024                                                                                 
False           8     7     0.06       132.75         117.06      15.69      1.96        0.00
True          203   143     0.10       381.01        3964.72   -3583.71    -17.65       -0.18
```

## Per-mode breakdown

```
               n  gross_total  charges_total  net_total  net_mean  net_sharpe  win_rate_net
mode                                                                                       
debit_spread  88     -7352.62         591.02   -7943.64    -90.27       -1.16         48.86
iron_condor   27      7761.75         554.68    7207.07    266.93        1.06         70.37
strangle      96       104.63        2936.08   -2831.45    -29.49       -0.16         64.58
```

## Per-month breakdown

```
          n  gross_total  charges_total  net_total  net_mean  net_sharpe
month                                                                   
2024-09   8       132.75         117.06      15.69      1.96        0.00
2024-10  24     -3523.50         417.96   -3941.46   -164.23       -0.51
2024-11  24      5704.13         439.60    5264.53    219.36        0.85
2024-12  23      2025.00         492.58    1532.42     66.63        0.24
2025-01  28     -8149.50         602.54   -8752.04   -312.57       -1.08
2025-02  22      9588.01         425.28    9162.73    416.49        1.85
2025-03  18      3951.00         287.82    3663.18    203.51        0.83
2025-04  20     -5947.50         405.64   -6353.14   -317.66       -0.84
2025-05  24      -493.50         470.08    -963.58    -40.15       -0.14
2025-06  20     -2773.13         423.22   -3196.35   -159.82       -0.58
```

## Charge rate methodology (date-aware)

- 2024-09-02 → 2024-09-30 (29 trading days): STT options sell **0.0625%**
- 2024-10-01 → 2025-06-25 (~170 trading days): STT options sell **0.10%**
- (Apr 1 2026 hike to 0.15% does not affect this window)

Other charges (per `src/core/constants.py::CHARGES` after Apr 25 2026 audit):
- Exchange (NSE F&O options): 0.0353% on premium turnover (post Oct 2024)
- SEBI: 0.0001%
- GST: 18% on (brokerage + exchange + SEBI)
- Stamp duty: 0.003% on options buy
- Brokerage: Zerodha 0.03% or ₹20/order whichever lower

**Charge approximation:** entry_premium is split evenly across legs (2 for strangle/straddle, 4 for iron condor). Implied exit price is back-derived from outcome_pnl. This is approximate but conservative for the cost magnitude.

## What this verdict means

Phase 3a does NOT start. Per PHASE3_MASTER §V.5.3, the
operator commits to one of three replan options:
- (a) Pivot to institutional size where the operator
      collects spread instead of paying it.
- (b) Pivot to different strategies (Iron Butterfly, Long
      Calendar, NIFTY/BANKNIFTY relative-vol pair).
- (c) Retire systematic premium-selling at retail scale.

The 30-day moratorium on parameter optimization begins now.

## Methodology caveats

- **outcome_pnl is GROSS** (premium-decay × qty); the strategy
  does NOT deduct charges. Re-costing applies them on top.
- **Charge approximation** splits entry_premium across legs.
  For IC's asymmetric short+wing legs, this slightly
  overstates wing costs (wings are cheaper than shorts).
  Direction: makes the verdict slightly conservative.
- **No slippage adjustment.** Pure charge re-costing. The
  wide_baseline used GDFL parquet bid/ask which may have
  been tighter than real-world chain spreads. Real net is
  therefore OPTIMISTIC vs reality. The ChartreuseyellowFAIL on
  cost_sensitivity at +0.25 in wide_baseline_portfolio.md
  already showed this margin is thin.
- **Linear lot scaling NOT applied** in this report. The
  original wide_baseline already showed -₹82/lot at 75 lots
  (capacity FAIL). The 1-lot Sharpe alone does not imply
  multi-lot viability.
