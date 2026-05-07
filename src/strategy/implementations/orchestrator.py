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
        # Name of the child currently holding a position (None = no slot active).
        self._active_child: str | None = None
        # V5 coordinator state — day PnL tracker, drawdown breaker, etc.
        self._coord = CoordinatorState()

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
                await child.on_start()
            except Exception as e:
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

    async def on_tick(self, tick: Tick) -> Signal | None:
        if not self._children:
            return None

        # ── Day-rollover ─────────────────────────────────────
        # Reset coordinator day state on the first tick of a new
        # trading day. Idempotent within a day.
        try:
            today = self.ctx.clock.now().date()
            if reset_day(self._coord, today):
                logger.info(
                    f"[{self.strategy_id}] coordinator reset for {today}: "
                    f"day_pnl=0, drawdown_breaker=cleared"
                )
        except Exception:
            pass  # No clock yet — first tick before runner wired it

        # ── Phase 1: managed-position routing ──
        # If a child currently holds the slot, route ONLY to it. The child
        # manages its own exits (profit target, stop loss, time stop). When
        # it emits an EXIT, the orchestrator releases the slot.
        if self._active_child and self._active_child in self._children:
            active = self._children[self._active_child]
            try:
                signal = await active.on_tick(tick)
            except Exception as e:
                logger.error(f"[{self.strategy_id}] active child '{self._active_child}' errored: {e}")
                return None
            if signal is not None and signal.signal_type == SignalType.EXIT:
                logger.info(f"[{self.strategy_id}] '{self._active_child}' exited — releasing slot")
                self._active_child = None
            return signal

        # ── V5 circuit breaker: daily drawdown ──
        # Cheaper than scoring children — check breaker first.
        blocked, why = should_block_entries(self._coord, self.params.daily_max_drawdown_inr)
        if blocked:
            self._log_skip_throttled(
                "ORCH_DRAWDOWN_BREAKER",
                f"[{self.strategy_id}] new entries blocked: {why}",
            )
            return None

        # ── Phase 2: build candidate snapshots ──
        candidates = self._build_candidates()

        # ── V5 cash floor: refuse when no family clears ──
        if cash_floor_breached(candidates, self.params.cash_floor_confidence):
            best_fam, best_conf = best_family_confidence(candidates)
            self._log_skip_throttled(
                "ORCH_CASH_FLOOR",
                f"[{self.strategy_id}] cash floor breached: best family={best_fam} "
                f"@ confidence={best_conf:.2f} < floor={self.params.cash_floor_confidence:.2f}",
            )
            return None

        # ── V5 correlation guard: exclude families already active ──
        # Single-slot orchestrator: at this point _active_child is None,
        # so excluded_families is empty. The hook is here for the day a
        # multi-slot variant lands.
        excluded_families: set[str] = set()
        if self.params.block_correlated_families and self._active_child:
            ac = self._children.get(self._active_child)
            if ac is not None:
                excluded_families.add(getattr(ac, "regime_family", "unknown"))

        # ── Phase 3: rank ──
        eligible = rank_candidates(
            candidates,
            min_score_to_trade=self.params.min_score_to_trade,
            regime_aware=self.params.regime_aware_scoring,
            margin_aware=self.params.margin_aware_selection,
            block_correlated=self.params.block_correlated_families,
            excluded_families=excluded_families,
        )

        if not eligible:
            best = candidates[0] if candidates else None
            best_eff = best.effective_score(self.params.regime_aware_scoring) if best else 0.0
            self._log_skip_throttled(
                "ORCH_NO_CANDIDATE",
                f"[{self.strategy_id}] no child scored >= {self.params.min_score_to_trade} "
                f"(top eff={best_eff:.1f} for {best.name if best else 'none'})",
            )
            return None

        # ── Phase 4: route to best, fall back on None (V4 fallback) ──
        for cand, rank_value in eligible:
            child = self._children.get(cand.name)
            if child is None:
                continue
            try:
                signal = await child.on_tick(tick)
            except Exception as e:
                logger.error(f"[{self.strategy_id}] child '{cand.name}' on_tick errored: {e}")
                continue
            if signal is None:
                self._log_skip_throttled(
                    f"ORCH_FALLBACK_{cand.name}",
                    f"[{self.strategy_id}] '{cand.name}' (rank={rank_value:.2f}, "
                    f"eff={cand.effective_score(self.params.regime_aware_scoring):.1f}) "
                    f"returned None — trying next eligible",
                )
                continue
            # Got a real signal — claim the slot if it's an entry
            if signal.signal_type == SignalType.ENTRY:
                self._active_child = cand.name
                fam_name, fam_conf = best_family_confidence(candidates)
                logger.info(
                    f"[{self.strategy_id}] selected '{cand.name}' "
                    f"(legacy={cand.legacy_score} regime_conf={cand.regime_confidence:.2f} "
                    f"eff={cand.effective_score(self.params.regime_aware_scoring):.1f} "
                    f"margin=₹{cand.expected_margin_lakhs:.1f}L family={cand.regime_family}) | "
                    f"best_family={fam_name}@{fam_conf:.2f}"
                )
            return signal

        # All eligible children returned None this tick
        self._log_skip_throttled(
            "ORCH_ALL_REJECTED",
            f"[{self.strategy_id}] all {len(eligible)} eligible children returned None "
            f"(top was {eligible[0][0].name})",
        )
        return None

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
        for name, child in self._children.items():
            try:
                await child.on_stop()
            except Exception as e:
                logger.warning(f"[{self.strategy_id}] '{name}'.on_stop errored: {e}")
        if self._active_child:
            logger.info(f"[{self.strategy_id}] stopped while '{self._active_child}' held an open position")

    def reset_day_state(self) -> None:
        """Cascade day-state reset to children that support it."""
        # Orchestrator's own per-day state: drop the active slot at day start.
        if self._active_child:
            self._active_child = None
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
