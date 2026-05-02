# Validation Report — trend_itm

- Run ID: `trend_itm_20260502_0954`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-0.660 (>0.3); p05=-2.050 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | FAIL | median_decay=3.870 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.40 (>=0.6) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-216.05, -221.05, -226.05] |
| mc_skill_pvalue | FAIL | p=0.3542 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.372, +2.101] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -0.804
- Median Sharpe: -0.660
- 5th pct Sharpe: -2.050
- 95th pct Sharpe: 0.178
- PBO: N/A (single-config)
- DSR: 0.000
- PSR: 0.116

```
[ -2.190.. -1.952] ######################### (1)
[ -1.952.. -1.714]  (0)
[ -1.714.. -1.476] ######################### (1)
[ -1.476.. -1.238]  (0)
[ -1.238.. -1.000]  (0)
[ -1.000.. -0.762]  (0)
[ -0.762.. -0.524] ######################### (1)
[ -0.524.. -0.286]  (0)
[ -0.286.. -0.048]  (0)
[ -0.048.. +0.190] ################################################## (2)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=3.870 | frac_positive=0.40 | mean_test_sharpe=-1.034

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.530 | 1.730 | -3.260 | 38 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -1.200 | 4.730 | -5.930 | 36 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 1.300 | -2.570 | 3.870 | 38 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 1.200 | -4.850 | 6.050 | 36 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.480 | -4.210 | 4.690 | 28 |

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
| -1.00 | 0.000 | 1.34 | 30785.62 |
| -0.50 | 0.000 | 1.2 | 19035.94 |
| -0.25 | 0.000 | 1.13 | 13161.09 |
| +0.00 | 0.000 | 1.07 | 7286.25 |
| +0.25 | 0.000 | 1.01 | 1411.41 |
| +0.50 | 0.000 | 0.96 | -4463.44 |
| +1.00 | 0.000 | 0.86 | -16213.13 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -16203.75 | -216.05 | 20.0 | 0 |
| 150 | -33157.5 | -221.05 | 20.33 | 0 |
| 300 | -67815.0 | -226.05 | 20.65 | 0 |
| 750 | -171787.5 | -229.05 | 20.84 | 0 |
| 1500 | -345075.0 | -230.05 | 20.91 | 0 |

```
   75 |  -216.0500 --------------------------------------
  150 |  -221.0500 --------------------------------------
  300 |  -226.0500 ---------------------------------------
  750 |  -229.0500 ----------------------------------------
 1500 |  -230.0500 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

