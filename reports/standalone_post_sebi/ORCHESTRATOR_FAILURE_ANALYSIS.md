# Orchestrator V4 — Why Negative Despite All-Positive Standalones

May 7 2026 · branch `FnO-v5-orchestration-impl`

User question: "Why are individual strategies positive but orchestrator
negative? It should be more positive."

This document is a structural analysis of WHY the orchestrator failed
validation. Per-child fill counts from the diagnostic script will
arrive in ~15 min and confirm or refute these hypotheses.

## Expected vs actual

If the orchestrator picked the BEST child per opportunity:

  Standalone PnL:  IC +₹263 + IB +₹222 + SS +₹788 = **+₹1,273 across 76 standalone trips**
  Best-case orchestrator (single-slot, picks best per day):
    18 SS days × +₹43.8 = +₹788
    + IC's best 2 unique days × +₹13.1 = +₹26 (rough)
    ≈ **+₹800 expected**

Actual orchestrator: **-₹150 across ~19 trips.** So the orchestrator
isn't even matching the worst standalone child (IB +₹222), let alone
beating the best.

## Hypothesis 1: trend_daily steals the slot for 30 days at a time

`TrendDailyStrategy` is a MULTI-DAY hold strategy:

  - max_hold_days: 30
  - Donchian-breakout entry → holds for up to 30 days
  - evaluate_score returns 60-80 when in band (VIX 12-22)

Premium-selling children (IC, IB, SS) all close intraday. trend_daily
holds overnight, for weeks.

**Hypothesis:** trend_daily wins the slot on a random day, locks it
for 30 days, blocks ALL premium-sellers during that window.

Orchestrator's Phase 1 routing (line 229 of orchestrator.py):

```python
if self._active_child and self._active_child in self._children:
    active = self._children[self._active_child]
    signal = await active.on_tick(tick)
    if signal is not None and signal.signal_type == SignalType.EXIT:
        self._active_child = None
    return signal
```

While `_active_child` is set, ONLY that child sees ticks. Other children
can't even build state, can't enter, can't do anything.

If TD enters once and holds 30 days: 1 multi-day trade. Premium-sellers
lose 30 days of opportunity. Standalone, those 30 days had IC/IB/SS
trades that totaled positive.

**This is the most plausible single explanation.** Confirmation: the
diagnostic should show TD with ~1-2 fills (entry + exit) but holding
position for many days. If TD has, say, 4 fills total but locked the
slot for 60 cumulative days, that's 60 days of premium-selling
opportunity lost.

## Hypothesis 2: list-order tie-break starves IB

Iron Butterfly inherits IronCondorStrategy's score config
(`IRON_CONDOR_CONFIG`). On ANY day where both are eligible, they
score IDENTICALLY. The orchestrator's stable tie-break by
`list_position` always picks the first one — and `children` list
order is `[iron_condor, iron_butterfly, short_strangle, trend_daily]`.

So IC is tried first. If IC's `_try_entry` succeeds, IB never fires.
Standalone IB had 38 trips — orchestrator gives IB at most the
fallback path (when IC returns None due to wing-strike unavailability
or other intra-strategy filter).

IB's standalone +₹5.8/trip × 38 trips = +₹222. Most of those days
also have IC eligible. The orchestrator routes those days to IC
(+₹13.1/trip) — slight gain on per-trip EV (+13 vs +6) but only if
IC actually fires. If IC fires on a winning day, fine. If the day
favors IB (e.g., IC's wing strike unavailable, IC blocks, IB tries),
the orchestrator gets IB's PnL.

The asymmetry: IC + IB serve the SAME regime (VIX 16-22). Standalone
they trade in parallel and BOTH profit. Orchestrator only takes one.
That's a 50% reduction in opportunity in the [16,22] regime — but
shouldn't go negative just from this.

## Hypothesis 3: Legacy 0-100 score ≠ per-trip EV

SS V1 wins (+₹43.8/trip) primarily through RISK MANAGEMENT (no trail
stop, asymmetric SL=30/PT=25). Its 0-100 entry score doesn't reflect
this — the score measures regime FIT, not expected P&L.

On a 14-VIX day:

  - SS score: 25 (VIX) + 25 (range) + 25 (move) + 15 (PCR) = ~90
  - IC score: 10 (VIX, "thin premium") + 25 + 25 + 15 = ~75
  - IB score: 10 + 25 + 25 + 15 = ~75 (IB inherits IC config)
  - TD score: 60 + fit (~10) = ~70

SS should win. But IC's vix_entry_max = 22.0 and vix_entry_min = 16.0,
so IC's `_try_entry` BLOCKS at VIX=14 (wrong band). IC returns None.
Same IB. SS's _try_entry passes (VIX=14 in [13,16]).

So actually SS DOES win on its band days. So this hypothesis alone
doesn't explain the loss.

## Hypothesis 4: trend_daily uses `_position` not `_entered`

BaseStrategy's `has_open_position()` checks `self._entered`. But
trend_daily uses `self._position` (an int: 0/+1/-1) and never sets
`_entered`.

This means the V5 calibration's `_entered`-property auto-apply hook
(`base.py:_entered.setter`) doesn't fire on trend_daily's position
transitions. NOT a P&L issue, but a code-smell consistency gap.

More importantly: the orchestrator tracks the slot via its OWN
`_active_child` field, not by polling `has_open_position()`. So this
inconsistency doesn't directly cause the negative PnL.

## What the diagnostic will show

The diagnostic script (running now) groups `result['trades']` by
`strategy_id` and reports per-child:

  - fills count
  - days fired
  - sell_value - buy_value (rough net before charges)
  - first/last fire dates

If Hypothesis 1 is correct: trend_daily will show 1-4 fills with
multi-day holds visible from the day-fire pattern, and premium-sellers
will have many fewer fills than their standalone counts.

If Hypothesis 2 is correct: IB will have very few fills (it's always
losing tie-break to IC).

If Hypothesis 3 alone: child counts roughly match standalone counts
on their respective regime days; the loss comes from charges
compounding on too many trades per child.

## Pending data + decision

Diagnostic results expected ~15 min from now. Will update this doc
with the per-child breakdown + final verdict on which hypotheses are
real.

Tomorrow's deployment decision: the V4 orchestrator failed validation
regardless of the root cause. Recommend reverting to multi-shadow
deployment + SS V1 solo LIVE while V5 orchestrator (regime-aware
scoring) is validated.
