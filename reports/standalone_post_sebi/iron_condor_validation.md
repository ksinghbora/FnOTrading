# Validation Report — iron_condor

- Run ID: `iron_condor_20260430_2100`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| fold_stability_median_sharpe | FAIL | median=-4.540 (>0.3); p05=-5.214 [diagnostic] |
| fold_stability_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| wf_decay | PASS | median_decay=0.280 (<0.5) |
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
- Paths: 45
- Mean Sharpe: -4.588
- Median Sharpe: -4.540
- 5th pct Sharpe: -5.214
- 95th pct Sharpe: -3.910
- PBO: N/A (single-config)
- DSR: 0.000
- PSR: 0.000

```
[ -5.440.. -5.257] ########## (2)
[ -5.257.. -5.074] ########## (2)
[ -5.074.. -4.891] ######################################## (8)
[ -4.891.. -4.708] ############################## (6)
[ -4.708.. -4.525] ################################### (7)
[ -4.525.. -4.342] ################################################## (10)
[ -4.342.. -4.159] ############### (3)
[ -4.159.. -3.976] #################### (4)
[ -3.976.. -3.793] ##### (1)
[ -3.793.. -3.610] ########## (2)
```

## 4. Walk-Forward

- Windows: 4 | median_decay=0.280 | frac_positive=0.00 | mean_test_sharpe=-5.235

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-04-01 | 2025-04-03..2025-05-20 | -5.080 | -6.310 | 1.230 | 108 |
| 1 | 2024-12-12..2025-04-25 | 2025-04-29..2025-06-10 | -5.290 | -6.860 | 1.570 | 128 |
| 2 | 2025-01-03..2025-05-19 | 2025-05-21..2025-07-01 | -5.490 | -4.820 | -0.670 | 68 |
| 3 | 2025-01-24..2025-06-09 | 2025-06-11..2025-07-22 | -4.780 | -2.950 | -1.830 | 8 |

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

