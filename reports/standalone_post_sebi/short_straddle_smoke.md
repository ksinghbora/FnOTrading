# Validation Report — short_straddle

- Run ID: `short_straddle_20260501_0027`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-7.250 (>0.3); p05=-8.514 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=-1.790 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.6) |
| regime | FAIL | failing: high_vix(sharpe=-0.75, n=61), mid_vix(sharpe=-3.17, n=115), expiry_week(sharpe=-4.21, n=71), event_day(sharpe=-3.71, n=25), range_bound(sharpe=-2.63, n=176) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-5423.3899, -5437.0732, -5463.0065] |
| mc_skill_pvalue | FAIL | p=1.0000 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-8.711, -5.947] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -7.212
- Median Sharpe: -7.250
- 5th pct Sharpe: -8.514
- 95th pct Sharpe: -6.116
- PBO: N/A (single-config)
- DSR: 0.001
- PSR: 0.003

```
[ -8.780.. -8.502] ################################################## (1)
[ -8.502.. -8.224]  (0)
[ -8.224.. -7.946]  (0)
[ -7.946.. -7.668]  (0)
[ -7.668.. -7.390] ################################################## (1)
[ -7.390.. -7.112] ################################################## (1)
[ -7.112.. -6.834]  (0)
[ -6.834.. -6.556] ################################################## (1)
[ -6.556.. -6.278]  (0)
[ -6.278.. -6.000] ################################################## (1)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=-1.790 | frac_positive=0.00 | mean_test_sharpe=-8.238

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -8.290 | -13.140 | 4.850 | 96 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -9.850 | -7.560 | -2.290 | 80 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -10.080 | -6.850 | -3.230 | 40 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -9.070 | -7.280 | -1.790 | 40 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -6.740 | -6.360 | -0.380 | 64 |

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
| -1.00 | 0.000 | 0.7 | -399317.99 |
| -0.50 | 0.000 | 0.7 | -401959.86 |
| -0.25 | 0.000 | 0.7 | -403280.8 |
| +0.00 | 0.000 | 0.69 | -404601.74 |
| +0.25 | 0.000 | 0.69 | -405922.68 |
| +0.50 | 0.000 | 0.69 | -407243.61 |
| +1.00 | 0.000 | 0.69 | -409885.49 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -406754.24 | -5423.3899 | 2.0 | 490 |
| 150 | -815560.98 | -5437.0732 | 9.3 | 490 |
| 300 | -1638901.96 | -5463.0065 | 23.78 | 490 |
| 750 | -4153024.89 | -5537.3665 | 67.0 | 490 |
| 1500 | -8490229.79 | -5660.1532 | 138.93 | 490 |

```
   75 | -5423.3899 --------------------------------------
  150 | -5437.0732 --------------------------------------
  300 | -5463.0065 ---------------------------------------
  750 | -5537.3665 ---------------------------------------
 1500 | -5660.1532 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

