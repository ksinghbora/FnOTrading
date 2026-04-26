# Validation Report — portfolio

- Run ID: `portfolio_20260424_0734`
- Split train_end: 2024-11-29
- Split val_end: 2024-12-31
- Split holdout_end: 2025-01-31

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| cpcv_stability | PASS | median=0.650 (>0.5), p05=0.540 (>0) |
| cpcv_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| dsr | FAIL | dsr=0.621 (>0.95) |
| wf_decay | PASS | median_decay=-5.950 (<0.5) |
| wf_coverage | PASS | fraction_positive_test=1.00 (>=0.7) |
| regime | FAIL | failing: high_vix(sharpe=-0.95, n=513), trending(sharpe=-5.83, n=160) |
| cost_sensitivity | PASS | sharpe@+0.5 shift = 0.200 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [91.4337, 83.367, 73.2503] |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2024-11-29 (61 days)
- Val window: 2024-11-29 → 2024-12-31 (21 days)
- Holdout window: 2024-12-31 → 2025-01-31 (0 days)

## 3. CPCV Distribution

- Paths: 6
- Mean Sharpe: 0.780
- Median Sharpe: 0.650
- 5th pct Sharpe: 0.540
- 95th pct Sharpe: 1.340
- PBO: N/A (single-config)
- DSR: 0.621
- PSR: 0.907

```
[ +0.540.. +0.639] ################################################## (3)
[ +0.639.. +0.738]  (0)
[ +0.738.. +0.837] ################################# (2)
[ +0.837.. +0.936]  (0)
[ +0.936.. +1.035]  (0)
[ +1.035.. +1.134]  (0)
[ +1.134.. +1.233]  (0)
[ +1.233.. +1.332]  (0)
[ +1.332.. +1.431]  (0)
[ +1.431.. +1.530] ################# (1)
```

## 4. Walk-Forward

- Windows: 3 | median_decay=-5.950 | frac_positive=1.00 | mean_test_sharpe=4.880

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-09-02..2024-11-05 | 2024-11-07..2024-11-29 | -0.130 | 5.820 | -5.950 | 100 |
| 1 | 2024-09-16..2024-11-21 | 2024-11-25..2024-12-13 | 1.370 | 1.720 | -0.350 | 80 |
| 2 | 2024-09-30..2024-12-05 | 2024-12-09..2024-12-30 | 0.680 | 7.100 | -6.420 | 104 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **513** | **-51347.34** | **-0.949** | **66.47** | **-132555.0** | **FAIL** |
| mid_vix | 703 | 113632.47 | 2.2015 | 63.02 | -103238.25 | PASS |
| low_vix | 568 | 142923.0 | 3.3116 | 54.4 | -88417.5 | PASS |
| expiry_week | 971 | 185597.13 | 2.9305 | 63.85 | -80358.75 | PASS |
| event_day | 204 | 78274.49 | 4.2217 | 71.57 | -4948.5 | PASS |
| **trending** | **160** | **-146927.25** | **-5.8254** | **48.75** | **-172235.25** | **FAIL** |
| range_bound | 1025 | 283079.13 | 3.799 | 56.98 | -70345.5 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.440 | 1.08 | 15293.15 |
| -0.50 | 0.360 | 1.06 | 12492.84 |
| -0.25 | 0.320 | 1.06 | 11092.68 |
| +0.00 | 0.280 | 1.05 | 9692.52 |
| +0.25 | 0.240 | 1.04 | 8292.37 |
| +0.50 | 0.200 | 1.03 | 6892.21 |
| +1.00 | 0.120 | 1.02 | 4091.9 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | 6857.52 | 91.4337 | 19.5 | 82 |
| 150 | 12505.05 | 83.367 | 30.48 | 82 |
| 300 | 21975.1 | 73.2503 | 46.83 | 82 |
| 750 | 43005.24 | 57.3403 | 82.44 | 82 |
| 1500 | 53540.09 | 35.6934 | 135.71 | 82 |

```
   75 |   +91.4337 ########################################
  150 |   +83.3670 ####################################
  300 |   +73.2503 ################################
  750 |   +57.3403 #########################
 1500 |   +35.6934 ################
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

