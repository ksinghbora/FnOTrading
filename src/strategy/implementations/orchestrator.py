"""OrchestratorStrategy V5 — coordinator across child strategies.

V5 (May 7 2026). The orchestrator is no longer just a "highest-scorer
wins" router; it is the COORDINATOR responsible for:

  1. Maximising portfolio profit by combining each child's setup score
     (0-100) with the underlying market regime's confidence in that
     child's family (premium_selling / long_vol / directional_trend)
  2. Blocking lossy trades via three layered circuit breakers:
       a. Cash floor — if no regime family clears
          ``cash_floor_confidence``, we sit in cash for the tick
       b. Daily drawdown — if cumulative day PnL drops below
          ``daily_max_drawdown_inr``, all NEW entries are blocked for
          the rest of the trading day (existing positions are still
          allowed to exit normally)
       c. Correlation guard — once a child of family F is active,
          other children of the same family are skipped on this tick
          (prevents stacking redundant exposure)
  3. Capital allocation by margin yield — when ``margin_aware_selection``
     is on, eligible children are ranked by
     ``effective_score / expected_margin_lakhs`` instead of raw
     effective score, so an IB candidate at score 60 / ₹1.5L margin
     out-ranks an IC at score 65 / ₹2.5L margin
  4. Coordination logs — each tick the orchestrator records the best
     family confidence and the slate of effective scores for
     post-mortem analysis
  5. Single-slot routing (V4 contract) — once a child has a position,
     route ALL ticks to that child until it exits. The other children
     stay quiet (their on_tick is not called this cycle)

## Backward compatibility

Every V5 enhancement is OFF by default. Setting any of these knobs to
their defaults reproduces V4 (Apr 27 2026) behaviour bit-identically:

    regime_aware_scoring   = False
    cash_floor_confidence  = 0.0
    margin_aware_selection = False
    block_correlated_families = False
    daily_max_drawdown_inr = 0.0

Existing tests (tests/unit/test_orchestrator.py) cover the V4 path and
are NOT modified — they continue to pass with the V5 code at default
flags. New tests live in tests/unit/test_orchestrator_v5.py and exercise
each V5 knob in isolation.

## Selection algorithm (V5)

Per tick, when no slot is held:

  1. Build a list of ChildCandidate snapshots: (name, legacy_score,
     regime_confidence, family, margin_lakhs, list_position) — one per
     registered child
  2. (V5) If cash_floor_confidence > 0 and best family confidence < floor,
     return None (no allocation this tick)
  3. (V5) If daily_max_drawdown_inr is set and the breaker has tripped
     today, return None (no NEW entries today)
  4. Rank eligible candidates via coordinator.rank_candidates with the
     active flags. Ranking key:
        - margin_aware_selection=False: effective_score
        - margin_aware_selection=True : effective_score / margin_lakhs
     where effective_score = legacy × regime_confidence (V5) or
     just legacy (V4)
  5. Try the top-ranked child's on_tick. If it returns None (filter
     blocked), fall through to the next eligible (V4 fallback path).
     First non-None signal wins the slot.

## Coordinator state lifecycle

  - on_start: instantiate children, init coordinator state at day=None
  - on_tick: route to active child OR rank+select OR drawdown-block.
             Day-rollover detection happens here on the FIRST tick of
             the new day (day-state reset is idempotent).
  - on_order_update: when a child's EXIT is filled, the orchestrator
                     records realised PnL via coordinator.record_pnl_change.
                     If that trips the drawdown breaker, the breaker stays
                     up for the rest of the day.
  - reset_day_state: clears active slot AND coordinator day_pnl/breaker.

## Correlation guard semantics

When ``block_correlated_families=True`` AND a child of family F is the
active slot-holder, no other child of family F gets routed to even on
the OFF-position score path (they're filtered out before ranking).
This is a no-op when the orchestrator is single-slot (only one child
active at a time anyway), but matters for future multi-slot extensions
and serves as documentation of the design contract.

## Why this lives in a strategy, not the runner

Keeping coordination ONE LAYER above the children — but inside the
strategy framework — preserves the runner's simplicity (it sees one
strategy, not N) while keeping the orchestrator testable in isolation.
The pure ranking math lives in src/strategy/coordinator.py so unit
tests can verify the logic with synthetic ChildCandidate objects.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.core.models import Order, Signal, Subscription, Tick
from src.core.types import SignalType
from src.strategy.base import BaseStrategy
from src.strategy.coordinator import (
    ChildCandidate,
    CoordinatorState,
    best_family_confidence,
    cash_floor_breached,
    rank_candidates,
    record_pnl_change,
    reset_day,
    should_block_entries,
)
from src.strategy.params import OrchestratorParams
from src.strategy.registry import register_strategy

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@register_strategy("orchestrator", OrchestratorParams)
class OrchestratorStrategy(BaseStrategy):
    """V5 coordinator across child strategies.

    Children are looked up by name at on_start; missing names log a warning
    and are skipped. Add/remove strategies via ``params.children`` only.
    All V5 coordinator behaviours (regime-aware scoring, cash floor,
    margin-aware ranking, correlation guard, drawdown breaker) are
    independent flags on OrchestratorParams; default OFF for backward
    compatibility with V4 tests and prior backtests.
    """

    params: OrchestratorParams
    # The orchestrator itself doesn't belong to a regime family — it
    # ROUTES across families. Marked "unknown" so evaluate_regime_confidence
    # returns the neutral 0.5 if this orchestrator ever gets nested.
    regime_family: str = "unknown"

    def __init__(self, strategy_id: str, params: OrchestratorParams):
        super().__init__(strategy_id, params)
        # Map of registry-name → child instance. Empty until on_start runs.
        self._children: dict[str, BaseStrategy] = {}
        # V6 (May 8 2026): set of child names currently holding positions.
        # Single-slot mode (max_concurrent_slots=1) keeps this set ≤ 1
        # element, preserving V4/V5 semantics. Multi-slot mode allows up
        # to ``max_concurrent_slots`` children to hold positions
        # concurrently. The legacy ``_active_child`` property below
        # returns the FIRST active child for backward-compat reads.
        self._active_children: set[str] = set()
        # V5 coordinator state — day PnL tracker, drawdown breaker, etc.
        self._coord = CoordinatorState()

    @property
    def _active_child(self) -> str | None:
        """Backward-compat single-slot accessor.

        Returns the first child in ``_active_children`` (deterministic
        via insertion order in CPython 3.7+ dict-backed sets — actually
        sets aren't ordered, so we sort by registered position).
        """
        if not self._active_children:
            return None
        # Stable order: pick by ``params.children`` list position
        order = self.params.children
        for name in order:
            if name in self._active_children:
                return name
        # Fallback: arbitrary one
        return next(iter(self._active_children))

    @_active_child.setter
    def _active_child(self, value: str | None) -> None:
        """Backward-compat setter.

        Setting to None clears all active children (V4/V5 single-slot
        contract). Setting to a string clears all and sets that one
        active. Multi-slot code should use ``_active_children`` directly.
        """
        if value is None:
            self._active_children.clear()
        else:
            self._active_children = {value}

    def get_subscriptions(self) -> Subscription:
        # Aggregate child subscriptions. Done after children exist; for
        # simplicity we return an empty subscription here and rely on the
        # children's individual subscriptions via the runner's tick fan-out.
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        """Instantiate each child via the registry. Skip unknown names."""
        # Lazy import to avoid circular: registry imports BaseStrategy, and
        # this file is BaseStrategy-coupled but registry-decoupled by name.
        from src.strategy.registry import _PARAMS_REGISTRY, _REGISTRY

        for name in self.params.children:
            if name not in _REGISTRY:
                logger.warning(
                    f"[{self.strategy_id}] child '{name}' not in registry — skipping. "
                    f"Available: {sorted(_REGISTRY.keys())}"
                )
                continue
            if name == "orchestrator":
                # Defensive: never recursively orchestrate.
                logger.warning(f"[{self.strategy_id}] refusing to nest orchestrator inside itself")
                continue
            child_cls = _REGISTRY[name]
            params_cls = _PARAMS_REGISTRY[name]
            child_id = f"{self.strategy_id}/{name}"
            overrides = self.params.children_params.get(name, {}) or {}
            try:
                child_params = params_cls(**overrides)
            except Exception as e:
                logger.error(
                    f"[{self.strategy_id}] child '{name}' param construction failed "
                    f"(overrides={overrides}): {e} — skipping"
                )
                continue
            child = child_cls(strategy_id=child_id, params=child_params)
            child.set_context(self.ctx)
            try:
                # Mirror StrategyRunner.add_strategy state transitions so
                # the API's /api/strategies endpoint reports the child's
                # actual state (RUNNING) instead of the IDLE default.
                from src.core.types import StrategyState
                child.state = StrategyState.STARTING
                await child.on_start()
                child.state = StrategyState.RUNNING
            except Exception as e:
                from src.core.types import StrategyState
                child.state = StrategyState.ERROR
                logger.error(f"[{self.strategy_id}] child '{name}' on_start failed: {e} — skipping")
                continue
            self._children[name] = child

        logger.info(
            f"[{self.strategy_id}] V5 orchestrator started with "
            f"{len(self._children)} children: {list(self._children.keys())} | "
            f"flags: regime_aware={self.params.regime_aware_scoring} "
            f"cash_floor={self.params.cash_floor_confidence} "
            f"margin_aware={self.params.margin_aware_selection} "
            f"block_corr={self.params.block_correlated_families} "
            f"max_dd={self.params.daily_max_drawdown_inr}"
        )

    # ─── Tick routing ────────────────────────────────────────────

    async def on_tick(self, tick: Tick) -> "Signal | list[Signal] | None":
        """V6 multi-slot tick routing.

        Returns Signal | list[Signal] | None. The runner / backtest
        engine normalize all three shapes. Multiple signals are
        emitted on the same tick when multiple children act
        simultaneously (e.g. IC EXIT + SS ENTRY on the same minute).

        Phases:
          0. Day-rollover detect (reset coordinator state)
          1. Route tick to ALL active children → collect their signals,
             release slots that emitted EXIT
          2. Daily-drawdown check (skip new entries if tripped)
          3. Cash-floor check (skip if no regime family clears)
          4. Build + rank candidates (excluding already-active children
             and excluded families)
          5. Try to fill remaining slots up to max_concurrent_slots
             (and capital cap if set), collecting ENTRY signals
        """
        if not self._children:
            return None

        # ── Phase 0: Day-rollover ─────────────────────────────────
        try:
            today = self.ctx.clock.now().date()
            if reset_day(self._coord, today):
                logger.info(
                    f"[{self.strategy_id}] coordinator reset for {today}: "
                    f"day_pnl=0, drawdown_breaker=cleared"
                )
        except Exception:
            pass  # No clock yet — first tick before runner wired it

        # ── Per-minute heartbeat: log what's actually happening ──
        # Throttled summary line so the operator can `tail -f` and see
        # which children are active each minute, without scrolling
        # through every per-tick "best family" log.
        self._log_skip_throttled(
            "ORCH_HEARTBEAT",
            f"[{self.strategy_id}] active={sorted(self._active_children) or 'none'} "
            f"({len(self._active_children)}/{self.params.max_concurrent_slots} slots)",
        )

        emitted: list = []  # signals to return at end of tick

        # ── Phase 1: poll active children (for exits + intra-trade signals) ──
        # Each active child gets the tick; if it emits a signal, capture
        # it. EXIT signals release the slot.
        for child_name in list(self._active_children):
            child = self._children.get(child_name)
            if child is None:
                # Stale entry — child was removed somehow; clean up
                self._active_children.discard(child_name)
                continue
            try:
                result = await child.on_tick(tick)
            except Exception as e:
                logger.error(
                    f"[{self.strategy_id}] active child '{child_name}' errored: {e}"
                )
                continue
            # result may itself be Signal | list[Signal] | None (defensive)
            child_signals = self._normalize_child_signals(result)
            for signal in child_signals:
                if signal.signal_type == SignalType.EXIT:
                    self._active_children.discard(child_name)
                    logger.info(
                        f"[{self.strategy_id}] '{child_name}' exited — slot released "
                        f"(active now: {sorted(self._active_children)})"
                    )
                emitted.append(signal)

        # ── Phase 2: daily-drawdown circuit breaker ──
        blocked, why = should_block_entries(self._coord, self.params.daily_max_drawdown_inr)
        if blocked:
            self._log_skip_throttled(
                "ORCH_DRAWDOWN_BREAKER",
                f"[{self.strategy_id}] new entries blocked: {why}",
            )
            return _flatten_emitted(emitted)

        # Capacity check: if all slots full, no point ranking new entries
        free_slots = max(0, self.params.max_concurrent_slots - len(self._active_children))
        if free_slots == 0:
            return _flatten_emitted(emitted)

        # ── Phase 3: build candidate snapshots ──
        candidates = self._build_candidates()

        # Cash-floor check
        if cash_floor_breached(candidates, self.params.cash_floor_confidence):
            best_fam, best_conf = best_family_confidence(candidates)
            self._log_skip_throttled(
                "ORCH_CASH_FLOOR",
                f"[{self.strategy_id}] cash floor breached: best family={best_fam} "
                f"@ confidence={best_conf:.2f} < floor={self.params.cash_floor_confidence:.2f}",
            )
            return _flatten_emitted(emitted)

        # ── Phase 4: rank candidates excluding already-active and correlated ──
        excluded_families: set[str] = set()
        if self.params.block_correlated_families:
            for active_name in self._active_children:
                ac = self._children.get(active_name)
                if ac is not None:
                    excluded_families.add(getattr(ac, "regime_family", "unknown"))

        eligible = rank_candidates(
            candidates,
            min_score_to_trade=self.params.min_score_to_trade,
            regime_aware=self.params.regime_aware_scoring,
            margin_aware=self.params.margin_aware_selection,
            block_correlated=self.params.block_correlated_families,
            excluded_families=excluded_families,
        )
        # Filter out already-active children — they were polled in Phase 1
        eligible = [
            (cand, rv) for cand, rv in eligible
            if cand.name not in self._active_children
        ]

        if not eligible:
            best = candidates[0] if candidates else None
            best_eff = best.effective_score(self.params.regime_aware_scoring) if best else 0.0
            self._log_skip_throttled(
                "ORCH_NO_CANDIDATE",
                f"[{self.strategy_id}] no inactive child scored >= {self.params.min_score_to_trade} "
                f"(top eff={best_eff:.1f} for {best.name if best else 'none'}, "
                f"active: {sorted(self._active_children)})",
            )
            return _flatten_emitted(emitted)

        # ── Phase 5: try to fill remaining slots ──
        # Track current margin commitment (sum across active children)
        active_margin = sum(
            float(getattr(self._children[name].params, "expected_margin_per_lot_lakhs", 2.0))
            for name in self._active_children
            if name in self._children
        )
        slots_filled = 0
        for cand, rank_value in eligible:
            if slots_filled >= free_slots:
                break
            # In-tick correlation re-check: if an earlier slot fill in
            # this same tick already added a candidate of this family,
            # block now. ``excluded_families`` was pre-computed at the
            # start of the tick (already-active families), but we mutate
            # it inside this loop on each ENTRY so subsequent same-family
            # candidates get filtered.
            if (self.params.block_correlated_families
                    and cand.regime_family in excluded_families):
                self._log_skip_throttled(
                    f"ORCH_CORR_GUARD_{cand.name}",
                    f"[{self.strategy_id}] '{cand.name}' skipped: "
                    f"family {cand.regime_family!r} already filled this tick",
                )
                continue
            # Capital cap: skip if adding this child would exceed budget
            if self.params.max_total_margin_lakhs > 0.0:
                if active_margin + cand.expected_margin_lakhs > self.params.max_total_margin_lakhs:
                    self._log_skip_throttled(
                        f"ORCH_MARGIN_CAP_{cand.name}",
                        f"[{self.strategy_id}] '{cand.name}' skipped: "
                        f"active_margin=₹{active_margin:.1f}L + "
                        f"₹{cand.expected_margin_lakhs:.1f}L > "
                        f"₹{self.params.max_total_margin_lakhs:.1f}L cap",
                    )
                    continue

            child = self._children.get(cand.name)
            if child is None:
                continue
            try:
                result = await child.on_tick(tick)
            except Exception as e:
                logger.error(f"[{self.strategy_id}] child '{cand.name}' on_tick errored: {e}")
                continue
            child_signals = self._normalize_child_signals(result)
            if not child_signals:
                self._log_skip_throttled(
                    f"ORCH_FALLBACK_{cand.name}",
                    f"[{self.strategy_id}] '{cand.name}' (rank={rank_value:.2f}, "
                    f"eff={cand.effective_score(self.params.regime_aware_scoring):.1f}) "
                    f"returned None — trying next eligible",
                )
                continue
            for signal in child_signals:
                if signal.signal_type == SignalType.ENTRY:
                    self._active_children.add(cand.name)
                    active_margin += cand.expected_margin_lakhs
                    slots_filled += 1
                    fam_name, fam_conf = best_family_confidence(candidates)
                    logger.info(
                        f"[{self.strategy_id}] activated '{cand.name}' "
                        f"(legacy={cand.legacy_score} regime_conf={cand.regime_confidence:.2f} "
                        f"eff={cand.effective_score(self.params.regime_aware_scoring):.1f} "
                        f"margin=₹{cand.expected_margin_lakhs:.1f}L family={cand.regime_family}) | "
                        f"slots {len(self._active_children)}/{self.params.max_concurrent_slots} | "
                        f"active_margin=₹{active_margin:.1f}L"
                    )
                    # In multi-slot mode, also exclude this candidate's
                    # family from the rest of this tick's slot fills.
                    if self.params.block_correlated_families:
                        excluded_families.add(cand.regime_family)
                emitted.append(signal)
            # Stop scanning if we've filled all free slots
            if slots_filled >= free_slots:
                break

        # All eligible children scanned this tick
        if not emitted and slots_filled == 0 and eligible:
            self._log_skip_throttled(
                "ORCH_ALL_REJECTED",
                f"[{self.strategy_id}] all {len(eligible)} eligible children returned None "
                f"(top was {eligible[0][0].name})",
            )
        return _flatten_emitted(emitted)

    # ─── Multi-slot helpers ────────────────────────────────────────

    @staticmethod
    def _normalize_child_signals(result) -> list:
        """Children's on_tick may return Signal | list[Signal] | None.

        Defensive normalization for the multi-slot routing — most
        existing children still return a single Signal, but the API is
        now permissive. Returns a list of non-None Signal objects.
        """
        if result is None:
            return []
        if isinstance(result, list):
            return [s for s in result if s is not None]
        return [result]

    # ─── Coordinator helpers ──────────────────────────────────────

    def _build_candidates(self) -> list[ChildCandidate]:
        """Snapshot every child's evaluate_score + evaluate_regime_confidence.

        Pure data collection — no mutation of child or orchestrator state.
        Wraps each child call in try/except so a single broken child can't
        prevent the orchestrator from picking among the others.
        """
        order = self.params.children
        candidates: list[ChildCandidate] = []
        for idx, name in enumerate(order):
            child = self._children.get(name)
            if child is None:
                continue
            try:
                legacy = int(child.evaluate_score())
            except Exception as e:
                logger.warning(f"[{self.strategy_id}] '{name}'.evaluate_score errored: {e}")
                legacy = 0
            try:
                conf = float(child.evaluate_regime_confidence())
            except Exception as e:
                logger.warning(f"[{self.strategy_id}] '{name}'.evaluate_regime_confidence errored: {e}")
                conf = 0.5
            family = getattr(child, "regime_family", "unknown") or "unknown"
            margin = float(getattr(child.params, "expected_margin_per_lot_lakhs", 2.0) or 2.0)
            if margin <= 0.0:
                margin = 2.0  # Defensive — no division by zero in coordinator
            candidates.append(ChildCandidate(
                name=name,
                legacy_score=legacy,
                regime_confidence=conf,
                regime_family=family,
                expected_margin_lakhs=margin,
                list_position=idx,
            ))
        return candidates

    # ─── Order-update bridge: PnL tracking for drawdown breaker ──

    async def on_order_update(self, order: Order) -> None:
        """Forward fill notifications to the active child AND track PnL.

        V5 adds drawdown tracking. We can't call ctx.get_pnl_summary()
        (might not exist on every context impl), so we estimate the PnL
        impact from the order itself in the simplest robust way:
        average_price × qty for SELL minus average_price × qty for BUY,
        all signed. The active child also sees the order — this method
        does NOT consume the order, it just observes.

        Future enhancement: have children publish realised PnL on their
        EXIT signal and let the orchestrator read that directly. For
        now this is good enough to fire the breaker on a clearly bad
        day without requiring child-strategy changes.
        """
        # Forward to the active child (and ALL children — match prior
        # behaviour where each child sees its own order updates routed
        # by strategy_id prefix).
        for name, child in self._children.items():
            try:
                if order.strategy_id and order.strategy_id.startswith(child.strategy_id):
                    await child.on_order_update(order)
            except Exception as e:
                logger.debug(f"[{self.strategy_id}] '{name}'.on_order_update errored: {e}")

        # Drawdown PnL accounting — simple estimate
        if self.params.daily_max_drawdown_inr >= 0.0:
            return  # Disabled
        try:
            avg = float(getattr(order, "average_price", 0.0) or 0.0)
            qty = int(getattr(order, "filled_quantity", 0) or 0)
            side = getattr(order, "side", None)
            # Convention: SELL adds rupees, BUY subtracts. This matches
            # short-premium accounting (sell credit, buy debit) but
            # is only an APPROXIMATION when futures or stock are
            # involved. The breaker is a soft circuit, not a P&L
            # ledger — false positives are acceptable, false negatives
            # (missing a real drawdown) are not.
            if side is not None and avg > 0 and qty > 0:
                signed = (avg * qty) if str(side).upper().endswith("SELL") else -(avg * qty)
                tripped = record_pnl_change(
                    self._coord, signed, self.params.daily_max_drawdown_inr,
                )
                if tripped:
                    logger.warning(
                        f"[{self.strategy_id}] DAILY DRAWDOWN BREAKER TRIPPED: "
                        f"day_pnl=₹{self._coord.day_pnl_inr:.0f} "
                        f"<= floor=₹{self.params.daily_max_drawdown_inr:.0f}. "
                        f"New entries blocked for the rest of the day."
                    )
        except Exception as e:
            logger.debug(f"[{self.strategy_id}] drawdown accounting failed: {e}")

    # ─── Score + on_stop + reset_day_state ────────────────────────

    def evaluate_score(self) -> int:
        """Orchestrator's own score = best child's effective score.

        Used if this orchestrator itself is nested inside another
        orchestrator (rare). When V5 regime-aware mode is on, returns
        ``max(legacy × confidence)`` to keep the same score units;
        otherwise returns ``max(legacy_score)`` as in V4.
        """
        if not self._children:
            return 0
        try:
            cands = self._build_candidates()
            if not cands:
                return 0
            scores = [c.effective_score(self.params.regime_aware_scoring) for c in cands]
            return int(max(scores))
        except Exception:
            return 0

    async def on_stop(self) -> None:
        """Stop all children cleanly."""
        from src.core.types import StrategyState
        for name, child in self._children.items():
            try:
                child.state = StrategyState.STOPPING
                await child.on_stop()
                child.state = StrategyState.STOPPED
            except Exception as e:
                child.state = StrategyState.ERROR
                logger.warning(f"[{self.strategy_id}] '{name}'.on_stop errored: {e}")
        if self._active_children:
            logger.info(
                f"[{self.strategy_id}] stopped while {sorted(self._active_children)} "
                f"held open positions"
            )

    def reset_day_state(self) -> None:
        """Cascade day-state reset to children that support it."""
        # Orchestrator's own per-day state: drop ALL active slots at day start.
        # Multi-slot V6: clear the whole set, not just the first child.
        if self._active_children:
            self._active_children.clear()
        for child in self._children.values():
            if hasattr(child, "reset_day_state"):
                try:
                    child.reset_day_state()
                except Exception as e:
                    logger.warning(f"[{self.strategy_id}] child reset_day_state errored: {e}")
        # Reset coordinator day state explicitly — even if reset_day was
        # never called via on_tick (e.g. boot at end-of-day, no live
        # ticks, then reset_day_state called by runner)
        self._coord = CoordinatorState()
        # Clear our own skip-log dedup
        self._last_skip_log_minute.clear()


# ─── Module-level helpers ──────────────────────────────────────────


def _flatten_emitted(emitted: list):
    """Convert the per-tick emitted-signals list to the runner's expected return shape.

    - 0 signals → None
    - 1 signal  → that single Signal (preserves V4/V5 single-signal API)
    - 2+ signals → list[Signal] (V6 multi-slot)

    The runner / backtest engine handle all three shapes via
    ``_normalize_signals`` (runner.py) and the inline normalization in
    backtest engine. Returning the simplest-possible shape avoids
    perturbing existing tests that compare ``await orchestrator.on_tick(...)
    is sample_signal``.
    """
    if not emitted:
        return None
    if len(emitted) == 1:
        return emitted[0]
    return list(emitted)
