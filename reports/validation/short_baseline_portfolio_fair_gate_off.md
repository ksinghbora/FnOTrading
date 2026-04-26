# Validation Report — portfolio

- Run ID: `portfolio_20260425_0153`
- Split train_end: 2024-11-29
- Split val_end: 2024-12-31
- Split holdout_end: 2025-01-31

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| cpcv_stability | PASS | median=0.990 (>0.5), p05=0.890 (>0) |
| cpcv_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| dsr | PASS | dsr=0.995 (>0.95) |
| wf_decay | PASS | median_decay=0.000 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.7) |
| regime | FAIL | failing: high_vix(sharpe=-2.33, n=2516), trending(sharpe=-7.38, n=1009) |
| cost_sensitivity | PASS | sharpe@+0.5 shift = 0.200 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [91.4337, 83.367, 73.2503] |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2024-11-29 (61 days)
- Val window: 2024-11-29 → 2024-12-31 (21 days)
- Holdout window: 2024-12-31 → 2025-01-31 (0 days)

## 3. CPCV Distribution

- Paths: 45
- Mean Sharpe: 1.067
- Median Sharpe: 0.990
- 5th pct Sharpe: 0.890
- 95th pct Sharpe: 1.570
- PBO: N/A (single-config)
- DSR: 0.995
- PSR: 1.000

```
[ +0.550.. +0.652] ### (1)
[ +0.652.. +0.754]  (0)
[ +0.754.. +0.856]  (0)
[ +0.856.. +0.958] ################################################## (18)
[ +0.958.. +1.060] #################################### (13)
[ +1.060.. +1.162] ################# (6)
[ +1.162.. +1.264]  (0)
[ +1.264.. +1.366]  (0)
[ +1.366.. +1.468] ### (1)
[ +1.468.. +1.570] ################# (6)
```

## 4. Walk-Forward

- Windows: 0 | median_decay=0.000 | frac_positive=0.00 | mean_test_sharpe=0.000

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **2516** | **-673826.52** | **-2.3262** | **61.21** | **-734280.23** | **FAIL** |
| mid_vix | 4383 | 436035.95 | 1.3289 | 61.76 | -623505.75 | PASS |
| low_vix | 3676 | 821457.75 | 2.9859 | 53.56 | -568791.75 | PASS |
| expiry_week | 5764 | 876029.18 | 2.2863 | 61.21 | -534328.5 | PASS |
| event_day | 1157 | 284749.49 | 2.2846 | 65.95 | -30598.5 | PASS |
| **trending** | **1009** | **-1297759.5** | **-7.3828** | **37.56** | **-1305883.5** | **FAIL** |
| range_bound | 6060 | 1223921.93 | 2.6565 | 53.2 | -488119.5 | PASS |

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

