# Orchestrator V5 — Coordinator Design

May 7 2026 · branch `FnO-v5-orchestration-impl`

## 1. Why V5

The V4 orchestrator (Apr 27 2026) is a "highest-scorer wins" router: each
tick, every child strategy returns a 0-100 score from `evaluate_score()`,
the orchestrator picks the highest above a fixed threshold, and routes the
tick to that child. Once a child has a position the slot is locked until
it exits.

V4 ships TODAY in production (`orchestrator_1` on the daemon). It works,
but it's **not a coordinator** — it's a switchboard. There are four
responsibilities a coordinator should have that V4 does not:

1. **Profit maximisation across regimes.** V4's score signal is a
   heuristic over VIX bands, morning range, PCR, etc. — same scorer used
   in production for two years. It does NOT incorporate the
   regime-detector's confidence in the strategy's edge. A short-strangle
   scoring 75/100 on a sleepy Tuesday morning AND a short-strangle
   scoring 75/100 on a pre-FOMC Wednesday afternoon both look the same
   to V4. They should not.

2. **Loss prevention.** V4 has no daily-PnL circuit breaker. A
   sequence of three losing IC trades on a high-VIX gap day will let
   V4 keep entering a fourth and a fifth. The discipline is on each
   child's stop-loss — there's no portfolio-level kill switch.

3. **Capital allocation by margin yield.** Iron Butterfly costs
   ~₹1.5L/lot on NIFTY post-SEBI; Iron Condor costs ~₹2.5L/lot. At
   equal score, IB ties up 60% as much capital — for the same edge,
   it's strictly better. V4 ignores this because score is unitless.

4. **Correlation-aware position management.** Two premium-sellers
   (e.g. IC + Strangle) entering on the same morning are NOT
   independent bets — they're the same trade twice. V4's single-slot
   model accidentally handles this (only one slot), but the
   semantics aren't explicit, and any future multi-slot extension
   would re-introduce stacking risk.

V5 adds these four responsibilities as INDEPENDENTLY-FLAGGED knobs.
Default-OFF preserves V4 bit-identically; tests + smoke runs already on
record continue to reproduce.

## 2. Architecture

```
┌──────────────────────┐       ┌─────────────────────────────────────────────┐
│ RegimeDetector       │       │ OrchestratorStrategy V5                     │
│  - compute CI, VRP   │       │  on each tick:                              │
│  - compute ADX       │ ────▶ │   1. day-rollover reset (idempotent)        │
│  - confidence_for_*  │       │   2. route to active child if slot held     │
└──────────────────────┘       │   3. drawdown breaker → block?              │
            ▲                  │   4. build candidates (score + confidence)  │
            │                  │   5. cash floor → block?                    │
            │                  │   6. rank by effective score / margin       │
            │                  │   7. fall through children until one fires  │
            │                  └─────────────────────────────────────────────┘
            │                              │
            │                              ▼
   ┌────────┴────────┐         ┌─────────────────────────────────────────────┐
   │ Each strategy:  │ ────▶   │ src/strategy/coordinator.py (PURE)          │
   │  regime_family  │         │   ChildCandidate dataclass                  │
   │  evaluate_score │         │   rank_candidates(...)                      │
   │  evaluate_      │         │   cash_floor_breached(...)                  │
   │   regime_       │         │   record_pnl_change(...)                    │
   │   confidence    │         │   should_block_entries(...)                 │
   └─────────────────┘         └─────────────────────────────────────────────┘
```

### Layer 1: RegimeDetector V5 methods

`src/strategy/regime.py` — three new methods, each returns a continuous
confidence in `[0.0, 1.0]`:

```python
detector.regime_confidence_for_premium_selling("NIFTY")    # → 0.62
detector.regime_confidence_for_long_vol("NIFTY")           # → 0.18
detector.regime_confidence_for_directional_trend("NIFTY")  # → 0.04
detector.regime_confidence_snapshot("NIFTY")
# {'premium_selling': 0.62, 'long_vol': 0.18, 'directional_trend': 0.04}
```

Each is a 4th-root (or 3rd-root) GEOMETRIC mean of independently-derived
sub-factors. Geometric mean is deliberate: a single failing factor
drives confidence to zero. Arithmetic mean would let "good on 3, bad
on 1" still produce middling confidence — which is the failure mode
we want to avoid.

| Family | Factors |
|---|---|
| `premium_selling` | CI ≥ 38.2 (up to 61.8 ideal), VRP ≥ -2 (up to +2 ideal), VIX in 13-22 with peak 16-20, DoW Tue/Wed/Thu = 1.0 |
| `long_vol` | VRP ≤ +1 (down to -3 ideal), VIX in 12-25 with peak 14-20, CI mid-range (peak at 50) |
| `directional_trend` | ADX 22→28+ ramp, VIX 12-22 with peak 14-20, CI inverse of premium-selling |

Thresholds are LITERATURE-CANONICAL, not parameter-tuned (Fibonacci
38.2/61.8 for CI, Bollerslev-Tauchen-Zhou VRP=0 break-even, Wilder
ADX 20/30 conventions adjusted for Indian 5-min noise to 22/28).

### Layer 2: per-strategy declarations

Each strategy class declares one line:

```python
@register_strategy("iron_condor", IronCondorParams)
class IronCondorStrategy(BaseStrategy):
    regime_family: str = "premium_selling"
```

And each params class declares one line:

```python
class IronCondorParams(BaseStrategyParams):
    expected_margin_per_lot_lakhs: float = 2.5
```

These are the only changes per strategy. `evaluate_score()` is unchanged
(legacy 0-100 scoring preserved). `evaluate_regime_confidence()` lives
on `BaseStrategy` with a default that dispatches by family — no override
needed for the canonical roster. A strategy can override
`evaluate_regime_confidence()` for finer control if it ever needs to.

| Strategy | Family | Margin (₹L/lot) |
|---|---|---|
| iron_condor | premium_selling | 2.5 |
| iron_butterfly | premium_selling | 1.5 |
| short_strangle | premium_selling | 1.5 |
| short_straddle | premium_selling | 2.0 |
| long_calendar | long_vol | 0.6 |
| long_straddle | long_vol | 0.4 |
| trend_daily | directional_trend | 1.0 |
| trend_itm | directional_trend | 0.5 |
| trend_debit_spread | directional_trend | 0.4 |

### Layer 3: pure coordinator helpers

`src/strategy/coordinator.py` is a side-effect-free module. It owns the
ranking math:

```python
@dataclass(frozen=True)
class ChildCandidate:
    name: str
    legacy_score: int
    regime_confidence: float
    regime_family: str
    expected_margin_lakhs: float
    list_position: int

    def effective_score(self, regime_aware: bool) -> float: ...
    def margin_yield(self, regime_aware: bool) -> float: ...

def rank_candidates(...): ...
def cash_floor_breached(...) -> bool: ...
def should_block_entries(...) -> tuple[bool, str]: ...
def record_pnl_change(...) -> bool: ...  # mutates state, returns "tripped"
def reset_day(state, today) -> bool: ...
def best_family_confidence(...) -> tuple[str, float]: ...
```

Pure functions (except the obvious mutators that mutate `CoordinatorState`
in place). Unit tests can verify the math with synthetic
`ChildCandidate` objects, no strategy or feed needed.

### Layer 4: OrchestratorStrategy V5

`src/strategy/implementations/orchestrator.py` rewritten to delegate to
the coordinator. Tick flow:

```
on_tick(tick):
  1. day-rollover detect → coordinator.reset_day(state, today)
  2. if active_child: route ONLY to that child (V4 contract)
  3. coordinator.should_block_entries(state, threshold) → block?
  4. candidates = self._build_candidates()  # snapshot all children
  5. coordinator.cash_floor_breached(candidates, floor) → block?
  6. eligible = coordinator.rank_candidates(candidates, ...)
  7. for each eligible (highest first):
       result = await child.on_tick(tick)
       if result is not None: claim slot, return signal
       else: log "fallback" and try next
  8. return None (all rejected)
```

V4 single-slot semantics preserved exactly: the active-child branch is
unchanged. New branches (cash floor, drawdown breaker, correlation
guard) are early-exits in front of the existing ranking flow.

## 3. The five V5 knobs

All five are independent. Set any subset to opt in to that behaviour.

### Knob 1 — `regime_aware_scoring: bool = False`

When True:

```
effective_score = legacy_score × regime_confidence
```

A child returning legacy=80 and confidence=0.30 ranks at effective=24.
With the default `min_score_to_trade=60`, that child is filtered out
even though its legacy score would clear V4's threshold. This is the
intended behaviour — high setup score on a bad regime is a setup
that LOOKS good in isolation but the portfolio doesn't want.

When False, V4 path: effective_score = legacy_score (regime confidence
is computed but ignored).

### Knob 2 — `cash_floor_confidence: float = 0.0`

When > 0.0 AND `regime_aware_scoring=True`, the orchestrator computes
the max regime confidence across all child families. If that max is
below the floor, the orchestrator returns None — no allocation, sit
in cash.

Captures the "no edge anywhere" case: every family's regime is
unfavourable. Different from `min_score_to_trade` because it gates on
regime FITNESS, not setup quality. A high-quality setup in an
unfavourable regime should still not trigger if the underlying market
has no edge for ANY family.

Recommended starting value: 0.50. Lower (0.30) lets the orchestrator
trade more often; higher (0.70) reserves capital for clearly-favourable
days only (the long-tail strategy).

### Knob 3 — `margin_aware_selection: bool = False`

When True, eligible candidates are ranked by `effective_score / expected_margin_lakhs`
instead of raw effective score. Empirical effect on the canonical
roster:

- IC at score 70 / ₹2.5L → yield 28
- IB at score 65 / ₹1.5L → yield 43
- Strangle at score 60 / ₹1.5L → yield 40

IB wins despite a lower effective score because it yields more PnL per
unit of capital tied up. This is the Indian-quant canonical "margin
efficiency" view that post-SEBI favours: lot size went 25→75 but per-lot
margin barely budged, so capital efficiency now dominates raw signal
strength.

Off by default until the IB+SS Phase 1.5 risk-management ablation lands
the calibrated PT/SL — turning this on while SS still has -₹52/trade
just routes more capital into a known loser.

### Knob 4 — `block_correlated_families: bool = False`

When True AND `_active_child` is set, candidates whose `regime_family`
matches the active child's family are filtered out before ranking.

Today's single-slot orchestrator only ever has one active child, so this
is a no-op in practice. The value is forward-looking: when V6 lands a
multi-slot variant (orchestrator runs N independent slots in parallel),
this flag prevents the obvious mistake of letting two premium-sellers
both grab slots and double-down on the same exposure.

### Knob 5 — `daily_max_drawdown_inr: float = 0.0`

When set to a NEGATIVE rupee value (e.g. -15000.0), the orchestrator
tracks day-PnL via `on_order_update` (estimating via average_price ×
filled_quantity, signed by side). When day-PnL drops below the floor,
the breaker trips. Once tripped:

- New entries are blocked for the rest of the day
- Existing positions can still EXIT normally (the breaker only gates
  Phase 2 / 3 of `on_tick`; the active-child branch is untouched —
  blocking exits would be a footgun)
- Reset on day-rollover (`coordinator.reset_day`)

When 0.0 (default), disabled — V4 behaviour.

Note: the PnL estimate is approximate. We can't call
`ctx.get_pnl_summary()` because that interface isn't on every context
implementation. The `average_price × filled_quantity` estimate is
correct for short-premium strategies (sell credit + buy debit at exit
nets to realised PnL) and approximate elsewhere. The breaker is a
SOFT circuit, not a P&L ledger — false positives (blocking on a day
that wasn't actually a drawdown) are acceptable; false negatives
(missing a real drawdown) are not.

## 4. The math: why geometric mean

Each `regime_confidence_for_*` method computes:

```
confidence = (factor_1 × factor_2 × ... × factor_N) ** (1/N)
```

This is the GEOMETRIC mean. Compare to the obvious alternative,
arithmetic mean:

```
confidence = (factor_1 + factor_2 + ... + factor_N) / N
```

On a market where premium-selling is favourable on 3/4 factors but
unfavourable on 1 (say, VIX is too high at 24 → vix_factor=0):

- Arithmetic mean: (1.0 + 1.0 + 0.0 + 1.0) / 4 = 0.75 → still
  "favourable" by a 0.50 cash-floor → enters trade → blowup
- Geometric mean: (1.0 × 1.0 × 0.0 × 1.0) ** 0.25 = 0.0 → "no" →
  blocked

We want the second behaviour. A single structural failure (extreme
VIX, expiry day, FOMC tomorrow) should be sufficient to block, not
require three other factors to also fail. Geometric mean enforces
that.

## 5. Backward compatibility verification

V4 tests at `tests/unit/test_orchestrator.py` cover:

1. Registration with the registry
2. Construction with defaults
3. Skip unknown child names
4. Refuse to nest itself
5. Per-child param isolation (one pre-existing failure unrelated to V5)
6. Highest-score child wins
7. Below-threshold returns None
8. Active-child routing is exclusive
9. Fallback to next eligible
10. All-rejected returns None
11. Slot release on EXIT

ALL 10 of these pass with the V5 code at default flags. Confirmed
against `pytest tests/unit/test_orchestrator.py`.

V5-specific tests live at `tests/unit/test_orchestrator_v5.py` and
exercise each new knob independently. See that file for full coverage.

## 6. Operational rollout plan

The V5 orchestrator is shipped behind feature flags so production can
move gradually:

### Phase A — code lands, all flags OFF (this commit)
- Existing `orchestrator_1` config keeps default flags
- Production behaviour is bit-identical to V4
- V5 tests pass; V4 tests still pass

### Phase B — opt in regime-aware scoring on one shadow
- Add `orchestrator_v5_shadow` to `.env STRATEGIES` with
  `regime_aware_scoring=true` only
- Run alongside `orchestrator_1` for 30 days
- Compare per-tick decisions and total PnL

### Phase C — opt in cash floor + drawdown breaker
- Add `cash_floor_confidence=0.50` and `daily_max_drawdown_inr=-15000.0`
- 30 more days of shadow

### Phase D — opt in margin-aware selection
- Requires SS Phase 1.5 risk-management ablation to land first (so
  capital gets routed to a CALIBRATED IB/SS, not the current
  underperforming SS Phase 1)
- Add `margin_aware_selection=true` to the V5 shadow

### Phase E — promote shadow to champion
- Once V5 shadow demonstrates positive incremental PnL over V4 across
  all four phases, promote `orchestrator_v5_shadow` to
  `orchestrator_1`'s config and retire the old shadow

Each phase produces an A/B report comparing V5-with-flag against
V4-baseline on the same 173-day post-SEBI corpus.

## 7. Open questions / known limitations

1. **PnL estimation is approximate.** The drawdown breaker uses
   `average_price × filled_quantity`, which under-counts realised PnL
   when one side fills and the other rejects (e.g. partial IC entry).
   Mitigation: prefer publishing realised PnL from each child's EXIT
   signal in V6. Not blocking for V5.

2. **No per-strategy weight cap (yet).** `max_strategy_weight: float =
   1.0` is reserved on `OrchestratorParams` for the multi-slot V6
   extension. Not enforced today since single-slot makes "weight"
   moot.

3. **`evaluate_regime_confidence()` default returns 0.5 when
   `regime_family == "unknown"`.** Strategies that haven't declared
   a family aren't penalised, but they also don't benefit from
   regime-aware boosting. Acceptable: the canonical roster all has
   declarations now; new strategies just need one line to opt in.

4. **`expected_margin_per_lot_lakhs` defaults are calibrated
   estimates, not live SPAN+ELM lookups.** If margin requirements
   change materially (e.g. SEBI bumps ELM again) the figures need
   manual update. A future enhancement could read the broker's
   margin endpoint and override at runtime.

5. **Pre-existing test failure** at
   `test_orchestrator_per_child_params_isolated` — IB's wing_width
   default is 2 (its own override), but the test asserts 8 (IC's
   default). Not introduced by V5; fixing it is out of scope for
   this branch. File a separate task.

## 8. File index

New / modified files on this branch:

```
src/strategy/regime.py                           +200 lines   3 new methods + helper
src/strategy/coordinator.py                      NEW (220 lines)
src/strategy/implementations/orchestrator.py     rewritten (V5 + V4 contract)
src/strategy/params.py                           +OrchestratorParams V5 fields
                                                 +expected_margin_per_lot_lakhs
                                                  on each strategy params class
src/strategy/base.py                             +regime_family class attr
                                                 +evaluate_regime_confidence()
src/strategy/implementations/iron_condor.py      +regime_family
src/strategy/implementations/iron_butterfly.py   +regime_family
src/strategy/implementations/short_strangle.py   +regime_family
src/strategy/implementations/short_straddle.py   +regime_family
src/strategy/implementations/long_calendar.py    +regime_family
src/strategy/implementations/long_straddle.py    +regime_family
src/strategy/implementations/trend_daily.py      +regime_family
src/strategy/implementations/trend_itm.py        +regime_family
src/strategy/implementations/trend_debit_spread.py +regime_family
docs/ORCHESTRATOR_V5_DESIGN.md                   NEW (this file)
tests/unit/test_orchestrator_v5.py               NEW
```
