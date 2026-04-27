"""OrchestratorStrategy — registry-discovered, decoupled multi-strategy meta-controller.

Phase 3c (Apr 27 2026). Goal: combine N independently-validated child
strategies into a single book that picks the highest-scoring eligible
child per tick. Each child is regime-matched (e.g., iron_condor for
mid-VIX, long_calendar for low-VIX-with-vol-expansion) — the orchestrator
allocates the slot to whichever child has the strongest setup right now.

## Design contract (see memory/orchestrator_decoupling.md)

- **No hardcoded children.** This file imports nothing from
  ``src/strategy/implementations/``. Children are listed by registry name
  in ``OrchestratorParams.children`` and instantiated via
  ``src.strategy.registry.create_strategy``.
- **Adding a strategy is config-only.** Register the new class with
  ``@register_strategy("my_new_strategy", MyParams)`` and add the name
  to ``children``. The orchestrator picks it up automatically.
- **Per-child params fully isolated.** ``children_params`` is a dict
  keyed by child name; each child receives only its own override slice.
- **Children don't know they're orchestrated.** They behave identically
  standalone vs orchestrated. The orchestrator decides which child's
  signal actually executes; non-selected children stay quiet (their
  on_tick is simply not called this cycle).

## Selection algorithm

Each tick:

1. If a child currently holds the position (``self._active_child`` set),
   route the tick to that child only — let it manage exits.
2. Otherwise, call ``evaluate_score()`` on every child (pure, no state
   mutation). Pick the child with the highest score >= ``min_score_to_trade``.
3. Run full ``on_tick()`` on the selected child only. If it produces an
   ENTRY signal, mark it active. If it returns None despite high score
   (filters blocked), no signal goes out.

This means non-selected children never advance their entry state, so the
orchestrator avoids the stacked-position pitfall.

## Score-tying behavior

Children exposing scores 0–100 via ``evaluate_score()``:
  - iron_condor / iron_butterfly / short_strangle / short_straddle:
    rule-based 0–100 from ``score_strategy()``
  - long_calendar: 0 if filters fail, 60–80 if eligible
  - default (no scorer): 0 (orchestrator ignores)

Tie-breaking: stable sort by registered name (children list order).
"""

import logging
from datetime import date
from typing import TYPE_CHECKING

from src.core.models import Signal, Subscription, Tick
from src.core.types import SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import OrchestratorParams
from src.strategy.registry import register_strategy

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@register_strategy("orchestrator", OrchestratorParams)
class OrchestratorStrategy(BaseStrategy):
    """Registry-discovered multi-strategy orchestrator.

    Children are looked up by name at on_start; missing names log a warning
    and are skipped. Add or remove strategies via ``params.children`` only.
    """

    params: OrchestratorParams

    def __init__(self, strategy_id: str, params: OrchestratorParams):
        super().__init__(strategy_id, params)
        # Map of registry-name → child instance. Empty until on_start runs.
        self._children: dict[str, BaseStrategy] = {}
        # Name of the child currently holding a position (None = no slot active).
        self._active_child: str | None = None

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
            f"[{self.strategy_id}] orchestrator started with "
            f"{len(self._children)} children: {list(self._children.keys())}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        if not self._children:
            return None

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

        # ── Phase 2: scoring ──
        # No active child — score every candidate (pure, no state mutation)
        # and pick the highest-scoring one above the threshold.
        scores: dict[str, int] = {}
        for name, child in self._children.items():
            try:
                scores[name] = int(child.evaluate_score())
            except Exception as e:
                logger.warning(f"[{self.strategy_id}] '{name}'.evaluate_score errored: {e}")
                scores[name] = 0

        # Stable sort by score desc, then by registered position (children list order)
        order = self.params.children
        ranked = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], order.index(kv[0]) if kv[0] in order else 99),
        )
        # Filter to eligible (score >= threshold)
        eligible = [(name, s) for name, s in ranked if s >= self.params.min_score_to_trade]

        if not eligible:
            best_pair = ranked[0] if ranked else ("none", 0)
            self._log_skip_throttled(
                "ORCH_NO_CANDIDATE",
                f"[{self.strategy_id}] no child scored >= {self.params.min_score_to_trade} "
                f"(best={best_pair[0]}@{best_pair[1]})",
            )
            return None

        # ── Phase 3: route to the best eligible child, fall back to next-best
        # if it returns None (e.g., a child's hard filter blocks entry despite
        # the score being above threshold — common cause: thin chain, missing
        # wing strikes, expiry-day block). The first child to emit a non-None
        # signal wins the slot. Without this fallback, the orchestrator gets
        # stuck on the highest-scoring child even when its data conditions
        # prevent entry, and other eligible children never get a chance.
        for name, score in eligible:
            child = self._children[name]
            try:
                signal = await child.on_tick(tick)
            except Exception as e:
                logger.error(f"[{self.strategy_id}] child '{name}' on_tick errored: {e}")
                continue
            if signal is None:
                # Child rejected this tick (filter blocked, no fill, etc.) — try next eligible
                self._log_skip_throttled(
                    f"ORCH_FALLBACK_{name}",
                    f"[{self.strategy_id}] '{name}' (score={score}) returned None — trying next eligible",
                )
                continue
            # Got a real signal — claim the slot if it's an entry
            if signal.signal_type == SignalType.ENTRY:
                self._active_child = name
                logger.info(
                    f"[{self.strategy_id}] selected '{name}' (score={score}) — "
                    f"position now active under this child"
                )
            return signal

        # All eligible children returned None this tick
        self._log_skip_throttled(
            "ORCH_ALL_REJECTED",
            f"[{self.strategy_id}] all {len(eligible)} eligible children returned None "
            f"(best score was {eligible[0][1]} for {eligible[0][0]})",
        )
        return None

    def evaluate_score(self) -> int:
        """Orchestrator's own score = best child's score. Used if this
        orchestrator itself is nested inside another orchestrator (rare)."""
        if not self._children:
            return 0
        try:
            return max(c.evaluate_score() for c in self._children.values())
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
        # The children that DO hold multi-day positions (long_calendar) will
        # internally preserve _entered via their own reset_day_state.
        if self._active_child:
            # Defensive: if the active child was a same-day strategy, it'll
            # clear its own _entered in its reset_day_state. We just stop
            # forcing routing to it — Phase 2 will pick a fresh candidate.
            self._active_child = None
        for child in self._children.values():
            if hasattr(child, "reset_day_state"):
                try:
                    child.reset_day_state()
                except Exception as e:
                    logger.warning(f"[{self.strategy_id}] child reset_day_state errored: {e}")
        # Clear our own skip-log dedup
        self._last_skip_log_minute.clear()
