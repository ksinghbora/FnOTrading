# Validation Report — iron_condor

- Run ID: `iron_condor_20260430_1802`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-4.360 (>0.3); p05=-5.032 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=-0.730 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.6) |
| regime | FAIL | failing: high_vix(sharpe=-6.11, n=29), range_bound(sharpe=-6.11, n=29) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-5148.8317, -5158.4317, -5176.2317] |
| mc_skill_pvalue | FAIL | p=1.0000 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-5.788, -3.666] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Fold-Stability Distribution

- Evaluation mode: `train_in_sample` — in-sample fold stability (runner invoked with train_dates)
- Paths: 5
- Mean Sharpe: -4.434
- Median Sharpe: -4.360
- 5th pct Sharpe: -5.032
- 95th pct Sharpe: -3.966
- PBO: N/A (single-config)
- DSR: 0.001
- PSR: 0.004

```
[ -5.110.. -4.994] ######################### (1)
[ -4.994.. -4.878]  (0)
[ -4.878.. -4.762]  (0)
[ -4.762.. -4.646] ######################### (1)
[ -4.646.. -4.530]  (0)
[ -4.530.. -4.414]  (0)
[ -4.414.. -4.298] ######################### (1)
[ -4.298.. -4.182]  (0)
[ -4.182.. -4.066]  (0)
[ -4.066.. -3.950] ################################################## (2)
```

## 4. Walk-Forward

- Windows: 5 | median_decay=-0.730 | frac_positive=0.00 | mean_test_sharpe=-4.320

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -6.080 | -3.640 | -2.440 | 16 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -5.650 | -4.920 | -0.730 | 32 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -5.130 | -7.000 | 1.870 | 76 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -4.560 | -6.040 | 1.480 | 68 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -5.340 | 0.000 | -5.340 | 0 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **29** | **-26058.75** | **-6.1132** | **31.03** | **-28908.75** | **FAIL** |
| mid_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 9 | -645.0 | -0.729 | 33.33 | -3495.0 | PASS |
| event_day | 7 | -2392.5 | -3.1346 | 57.14 | -4473.75 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **range_bound** | **29** | **-26058.75** | **-6.1132** | **31.03** | **-28908.75** | **FAIL** |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.37 | -375188.0 |
| -0.50 | 0.000 | 0.36 | -378245.19 |
| -0.25 | 0.000 | 0.36 | -379773.78 |
| +0.00 | 0.000 | 0.36 | -381302.38 |
| +0.25 | 0.000 | 0.36 | -382830.97 |
| +0.50 | 0.000 | 0.36 | -384359.56 |
| +1.00 | 0.000 | 0.35 | -387416.75 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -386162.38 | -5148.8317 | 15.81 | 328 |
| 150 | -773764.75 | -5158.4317 | 32.69 | 328 |
| 300 | -1552869.51 | -5176.2317 | 65.98 | 328 |
| 750 | -3919703.76 | -5226.2717 | 164.72 | 328 |
| 1500 | -7962827.53 | -5308.5517 | 328.93 | 328 |

```
   75 | -5148.8317 ---------------------------------------
  150 | -5158.4317 ---------------------------------------
  300 | -5176.2317 ---------------------------------------
  750 | -5226.2717 ---------------------------------------
 1500 | -5308.5517 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

