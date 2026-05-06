# Validation Report — iron_condor

- Run ID: `iron_condor_20260506_0021`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | FAIL | mean_test_sharpe=0.000 (>=0.3) |
| wf_decay | FAIL | median_decay=3.260 (<1.0) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.55) |
| wf_test_sharpe_p25 | PASS | p25_test_sharpe=0.000 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | FAIL | failing: range_bound(sharpe=-3.52, n=31) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [46.95, 40.35, 33.75] |
| mc_skill_pvalue | PASS | p=0.0510 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | PASS | 90%-CI=[-0.027, +4.924] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=3.260 | frac_positive=0.00 | mean_test_sharpe=0.000

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -0.140 | 0.000 | -0.140 | 0 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | 5.170 | 0.000 | 5.170 | 0 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 4.580 | 0.000 | 4.580 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 3.260 | 0.000 | 3.260 | 0 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -0.610 | 0.000 | -0.610 | 0 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 10 | -1428.75 | -2.563 | 60.0 | -671.25 | PASS |
| mid_vix | 10 | -491.25 | -1.2143 | 80.0 | -2332.5 | PASS |
| low_vix | 11 | -2325.0 | -14.1319 | 9.09 | -2385.0 | PASS |
| expiry_week | 7 | -1151.25 | -5.218 | 57.14 | -352.5 | PASS |
| event_day | 4 | -11.25 | -0.1792 | 50.0 | -240.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **range_bound** | **31** | **-4245.0** | **-3.5168** | **48.39** | **-2385.0** | **FAIL** |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 1.26 | 5872.5 |
| -0.50 | 0.000 | 1.23 | 5298.75 |
| -0.25 | 0.000 | 1.21 | 5011.88 |
| +0.00 | 0.000 | 1.2 | 4725.0 |
| +0.25 | 0.000 | 1.19 | 4438.12 |
| +0.50 | 0.000 | 1.17 | 4151.25 |
| +1.00 | 0.000 | 1.15 | 3577.5 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | 3521.25 | 46.95 | 49.1 | 0 |
| 150 | 6052.5 | 40.35 | 74.77 | 0 |
| 300 | 10125.0 | 33.75 | 100.43 | 0 |
| 750 | 22342.5 | 29.79 | 115.82 | 0 |
| 1500 | 42705.0 | 28.47 | 120.95 | 0 |

```
   75 |   +46.9500 ########################################
  150 |   +40.3500 ##################################
  300 |   +33.7500 #############################
  750 |   +29.7900 #########################
 1500 |   +28.4700 ########################
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

