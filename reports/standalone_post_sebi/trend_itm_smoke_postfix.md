# Validation Report — trend_itm

- Run ID: `trend_itm_20260501_1627`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-0.090 (>0.3); p05=-0.718 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | FAIL | median_decay=2.340 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.40 (>=0.6) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-100.1, -105.1, -110.1] |
| mc_skill_pvalue | FAIL | p=0.2183 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-0.851, +2.209] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -0.216
- Median Sharpe: -0.090
- 5th pct Sharpe: -0.718
- 95th pct Sharpe: 0.226
- PBO: N/A (single-config)
- DSR: 0.112
- PSR: 0.429

```
[ -0.750.. -0.649] ################################################## (1)
[ -0.649.. -0.548] ################################################## (1)
[ -0.548.. -0.447]  (0)
[ -0.447.. -0.346]  (0)
[ -0.346.. -0.245]  (0)
[ -0.245.. -0.144]  (0)
[ -0.144.. -0.043] ################################################## (1)
[ -0.043.. +0.058]  (0)
[ +0.058.. +0.159] ################################################## (1)
[ +0.159.. +0.260] ################################################## (1)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=2.340 | frac_positive=0.40 | mean_test_sharpe=-0.684

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.210 | 3.370 | -4.580 | 38 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -0.400 | 2.530 | -2.930 | 36 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 1.510 | -1.250 | 2.760 | 38 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 1.530 | -0.810 | 2.340 | 36 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 1.030 | -7.260 | 8.290 | 28 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| mid_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| event_day | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 1.39 | 39826.88 |
| -0.50 | 0.000 | 1.26 | 27994.69 |
| -0.25 | 0.000 | 1.2 | 22078.59 |
| +0.00 | 0.000 | 1.14 | 16162.5 |
| +0.25 | 0.000 | 1.09 | 10246.41 |
| +0.50 | 0.000 | 1.04 | 4330.31 |
| +1.00 | 0.000 | 0.94 | -7501.88 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -7507.5 | -100.1 | 20.15 | 0 |
| 150 | -15765.0 | -105.1 | 20.47 | 0 |
| 300 | -33030.0 | -110.1 | 20.79 | 0 |
| 750 | -84825.0 | -113.1 | 20.99 | 0 |
| 1500 | -171150.0 | -114.1 | 21.05 | 0 |

```
   75 |  -100.1000 -----------------------------------
  150 |  -105.1000 -------------------------------------
  300 |  -110.1000 ---------------------------------------
  750 |  -113.1000 ----------------------------------------
 1500 |  -114.1000 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

