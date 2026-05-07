"""V5 Orchestrator coordinator helpers — composable, side-effect-free.

This module hosts the pure decision logic that the OrchestratorStrategy V5
uses to act as a coordinator across child strategies:

  1. Combine each child's setup score (0-100) and regime confidence
     (0.0-1.0) into a single effective score
  2. Cash-floor: refuse to allocate when no regime family clears the
     confidence threshold
  3. Margin-aware ranking: prefer children with higher expected
     PnL-per-rupee-of-margin
  4. Correlation guard: don't let two same-family children both fight
     for the slot on the same tick
  5. Daily drawdown circuit breaker: stop new entries once the day's
     cumulative PnL crosses a negative threshold

Keeping this logic outside ``orchestrator.py`` lets the unit tests
exercise the math directly with synthetic inputs (no need to spin up
strategy instances or fake feed objects). The orchestrator delegates
to a single ``rank_candidates`` call per tick and to ``should_block_entries``
when checking the circuit breaker.

All functions in this module are PURE: they take simple inputs (dicts,
floats), return simple outputs, and never mutate caller state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class ChildCandidate:
    """One child strategy's candidacy snapshot for a single tick.

    Filled in by the orchestrator from child.evaluate_score() +
    child.evaluate_regime_confidence() + child.regime_family +
    child.params.expected_margin_per_lot_lakhs. Frozen so the ranking
    code can't accidentally mutate it.
    """

    name: str
    legacy_score: int             # 0-100 from evaluate_score()
    regime_confidence: float      # 0.0-1.0 from evaluate_regime_confidence()
    regime_family: str            # "premium_selling" | "long_vol" | "directional_trend" | "unknown"
    expected_margin_lakhs: float  # >0; rough SPAN+ELM per lot
    list_position: int            # Original position in OrchestratorParams.children — for stable tie-break

    def effective_score(self, regime_aware: bool) -> float:
        """0-100 effective score after regime-confidence weighting.

        With regime_aware=False, returns the legacy_score unchanged
        (V4 behaviour). With regime_aware=True, returns
        ``legacy_score × regime_confidence`` so a child with a high
        setup score but a bad regime fit gets pushed down.
        """
        if not regime_aware:
            return float(self.legacy_score)
        return float(self.legacy_score) * float(self.regime_confidence)

    def margin_yield(self, regime_aware: bool) -> float:
        """Effective score per lakh of margin tied up.

        Used when ``margin_aware_selection`` is on. Higher is better:
        a child returning 60 effective score for ₹1.5L margin (yield=40)
        out-ranks a child returning 70 for ₹2.5L (yield=28). Falls
        back to ``effective_score`` when expected_margin_lakhs is
        non-positive (defensive).
        """
        eff = self.effective_score(regime_aware)
        if self.expected_margin_lakhs <= 0:
            return eff
        return eff / self.expected_margin_lakhs


@dataclass
class CoordinatorState:
    """Mutable per-orchestrator state outside the OrchestratorStrategy.

    Kept here so the strategy's __init__ stays tidy and so the unit
    tests can poke this state directly. The OrchestratorStrategy
    instance owns one CoordinatorState; all reads/writes go through
    coordinator helper functions for symmetry with the pure ranking
    code above.
    """

    # Cumulative day-PnL for circuit-breaker. Updated by orchestrator
    # via ``record_pnl_change`` whenever a child reports an EXIT; reset
    # at midnight by ``reset_day``.
    day_pnl_inr: float = 0.0
    # Last seen trading-day date (for the rollover check in reset_day).
    last_day: date | None = None
    # Has the day's drawdown breaker tripped? Once True, stays True
    # until the next reset_day.
    drawdown_tripped: bool = False
    # Names of children that have been ENTERED at least once today —
    # informational, used in correlation accounting and logs.
    families_entered_today: dict[str, int] = field(default_factory=dict)


def reset_day(state: CoordinatorState, today: date) -> bool:
    """Reset day-state on a new trading day.

    Returns True iff a reset actually happened (today differs from
    state.last_day). Idempotent within a day. Caller is responsible
    for clearing the active-child slot separately — that lives on
    the strategy, not in coordinator state.
    """
    if state.last_day == today:
        return False
    state.day_pnl_inr = 0.0
    state.drawdown_tripped = False
    state.families_entered_today.clear()
    state.last_day = today
    return True


def record_pnl_change(
    state: CoordinatorState, delta_inr: float, threshold_inr: float
) -> bool:
    """Record an exit's realised PnL and check the drawdown circuit breaker.

    Args:
      state: mutated in place
      delta_inr: realised PnL of the exit (positive = win, negative = loss)
      threshold_inr: drawdown floor (NEGATIVE number, e.g. -15000.0).
                     0.0 disables the breaker.

    Returns True if the breaker tripped on this update (caller should
    log loud). Once True, stays True for the rest of the day.
    """
    state.day_pnl_inr += float(delta_inr)
    if threshold_inr >= 0.0:
        return False  # Disabled
    if state.day_pnl_inr <= threshold_inr and not state.drawdown_tripped:
        state.drawdown_tripped = True
        return True
    return False


def should_block_entries(
    state: CoordinatorState, threshold_inr: float
) -> tuple[bool, str]:
    """Should the orchestrator block all new entries this tick?

    Currently the only blocker is the daily-drawdown circuit breaker.
    Returns (blocked, reason). Reason is a human-readable short string
    used by the orchestrator for throttled skip logs.
    """
    if threshold_inr < 0.0 and state.drawdown_tripped:
        return True, (
            f"DAILY_DRAWDOWN_TRIPPED: day_pnl=₹{state.day_pnl_inr:.0f} "
            f"<= floor=₹{threshold_inr:.0f}"
        )
    return False, ""


def cash_floor_breached(
    candidates: list[ChildCandidate], floor_confidence: float
) -> bool:
    """Are ALL regime families below the cash-floor confidence?

    When True, the orchestrator yields None for the tick — no edge in
    any family means we'd rather sit in cash. Single source-of-truth
    for the "no allocation" decision so unit tests can prove the
    behaviour independent of the strategy class.

    floor_confidence == 0.0 disables the check (returns False always).
    """
    if floor_confidence <= 0.0 or not candidates:
        return False
    # Per-family max confidence (use max because two strategies in the
    # same family share the family's regime confidence number)
    family_max: dict[str, float] = {}
    for c in candidates:
        family_max[c.regime_family] = max(
            family_max.get(c.regime_family, 0.0), c.regime_confidence
        )
    overall_max = max(family_max.values()) if family_max else 0.0
    return overall_max < floor_confidence


def rank_candidates(
    candidates: list[ChildCandidate],
    *,
    min_score_to_trade: int,
    regime_aware: bool,
    margin_aware: bool,
    block_correlated: bool,
    excluded_families: set[str] | None = None,
) -> list[tuple[ChildCandidate, float]]:
    """Rank eligible candidates highest-to-lowest by their figure of merit.

    Args:
      candidates: full slate (post-evaluate_score, post-evaluate_regime_confidence)
      min_score_to_trade: 0-100 floor on the EFFECTIVE score after
                          regime-aware weighting (0 = no floor)
      regime_aware: when True, scale legacy score by regime confidence
      margin_aware: when True, rank by score-per-lakh-of-margin
      block_correlated: when True, drop candidates whose family is in
                        excluded_families
      excluded_families: set of family names to skip (e.g. families with
                         an active position elsewhere in the orchestrator)

    Returns: list of (candidate, ranking_value) sorted DESC by ranking_value,
             then by list_position (stable tie-break — matches V4 behaviour)

    Pure function — no mutation, no logging. The orchestrator handles
    logging from the returned list.
    """
    excluded = excluded_families or set()
    eligible: list[tuple[ChildCandidate, float]] = []
    for c in candidates:
        if block_correlated and c.regime_family in excluded:
            continue
        eff = c.effective_score(regime_aware)
        if eff < min_score_to_trade:
            continue
        rank_value = c.margin_yield(regime_aware) if margin_aware else eff
        eligible.append((c, rank_value))
    # Stable sort: rank_value DESC, list_position ASC (deterministic for ties)
    eligible.sort(key=lambda pair: (-pair[1], pair[0].list_position))
    return eligible


def best_family_confidence(candidates: list[ChildCandidate]) -> tuple[str, float]:
    """Return the (family, confidence) with the highest regime confidence.

    Used by the orchestrator for decision-log breadcrumbs ("regime
    snapshot at this tick: best family was premium_selling at 0.62").
    Empty input → ("none", 0.0).
    """
    if not candidates:
        return "none", 0.0
    family_max: dict[str, float] = {}
    for c in candidates:
        family_max[c.regime_family] = max(
            family_max.get(c.regime_family, 0.0), c.regime_confidence
        )
    name, value = max(family_max.items(), key=lambda kv: kv[1])
    return name, value
