# Validation Report — long_calendar

- Run ID: `long_calendar_20260501_0027`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-3.230 (>0.3); p05=-3.650 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | FAIL | median_decay=0.520 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.6) |
| regime | FAIL | failing: high_vix(sharpe=-0.75, n=61), mid_vix(sharpe=-3.17, n=115), expiry_week(sharpe=-4.21, n=71), event_day(sharpe=-3.71, n=25), range_bound(sharpe=-2.63, n=176) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-1374.8559, -1375.7309, -1377.0309] |
| mc_skill_pvalue | FAIL | p=0.9997 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-3.998, -1.886] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -3.212
- Median Sharpe: -3.230
- 5th pct Sharpe: -3.650
- 95th pct Sharpe: -2.606
- PBO: N/A (single-config)
- DSR: 0.001
- PSR: 0.005

```
[ -3.650.. -3.535] ################################################## (2)
[ -3.535.. -3.420]  (0)
[ -3.420.. -3.305]  (0)
[ -3.305.. -3.190] ######################### (1)
[ -3.190.. -3.075]  (0)
[ -3.075.. -2.960] ######################### (1)
[ -2.960.. -2.845]  (0)
[ -2.845.. -2.730]  (0)
[ -2.730.. -2.615]  (0)
[ -2.615.. -2.500] ######################### (1)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=0.520 | frac_positive=0.00 | mean_test_sharpe=-2.844

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -2.290 | 0.000 | -2.290 | 0 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -2.210 | -4.140 | 1.930 | 12 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -3.210 | -3.730 | 0.520 | 8 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -3.170 | -6.350 | 3.180 | 10 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -4.810 | 0.000 | -4.810 | 0 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **61** | **-4605.0** | **-0.7458** | **55.74** | **-12412.5** | **FAIL** |
| **mid_vix** | **115** | **-76931.25** | **-3.1652** | **56.52** | **-82691.25** | **FAIL** |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **expiry_week** | **71** | **-59775.0** | **-4.2068** | **47.89** | **-64552.5** | **FAIL** |
| **event_day** | **25** | **-20452.5** | **-3.7086** | **48.0** | **-27086.25** | **FAIL** |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **range_bound** | **176** | **-81536.25** | **-2.6323** | **56.25** | **-87296.25** | **FAIL** |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.18 | -96032.32 |
| -0.50 | 0.000 | 0.17 | -99142.01 |
| -0.25 | 0.000 | 0.17 | -100696.85 |
| +0.00 | 0.000 | 0.16 | -102251.7 |
| +0.25 | 0.000 | 0.16 | -103806.54 |
| +0.50 | 0.000 | 0.16 | -105361.38 |
| +1.00 | 0.000 | 0.15 | -108471.07 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -103114.2 | -1374.8559 | 6.35 | 18 |
| 150 | -206359.64 | -1375.7309 | 186.5 | 18 |
| 300 | -413109.28 | -1377.0309 | 474.06 | 18 |
| 750 | -1034888.21 | -1379.8509 | 1162.2 | 18 |
| 1500 | -2076286.41 | -1384.1909 | 2250.91 | 18 |

```
   75 | -1374.8559 ----------------------------------------
  150 | -1375.7309 ----------------------------------------
  300 | -1377.0309 ----------------------------------------
  750 | -1379.8509 ----------------------------------------
 1500 | -1384.1909 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

