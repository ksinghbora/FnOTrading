# Validation Report — trend_itm

- Run ID: `trend_itm_20260501_2334`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-0.590 (>0.3); p05=-2.036 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | FAIL | median_decay=2.090 (<0.5) |
| wf_coverage | PASS | fraction_positive_test=0.60 (>=0.6) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-287.65, -292.5167, -297.3833] |
| mc_skill_pvalue | FAIL | p=0.4731 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.948, +1.753] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -0.906
- Median Sharpe: -0.590
- 5th pct Sharpe: -2.036
- 95th pct Sharpe: -0.162
- PBO: N/A (single-config)
- DSR: 0.001
- PSR: 0.138

```
[ -2.220.. -2.011] ######################### (1)
[ -2.011.. -1.802]  (0)
[ -1.802.. -1.593]  (0)
[ -1.593.. -1.384]  (0)
[ -1.384.. -1.175] ######################### (1)
[ -1.175.. -0.966]  (0)
[ -0.966.. -0.757]  (0)
[ -0.757.. -0.548] ######################### (1)
[ -0.548.. -0.339]  (0)
[ -0.339.. -0.130] ################################################## (2)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=2.090 | frac_positive=0.60 | mean_test_sharpe=-1.186

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.420 | 2.430 | -3.850 | 38 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -0.850 | 2.590 | -3.440 | 36 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 2.400 | 0.310 | 2.090 | 38 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 1.100 | -1.990 | 3.090 | 36 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.690 | -9.270 | 9.960 | 24 |

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
| -1.00 | 0.000 | 1.15 | 23139.37 |
| -0.50 | 0.000 | 1.07 | 11963.44 |
| -0.25 | 0.000 | 1.04 | 6375.47 |
| +0.00 | 0.000 | 1.0 | 787.5 |
| +0.25 | 0.000 | 0.97 | -4800.47 |
| +0.50 | 0.000 | 0.94 | -10388.44 |
| +1.00 | 0.000 | 0.88 | -21564.38 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -21573.75 | -287.65 | 19.54 | 0 |
| 150 | -43877.5 | -292.5167 | 19.86 | 0 |
| 300 | -89215.0 | -297.3833 | 20.19 | 0 |
| 750 | -225227.5 | -300.3033 | 20.38 | 0 |
| 1500 | -451915.0 | -301.2767 | 20.45 | 0 |

```
   75 |  -287.6500 --------------------------------------
  150 |  -292.5167 ---------------------------------------
  300 |  -297.3833 ---------------------------------------
  750 |  -300.3033 ----------------------------------------
 1500 |  -301.2767 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

