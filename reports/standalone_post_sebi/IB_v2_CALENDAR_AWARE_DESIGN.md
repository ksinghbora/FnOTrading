# Iron Butterfly v2 + Calendar-Aware Filter — In-Depth Design

**Date:** May 7 2026
**Status:** Implemented (commit pending) and smoke-test running
**Built on:** IC v2's CI+VRP regime gate (commit `1b65610` / live deployment `4cff9b3`)
**Inspired by:** Anurag Goel's Sharpe-1.96 short-strangle research, OptionX/Bajaj Broking IB-vs-straddle margin analysis, ResearchGate India-VIX day-of-week paper, RBI/MoSPI event calendar

This document captures the full design rationale for the **first
post-SEBI capital-efficient adaptation** in our codebase.

## Why Iron Butterfly is the right next experiment

After ~5 weeks of post-SEBI research, IC v2 stands as the only
options strategy with positive holdout (+₹584/324 trades/Sharpe 0.35).
The structural insight from that work: **theory-grounded regime
gate + defined-risk premium-selling structure** is the post-SEBI
formula that survives.

Iron Butterfly takes that insight and **doubles capital efficiency**
without changing the regime mechanic:

| Metric | IC v2 | **IB v2 (this design)** |
|---|---|---|
| Short body delta | 0.15 (OTM) | **0.50 (ATM)** |
| Wing width | 8 strikes (~₹400) | **2 strikes (~₹100)** |
| Net credit per lot | ~₹2,000-3,000 | **~₹6,000-9,000** (3× higher) |
| SPAN margin per lot | ~₹2.5 lakh | **~₹1.0 lakh** (60% lower) |
| Capital efficiency | 1× | **2.5×** |
| Max profit zone | ±400 pts from center | ±100 pts from center (tighter) |
| Gamma exposure | Low | **High** (ATM body) |
| Stop loss canonical | 40% of credit | **30% of credit** (gamma-adjusted) |

The 2.5× capital efficiency means a trader with ₹5 lakh can run
**5 lots of IB v2** instead of **2 lots of IC v2** — same regime
edge, more applied per unit of capital.

The trade-off: **higher gamma exposure**. IB's narrow profit zone
(±100 pts on NIFTY 24000 = ±0.4%) means quick spot moves bite
faster. We compensate via:
1. Tighter stop loss (30% vs 40%)
2. Same v2 regime gate (only fire on range-bound + IV-rich)
3. Calendar-aware filter (next section)

## The calendar-aware filter stack

Indian post-SEBI option markets have **predictable seasonality**
that the v2 gate alone doesn't capture. Three orthogonal axes:

### Axis 1 — Day-of-week (Indian VIX intraday seasonality)

ResearchGate India-VIX paper documented:
- **Monday: significant POSITIVE effect** on India VIX (rises early week)
- **Tuesday-Friday: negative effect** (declines through week)
- **Significant fall on options expiration day** (post-Sept-2025: Tuesday for NIFTY)

Anurag Goel's Sharpe-1.96 short-strangle backtest **only enters on
Tue/Wed/Thu**. With this filter Sharpe = 1.96 / 70% WR; without it
the strategy was net-negative.

**Our default `allowed_days_of_week = [1, 2, 3]`** = Tue/Wed/Thu.

Mechanism:
- **Mon (excluded):** weekend gap-reaction + IV-rise into week
- **Tue (included):** NIFTY weekly expiry day post-Sept-2025; intraday short premium captures peak theta
- **Wed (included):** post-expiry calm + fresh weekly contract; theta acceleration
- **Thu (included):** mid-week, full theta runway, low gap risk
- **Fri (excluded):** weekend gap risk + month-end positioning

### Axis 2 — Pre-event window (RBI/CPI/Budget/FOMC)

Sahi.com, ICICI Direct, multiple practitioner sources confirm:
> "Before RBI policy, VIX jumps 5-8 points. All option premiums rise
> 15-30%. After event announcements, VIX crashes quickly. **This IV
> crush hurts option BUYERS badly** — but option SELLERS who entered
> the day before get caught by the event move."

For premium-selling (IB v2 sells ATM straddle body), the failure
mode is:
1. Day before event: Sell ATM straddle at 50-Δ
2. Event announces: spot moves 1-2% → ATM legs go ITM → losses
3. The IV crush AFTER the event doesn't help us because we're
   already underwater on directional move

**Our filter: block entries on the `block_pre_event_days` (default 1)
trading days immediately preceding any HARD_BLOCK event** in
`data/event_days.csv`.

The CSV already exists with 23 HARD_BLOCK events (RBI MPC, FOMC,
Budget, election results) covering 2024-2026. Maintained by
`portfolio_strategy.py`.

Mechanism:
- T-1 trading day before event: block entry (event move risk)
- T (event day): expiry-day check + portfolio_strategy hard-block
- T+1: vol crushed, IV-rich-ness improves → safe to re-enter (next day's regime gate decides)

### Axis 3 — Friday weekend gap (opt-in)

Premium-selling books on Friday carry weekend-gap risk per
`event_calendar.is_friday_for_premium`. Our default `block_friday=False`
because the day-of-week filter (Tue/Wed/Thu only) already excludes
Friday. Optional extra-conservative books can enable it.

## Filter stack ordering in `_try_entry`

```
1. expiry-day block        (STRUCTURAL — keep in all modes)
2. calendar-aware filter   (NEW; opt-in via require_calendar_filter)
   2a. day-of-week
   2b. pre-event window
   2c. friday block (opt-in)
3. v2 regime gate          (CI≥61.8 AND VRP>0; opt-in via require_premium_selling_regime_v2)
4. legacy filters          (skipped when v2 regime active)
5. strike selection + sizing
```

The calendar filter runs **before** the v2 regime gate so that a
calendar-blocked day doesn't even compute regime metrics — saves a
small amount of CPU and keeps the skip-log message clear.

## Parameter set (final design)

```json
{
  "underlying": "NIFTY",
  "quantity_lots": 1,
  "entry_time": "09:30:00",
  "exit_time": "15:00:00",
  "skip_entry_on_expiry_day": true,
  "expiry_day_force_exit_at": "14:30:00",

  "short_call_delta": 0.5,
  "short_put_delta": -0.5,
  "wing_width_strikes": 2,
  "adjustment_threshold_pct": 60.0,
  "stop_loss_pct": 30.0,
  "profit_target_pct": 25.0,
  "max_spread_pct": 5.0,

  "require_premium_selling_regime_v2": true,
  "require_calendar_filter": true,
  "allowed_days_of_week": [1, 2, 3],
  "block_pre_event_days": 1,
  "block_friday": false
}
```

Every parameter is either a literature-canonical default or an
empirically-validated value from our IC v2 work.

### Per-parameter rationale

| Param | Value | Source |
|---|---|---|
| `short_call_delta` | 0.50 | ATM body — IB defining feature |
| `short_put_delta` | -0.50 | ATM body — IB defining feature |
| `wing_width_strikes` | 2 | Tighter than IC's 8; Bajaj Broking ₹100-protection guideline |
| `stop_loss_pct` | 30 | Tighter than IC v2's 40 due to ATM gamma; OptionX guideline 20-40% on net P&L |
| `profit_target_pct` | 25 | Same as IC v2 (validated at PF 1.05 OOS) |
| `adjustment_threshold_pct` | 60 | Same as IC v2 |
| `entry_time` | 09:30 | Skip auction-imbalance noise; IC v2 default |
| `exit_time` | 15:00 | Pre-close exit avoids 15:00-15:30 squaring chaos |
| `max_spread_pct` | 5 | IC v2 liquidity gate |
| `require_premium_selling_regime_v2` | true | CI+VRP gate — IC v2's edge source |
| `require_calendar_filter` | true | NEW |
| `allowed_days_of_week` | [1,2,3] | Anurag Goel Sharpe-1.96 research |
| `block_pre_event_days` | 1 | T-1 block; minimum sensible value |
| `block_friday` | false | Already covered by allowed_days_of_week |

## Expected outcomes

### Sample size projection

IC v2 fires ~324 trades on a 173-day window (≈1.9 trades/day).
IB v2's smaller wing won't change the regime-gate fire rate
materially. Calendar filter projections:

- DoW filter (Tue/Wed/Thu): retains 3 of 5 weekdays = 60% of days
- Pre-event filter (1-day): blocks ~23 days × 1 = ~23 days = ~13% of corpus
- Combined retention: ~52% of original

**Expected sample: ~165-170 trades** on the 173-day corpus
(IC v2's 324 × 0.52 ≈ 168). Still plenty for stable inference.

### PnL projection

If IB v2's gross edge per trade matches IC v2's (~₹4/day at 1 lot
× 22 trading days/month × 1 lot = ~₹90/month), then:
- 168 trades × ₹4 expected = ~₹670 net PnL on 1 lot
- BUT — IB has 3× the credit, so ABSOLUTE PnL per trade is ~3× IC v2
- Expected ~₹1,500-2,500 net PnL on 1 lot, **2-4× IC v2's holdout
  result on the same 173-day window**

### Win rate / Sharpe projection

Calendar filter should improve win rate:
- IC v2 holdout WR: 47%
- IB v2 + calendar projection: 55-65% (matches Anurag Goel's 70%
  for short strangle — IB's tighter zone gives back ~5-15% WR vs
  pure short strangle)

Expected Sharpe: 0.4-0.7 (vs IC v2's 0.35 on holdout)

## Risk factors not yet modeled

1. **Higher gamma exposure** — fast spot moves can blow through the
   30% stop. Backtest captures this; live execution may see worse
   slippage at stop-loss exits.

2. **ATM bid-ask spreads** — ATM strikes have the tightest spreads
   in normal conditions but widen during high-vol days. The
   `max_spread_pct=5%` filter rejects these days.

3. **Same-day expiry interactions** — on NIFTY weekly Tuesday
   expiry days, the strategy's existing `skip_entry_on_expiry_day`
   already handles entries; existing positions auto-roll via
   `_check_expiry_rollover`.

4. **Event calendar accuracy** — `data/event_days.csv` covers
   23 HARD_BLOCK events through 2026; would need maintenance for
   2027+ deployment. Document this gap.

5. **Day-count holidays** — `_count_trading_days` is weekday-only;
   doesn't deduct Indian trading holidays. Pre-event filter may
   slightly under-estimate when Diwali/Holi falls between today
   and the event. Acceptable for v1; future refinement could use
   `MarketClock.is_trading_holiday`.

## Implementation summary

| File | Change |
|---|---|
| `src/strategy/params.py` | Added 4 fields to `IronCondorParams`: `require_calendar_filter`, `allowed_days_of_week`, `block_pre_event_days`, `block_friday`. Updated `IronButterflyParams` with tighter defaults (wing 2, SL 30%). |
| `src/strategy/implementations/iron_condor.py` | Added EventCalendar import + load in `on_start`; calendar filter block in `_try_entry` (DoW + pre-event window + Friday); added `_count_trading_days` helper. |
| `reports/standalone_post_sebi/ib_v2_calendar_aware_params.json` | NEW IB v2 + calendar config |
| `scripts/smoke_ib_v2.py` | NEW smoke harness on 173-day post-SEBI corpus |

## Smoke result

(Appended after smoke completes — runs in ~12-15 minutes)

## Next steps if smoke is positive

1. **Formal WF + holdout validation** of IB v2 + calendar
   (run `scripts/validate_strategy.py --strategy iron_butterfly`
   with the params file)
2. **Live shadow deployment** alongside IC v2 + TrendDaily —
   add `iron_butterfly` entry to `.env STRATEGIES`
3. **Capital allocation comparison:** model 1 lot of IC v2 vs
   2.5 lots of IB v2 → which delivers higher absolute PnL per
   ₹ lakh of capital?

## Methodology takeaway

This design demonstrates the **stacking principle** for Indian
post-SEBI alpha:
1. Start with a structurally-correct vehicle (defined-risk IB)
2. Apply a theory-grounded regime gate (CI+VRP from IC v2)
3. Layer in market-microstructure filters (DoW from VIX seasonality,
   pre-event from event calendar)
4. Recalibrate risk params for vehicle-specific gamma/delta exposure

Each layer is independently grounded and additive without
overfitting (4 stacked filters from 4 different research traditions).
This is the opposite of "tune until backtest looks good" — every
threshold is literature-canonical or directly inherited from
already-validated work.
