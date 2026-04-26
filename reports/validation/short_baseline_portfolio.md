# Validation Report — portfolio

- Run ID: `portfolio_20260425_1056`
- Split train_end: 2024-11-29
- Split val_end: 2024-12-31
- Split holdout_end: 2025-01-31

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| cpcv_stability | FAIL | median=1.060 (>0.5), p05=-0.672 (>0) |
| cpcv_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| dsr | FAIL | dsr=0.000 (>0.95) |
| wf_decay | PASS | median_decay=0.000 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.7) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | PASS | sharpe@+0.5 shift = 0.160 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [52.5543, 46.421, 39.0376] |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2024-11-29 (61 days)
- Val window: 2024-11-29 → 2024-12-31 (21 days)
- Holdout window: 2024-12-31 → 2025-01-31 (0 days)

## 3. CPCV Distribution

- Paths: 45
- Mean Sharpe: 1.053
- Median Sharpe: 1.060
- 5th pct Sharpe: -0.672
- 95th pct Sharpe: 2.870
- PBO: N/A (single-config)
- DSR: 0.000
- PSR: 1.000

```
[ -1.560.. -1.012] ###### (1)
[ -1.012.. -0.464] ################### (3)
[ -0.464.. +0.084] ############################### (5)
[ +0.084.. +0.632] ################################################## (8)
[ +0.632.. +1.180] ################################################## (8)
[ +1.180.. +1.728] ############################################ (7)
[ +1.728.. +2.276] ###################################### (6)
[ +2.276.. +2.824] ################### (3)
[ +2.824.. +3.372] ############ (2)
[ +3.372.. +3.920] ############ (2)
```

## 4. Walk-Forward

- Windows: 0 | median_decay=0.000 | frac_positive=0.00 | mean_test_sharpe=0.000

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 24 | 2967.75 | 1.7415 | 66.67 | -3150.0 | PASS |
| mid_vix | 45 | -1275.37 | -0.3277 | 57.78 | -9708.75 | PASS |
| low_vix | 10 | 2766.0 | 9.5184 | 80.0 | -663.75 | PASS |
| expiry_week | 41 | 3420.38 | 1.0759 | 60.98 | -5823.75 | PASS |
| event_day | 11 | 3375.0 | 3.5445 | 63.64 | -1012.5 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 44 | 5838.75 | 1.2441 | 65.91 | -11745.0 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.430 | 1.08 | 10603.45 |
| -0.50 | 0.340 | 1.06 | 8365.63 |
| -0.25 | 0.290 | 1.05 | 7246.73 |
| +0.00 | 0.250 | 1.04 | 6127.82 |
| +0.25 | 0.200 | 1.04 | 5008.92 |
| +0.50 | 0.160 | 1.03 | 3890.01 |
| +1.00 | 0.070 | 1.01 | 1652.2 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | 3941.57 | 52.5543 | 23.3 | 50 |
| 150 | 6963.14 | 46.421 | 36.57 | 50 |
| 300 | 11711.29 | 39.0376 | 56.19 | 50 |
| 750 | 21455.72 | 28.6076 | 98.45 | 50 |
| 1500 | 22781.05 | 15.1874 | 161.26 | 50 |

```
   75 |   +52.5543 ########################################
  150 |   +46.4210 ###################################
  300 |   +39.0376 ##############################
  750 |   +28.6076 ######################
 1500 |   +15.1874 ############
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

