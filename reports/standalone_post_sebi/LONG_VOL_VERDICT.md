# Long-Vol on Indian Post-SEBI Options — Final Verdict

**Status:** Three long-vol structures tested with same VRP<0 gate. All
unprofitable on 173-day train+val. Cost wall + structural drag are
the binding constraints, not the gate. Pivot to NIFTY Futures Trend
recommended.

## TL;DR

| Variant | Round trips | Win Rate | Net P&L | Worst | Sharpe | Verdict |
|---|---|---|---|---|---|---|
| LC v2  (CI≥61.8 AND VRP<0) | 17 | 47.1% | -₹1,707     | -₹1,481  | -0.95 | Sample-thin loser |
| LC v2b (VRP<0 only)         | 31 | 19.4% | -₹120,734   | -₹26,640 | -3.02 | Catastrophic |
| **LS v2b (VRP<0 only)**     | **73** | **36.3%** | **-₹13,900** | **-₹7,084** | **-0.53** | **At cost wall** |

LS v2b achieves what we hoped: removes the catastrophic losses that
killed LC v2b (no more spread collapses from spot leaving strike).
Per-trade loss dropped from -₹4,117 (LC v2b) → -₹190 (LS v2b), a 22×
improvement. Best trade +₹14,640 confirms the structure can capture
big payoffs.

But it's still net negative: ~-₹190/trade × 73 trades = -₹13,900.

## What this proves

1. **The gate is not the binding constraint.** VRP<0 selects ~13% of
   trading days; both structures fired meaningful samples on those
   days.

2. **The structure is not the binding constraint** (in the
   "catastrophic vs survivable" sense). LS v2b's 36% win rate and
   -0.53 Sharpe are MUCH closer to break-even than LC v2b's
   catastrophic outcomes.

3. **The cost wall IS the binding constraint.** -₹190/trade matches
   the round-trip cost basis on 75-lot NIFTY weekly options:
   - 4 spread crosses (CE buy + PE buy at entry, CE sell + PE sell
     at exit), each ~₹50-100 per share on ATM options
   - 75-share lot
   - STT 0.10% on options sell side (post-SEBI), GST, brokerage
   - Total ~₹150-200 per round trip per lot

The cost basis approximately equals the gross edge from buying at
VRP<0 — leaving no margin for parameter variation or regime shifts.
This matches the trend_itm finding (May 1) that "intraday directional
on options doesn't have edge wide enough to bridge the cost wall."

## Exit-reason breakdown (LS v2b)

| Exit reason | Count | Implication |
|---|---|---|
| Exit time reached (15:00) | 46 (63%) | Most trades held all day, theta drift |
| Stop loss (-50%) | 9 (12%) | Straddle value collapsed to half debit |
| Profit target (+50%) | 9 (12%) | Big move or vol expansion captured |
| Expiry-day buffer | 9 (12%) | Forced close near expiry |

Symmetric stop/target rate (9 each) suggests the gate isn't selecting
days that systematically favor long-vol — random walk relative to
exit thresholds. Cost wall does the rest.

## Cumulative options-strategy verdict on Indian post-SEBI

After ~3 weeks of theory-grounded gate design across short and long
premium structures, the Indian post-SEBI options market shows:

| Strategy class | Best variant | Train+Val verdict | Cost-wall margin |
|---|---|---|---|
| Premium-selling | IC v2 (CI+VRP) | +PF 1.20 / +₹584 holdout | ~PF 1.05 OOS — marginal |
| Long-vol calendar | LC v2 / v2b | -₹1,707 to -₹120,734 | Below cost wall |
| Long-vol straddle | LS v2b | -₹13,900 | At cost wall |
| Trend on options (trend_itm) | v1 (May 1) | -0.09 Sharpe, PF 1.04 at +0.5 cost shift | At cost wall |

**Only IC v2 has a positive holdout result, and even that's marginal
(~PF 1.05 OOS, ~₹4/day expected).** Every other options strategy
either fails (premium-selling without v2 filtering) or sits at the
cost wall (long-vol, intraday-directional).

## Why the cost wall dominates

NIFTY weekly options post-SEBI:
- STT on sell: 0.10% (Oct 2024 - Mar 2026; rises to 0.15% Apr 2026)
- 4-leg structures cross 4 spreads per round trip
- Lot size: 75 (was 25 pre-SEBI)
- Spread typically 0.5-1.5% of premium per leg

The math: a strategy needs >100-150bps of edge per round trip to
overcome friction. None of the options strategies show that level
of edge gross of cost.

## Pivot recommendation: NIFTY Futures Trend (Candidate B)

The original PIVOT_DESIGN_trend_futures.md recommendation, deferred
because GDFL corpus lacks futures data. Now justified:

| Why futures | Cost basis |
|---|---|
| No SEBI options-sell STT hike | Futures STT structure unchanged |
| 1 leg vs 4 — atomic fill | ~1bp slippage round-trip vs 5-15bp on options |
| No theta decay | Position can hold through quiet periods |
| Different alpha source | Directional momentum/breakout |

Estimated effort: 3-4 weeks (data ingestion + strategy + WF validation).

Alternative pivots if futures also disappoints:
- Equity stat-arb on NIFTY constituents (different instrument class)
- Vol-targeting on individual stocks
- Multi-timeframe trend on commodities

## Files

- `reports/standalone_post_sebi/lc_v2_research_params.json`
- `reports/standalone_post_sebi/lc_v2b_research_params.json`
- `reports/standalone_post_sebi/ls_v2b_research_params.json`
- `reports/standalone_post_sebi/LC_v2_FINDINGS.md`
- `reports/standalone_post_sebi/LONG_VOL_VERDICT.md` (this file)
- `scripts/smoke_lc_v2.py`, `scripts/smoke_lc_v2b.py`, `scripts/smoke_ls_v2b.py`
- `src/strategy/implementations/long_straddle.py` (new)
- `src/strategy/implementations/long_calendar.py` (extended with v2/v2b)

## Final word

The long-vol thesis on Indian post-SEBI options is structurally
constrained by the cost wall. Even with theoretically-grounded
gates and the better-aligned long-straddle structure, the gross
edge approximately equals the round-trip transaction cost. There
is no parameter-tunable fix; the pivot to a different cost
structure (futures) or a different instrument class (cash equity
stat-arb) is the disciplined next step.

The **methodology continues to work** — every iteration produced
honest data and clear verdicts. The verdict is just that this
market regime + this instrument class + this strategy family don't
have sufficient edge to overcome friction.
