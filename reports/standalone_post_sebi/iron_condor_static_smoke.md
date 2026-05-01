# Validation Report — iron_condor

- Run ID: `iron_condor_20260501_0048`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-4.250 (>0.3); p05=-4.314 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=-1.660 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.6) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-2458.3776, -2463.7109, -2473.4442] |
| mc_skill_pvalue | FAIL | p=1.0000 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-4.996, -3.154] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -4.088
- Median Sharpe: -4.250
- 5th pct Sharpe: -4.314
- 95th pct Sharpe: -3.694
- PBO: N/A (single-config)
- DSR: 0.002
- PSR: 0.004

```
[ -4.320.. -4.251] ################################################## (2)
[ -4.251.. -4.182] ######################### (1)
[ -4.182.. -4.113]  (0)
[ -4.113.. -4.044]  (0)
[ -4.044.. -3.975]  (0)
[ -3.975.. -3.906] ######################### (1)
[ -3.906.. -3.837]  (0)
[ -3.837.. -3.768]  (0)
[ -3.768.. -3.699]  (0)
[ -3.699.. -3.630] ######################### (1)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=-1.660 | frac_positive=0.00 | mean_test_sharpe=-3.740

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -5.300 | -3.640 | -1.660 | 8 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -4.480 | -2.020 | -2.460 | 16 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -4.370 | -7.000 | 2.630 | 40 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -4.080 | -6.040 | 1.960 | 40 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -4.780 | 0.000 | -4.780 | 0 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 29 | 8126.25 | 3.6503 | 65.52 | -4166.25 | PASS |
| mid_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 9 | 3667.5 | 4.9507 | 55.56 | -2276.25 | PASS |
| event_day | 7 | 1323.75 | 1.6037 | 71.43 | -4012.5 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 29 | 8126.25 | 3.6503 | 65.52 | -4166.25 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.37 | -182617.69 |
| -0.50 | 0.000 | 0.37 | -183248.63 |
| -0.25 | 0.000 | 0.37 | -183564.1 |
| +0.00 | 0.000 | 0.37 | -183879.57 |
| +0.25 | 0.000 | 0.37 | -184195.04 |
| +0.50 | 0.000 | 0.37 | -184510.5 |
| +1.00 | 0.000 | 0.37 | -185141.44 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -184378.32 | -2458.3776 | 2.9 | 176 |
| 150 | -369556.63 | -2463.7109 | 18.0 | 176 |
| 300 | -742033.27 | -2473.4442 | 47.53 | 176 |
| 750 | -1875303.17 | -2500.4042 | 134.54 | 176 |
| 1500 | -3816886.34 | -2544.5909 | 279.02 | 176 |

```
   75 | -2458.3776 ---------------------------------------
  150 | -2463.7109 ---------------------------------------
  300 | -2473.4442 ---------------------------------------
  750 | -2500.4042 ---------------------------------------
 1500 | -2544.5909 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

