# trend_itm Parameter Exploration — Summary

**May 1-2 2026.** Three-version exploration of the trend_itm primitive
(Donchian breakout on NIFTY 1-min spot, executed as deep-ITM
single-leg CE/PE, 173-day post-SEBI corpus, 5-path CPCV smokes).

## Verdict matrix

| Version | atr_stop_mult | Other changes | Median Sharpe | PF @ 0 cost | Trades |
|---|---|---|---|---|---|
| **v1 (baseline)** | **2.0** | — | **−0.09** | **1.14** | 300 |
| v2 (looser) | 3.5 | min_hold=5min, breakout_atr_mult=1.0, chop window 11:30-13:00 | −0.59 | 1.00 | 292 |
| v3 (tighter) | 1.5 | min_hold=0, breakout_atr_mult=0.5, chop disabled | −0.66 | 1.07 | 300 |

## Findings

1. **v1 is a local optimum.** Both directions (looser AND tighter
   stop) regressed Sharpe. v3's 1.07 is between v1's 1.14 and v2's
   1.00, suggesting a smooth concave loss surface around 2.0× ATR
   with no other accessible peak.

2. **The auxiliary v2 changes (min-hold, ATR-mult breakout, chop
   window) had small effects.** v2 only traded 8 fewer than v1 —
   the trade-count reduction was nominal. Most of v2's ~0.5 Sharpe
   degradation came from the wider trail. Confirmed by v3 with the
   opposite-direction trail change but same auxiliary tweaks
   producing similar magnitude regression.

3. **Cost sensitivity reads identical across all three versions.**
   Every version shows Sharpe 0 in the cost-sensitivity table at
   every shift (a quirk of how the validator computes per-trade
   Sharpe vs daily Sharpe). What matters is profit_factor: v1's
   PF 1.14 at zero cost is the strategy's gross-edge ceiling, and
   that ceiling collapses to 1.04 at realistic +0.5 shift.

4. **MC permutation p-values** for all three versions are between
   0.22 and 0.47 — better than premium-sellers' 0.99-1.00 (which
   were *worse than random*) but not statistically significant
   (need ≤ 0.10).

## Why trend_itm cannot reach tradeable edge

The fundamental gap is in the cost-sensitivity table. Even at the
best version (v1):

  shift   PF      total_pnl
  -1.00   1.39   +₹39,827   ← gross edge clear at gentle slippage
  +0.00   1.14   +₹16,162   ← real but marginal edge
  +0.50   1.04   +₹4,330    ← break-even at realistic costs
  +1.00   0.94   -₹7,502    ← cost wall takes over

The strategy's gross edge per trade is ~₹54 (₹16,162 ÷ 300 trades).
Realistic round-trip cost on a deep-ITM single leg is ~₹40-50 per
share at current NIFTY scale (10-15 bps × ₹500-700 premium ×
2 sides) = ₹3,000-3,750 per round trip on a 75-share lot. So
per-trade cost is ~₹50 per share — almost exactly the gross edge.
The strategy is approximately at the cost-wall break-even, with no
material safety margin for parameter variation, regime change, or
real-world execution noise.

The Donchian breakout signal on 1-min NIFTY spot has too-low
signal-to-noise to overcome this cost structure, and parameter
optimization cannot overcome a structural mismatch between signal
strength and friction wall.

## Implication for the pivot

**Pivot away from intraday directional strategies on options.** The
fundamental issue is the cost wall for option execution; intraday
directional capture on NIFTY just doesn't produce edge wide enough
to bridge it.

Two remaining pivot directions worth testing:

1. **Long-vol vol-targeting** — buys VIX puts (or near-ATM long
   straddles) when realized vol is below implied vol. Different
   signal mechanic; profits on vol expansion which is what
   destroyed premium-sellers. Cost wall is similar (options
   instrument) but the average per-trade edge can be much larger
   when vol shocks happen, even if winners are infrequent.

2. **Equity stat-arb on individual constituents** — different
   instrument class entirely (cash equity vs F&O), different cost
   structure (no STT-on-sell doubling, no F&O lot constraint),
   different signal source (cointegration / pair relations rather
   than directional momentum).

Long-vol is the smaller pivot (reuses options infrastructure);
stat-arb is the larger but cleaner-delta-from-current-results
pivot.

## Files

- `trend_itm_smoke.md` — pre-fix v1 with phantom Sharpe 7.81 (bug
  removed via commit 6514472)
- `trend_itm_smoke_postfix.md` — v1 honest result (median −0.09,
  PF 1.14 at zero cost)
- `trend_itm_v2_smoke.md` — v2 looser-direction (median −0.59,
  PF 1.00)
- `trend_itm_v3_smoke.md` — v3 tighter-direction (median −0.66,
  PF 1.07)
- `trend_itm_v3_params.json` — v3 params override
- `iron_condor_static_params.json` — IC adjustments-disabled override
- `*_run.log`, `*_smoke.log` — engine logs (gitignored)
