# Validation Report — iron_condor

- Run ID: `iron_condor_20260502_1208`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=0.000 (>0.3); p05=-0.784 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=-4.930 (<0.5) |
| wf_coverage | PASS | fraction_positive_test=0.60 (>=0.6) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-7.3, -7.7667, -8.2333] |
| mc_skill_pvalue | FAIL | p=0.8556 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.686, +1.207] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: 0.164
- Median Sharpe: 0.000
- 5th pct Sharpe: -0.784
- 95th pct Sharpe: 1.440
- PBO: N/A (single-config)
- DSR: 0.008
- PSR: 0.500

```
[ -0.980.. -0.702] ################# (1)
[ -0.702.. -0.424]  (0)
[ -0.424.. -0.146]  (0)
[ -0.146.. +0.132] ################################################## (3)
[ +0.132.. +0.410]  (0)
[ +0.410.. +0.688]  (0)
[ +0.688.. +0.966]  (0)
[ +0.966.. +1.244]  (0)
[ +1.244.. +1.522]  (0)
[ +1.522.. +1.800] ################# (1)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=-4.930 | frac_positive=0.60 | mean_test_sharpe=2.184

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.290 | 3.640 | -4.930 | 8 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -2.070 | 3.640 | -5.710 | 8 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 2.300 | 0.000 | 2.300 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -2.070 | 3.640 | -5.710 | 8 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.000 | 0.000 | 0.000 | 0 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 2 | -348.75 | -3.5874 | 50.0 | -720.0 | PASS |
| mid_vix | 1 | -986.25 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| event_day | 1 | 371.25 | 0.0 | 100.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 3 | -1335.0 | -9.8206 | 33.33 | -1706.25 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.87 | -373.12 |
| -0.50 | 0.000 | 0.86 | -415.31 |
| -0.25 | 0.000 | 0.85 | -436.41 |
| +0.00 | 0.000 | 0.85 | -457.5 |
| +0.25 | 0.000 | 0.84 | -478.59 |
| +0.50 | 0.000 | 0.83 | -499.69 |
| +1.00 | 0.000 | 0.82 | -541.87 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -547.5 | -7.3 | 41.56 | 0 |
| 150 | -1165.0 | -7.7667 | 56.81 | 0 |
| 300 | -2470.0 | -8.2333 | 72.05 | 0 |
| 750 | -6385.0 | -8.5133 | 81.2 | 0 |
| 1500 | -12910.0 | -8.6067 | 84.25 | 0 |

```
   75 |    -7.3000 ----------------------------------
  150 |    -7.7667 ------------------------------------
  300 |    -8.2333 --------------------------------------
  750 |    -8.5133 ----------------------------------------
 1500 |    -8.6067 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

