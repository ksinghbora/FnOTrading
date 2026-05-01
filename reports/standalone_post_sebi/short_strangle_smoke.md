# Validation Report — short_strangle

- Run ID: `short_strangle_20260501_0027`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-8.340 (>0.3); p05=-9.868 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=-3.000 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.6) |
| regime | FAIL | failing: high_vix(sharpe=-0.75, n=61), mid_vix(sharpe=-3.17, n=115), expiry_week(sharpe=-4.21, n=71), event_day(sharpe=-3.71, n=25), range_bound(sharpe=-2.63, n=176) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-9911.7828, -9918.6495, -9931.2162] |
| mc_skill_pvalue | FAIL | p=1.0000 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-10.117, -6.712] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -8.066
- Median Sharpe: -8.340
- 5th pct Sharpe: -9.868
- 95th pct Sharpe: -6.604
- PBO: N/A (single-config)
- DSR: 0.000
- PSR: 0.003

```
[-10.250.. -9.879] ######################### (1)
[ -9.879.. -9.508]  (0)
[ -9.508.. -9.137]  (0)
[ -9.137.. -8.766]  (0)
[ -8.766.. -8.395]  (0)
[ -8.395.. -8.024] ################################################## (2)
[ -8.024.. -7.653]  (0)
[ -7.653.. -7.282]  (0)
[ -7.282.. -6.911]  (0)
[ -6.911.. -6.540] ################################################## (2)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=-3.000 | frac_positive=0.00 | mean_test_sharpe=-9.078

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -9.440 | -16.080 | 6.640 | 52 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -10.840 | -7.730 | -3.110 | 28 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -11.260 | -9.590 | -1.670 | 32 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -10.480 | -7.320 | -3.160 | 24 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -7.670 | -4.670 | -3.000 | 28 |

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
| -1.00 | 0.000 | 0.09 | -739080.59 |
| -0.50 | 0.000 | 0.09 | -740604.03 |
| -0.25 | 0.000 | 0.09 | -741365.74 |
| +0.00 | 0.000 | 0.09 | -742127.46 |
| +0.25 | 0.000 | 0.09 | -742889.18 |
| +0.50 | 0.000 | 0.09 | -743650.9 |
| +1.00 | 0.000 | 0.08 | -745174.34 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -743383.71 | -9911.7828 | 2.94 | 228 |
| 150 | -1487797.43 | -9918.6495 | 9.25 | 228 |
| 300 | -2979364.85 | -9931.2162 | 21.65 | 228 |
| 750 | -7474587.13 | -9966.1162 | 58.27 | 228 |
| 1500 | -15035024.26 | -10023.3495 | 119.13 | 228 |

```
   75 | -9911.7828 ----------------------------------------
  150 | -9918.6495 ----------------------------------------
  300 | -9931.2162 ----------------------------------------
  750 | -9966.1162 ----------------------------------------
 1500 | -10023.3495 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

