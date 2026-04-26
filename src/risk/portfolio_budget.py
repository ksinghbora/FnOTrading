"""Portfolio gamma × 1%-spot PnL budget.

Problem
-------
The per-leg stop-losses inside each strategy don't talk to each other. When an
IC + trend-leg + naked-premium stack is live at the same time, aggregate gamma
compounds: a 1% spot move produces a large P&L swing with no circuit to cap it.

Greeks risk monitor already guards scalar gamma/delta/theta/vega limits, but
those ceilings are picked in abstract units and don't translate cleanly to
"what does a 1% adverse move do to the book right now?".

Budget model (approximate)
--------------------------
    pnl_for_1pct_spot_move ≈ 0.5 × total_gamma × (spot × 0.01)^2

This is the second-order P&L contribution from a 1% underlying move,
assuming a delta-hedged or balanced book. It's an **upper bound proxy**:
positive gamma = long (favorable), negative gamma = short (adverse); the
absolute value is what we budget. We deliberately do not include the delta×dS
first-order term — the goal is to bound **convexity risk from short
option inventory** (IC bodies, straddle/strangle shorts, trend long-vol
legs combined), which is the part that compounds and is hard to stop out
of mid-swing.

Budget is expressed as a fraction of capital. Default 1% of capital means
"a 1% adverse spot move is capped at 1% of account equity in Greeks-only
convexity PnL". The manager rejects new orders whose incremental gamma
would exceed the remaining budget; the periodic monitor flattens the
largest-gamma position if utilization blows through 120%.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from src.core.models import OptionChain, Position

logger = logging.getLogger(__name__)


# ─── Config ─────────────────────────────────────────────────────────────


@dataclass
class GammaBudgetConfig:
    """Budget parameters. Wire via src/config.py settings."""

    # Cap on dollar_gamma_1pct PnL as fraction of capital. 0.01 = 1% of capital
    # per 1% adverse spot move.
    max_gamma_1pct_pnl_pct_of_capital: float = 0.01

    # Capital base for the budget. Set from settings.max_day_loss-derived
    # account capital (PaperBrokerClient default is 1_000_000).
    capital: float = 1_000_000.0

    # Emergency flatten trigger — utilization above this triggers kill-switch
    # flatten of the largest-gamma position.
    emergency_utilization: float = 1.2


# ─── Exposure snapshot ──────────────────────────────────────────────────


@dataclass
class GammaExposure:
    """Current portfolio gamma exposure, evaluated at latest spot."""

    total_gamma: float = 0.0          # Signed sum across positions
    abs_gamma: float = 0.0            # abs(total_gamma); the quantity budgeted
    spot: float = 0.0
    dollar_gamma_1pct: float = 0.0    # Approx |P&L| for a 1% spot move
    utilization: float = 0.0          # dollar_gamma_1pct / budget (0-1+)
    budget: float = 0.0               # capital × max_gamma_1pct_pnl_pct
    breaches: list[str] = None        # Non-empty when utilization > 1.0
    per_position: list[dict] = None   # Debug / logging breakdown

    def __post_init__(self):
        if self.breaches is None:
            self.breaches = []
        if self.per_position is None:
            self.per_position = []


# ─── Budget manager ─────────────────────────────────────────────────────


class PortfolioGammaBudget:
    """Aggregate gamma × 1%-spot PnL budget across all open positions.

    Usage
    -----
    ``current_exposure(positions, chain_builder)`` — snapshot for logging
    or emergency flatten.
    ``can_open(additional_gamma_1pct_pnl)`` — pre-trade gate. Called from
    ``RiskManager.validate_order()`` with the incremental convexity that
    would be added by the proposed position.
    """

    def __init__(self, config: GammaBudgetConfig | None = None):
        self._config = config or GammaBudgetConfig()
        self._last_exposure = GammaExposure(budget=self.budget)

    @property
    def config(self) -> GammaBudgetConfig:
        return self._config

    @property
    def budget(self) -> float:
        """Absolute PnL budget in rupees."""
        return self._config.capital * self._config.max_gamma_1pct_pnl_pct_of_capital

    @property
    def last_exposure(self) -> GammaExposure:
        return self._last_exposure

    # ─── Core math ──────────────────────────────────────────────────────

    @staticmethod
    def _dollar_gamma_1pct(total_gamma: float, spot: float) -> float:
        """Approximate |PnL| from a 1% spot move with only convexity.

        0.5 × gamma × dS² with dS = spot × 0.01. Uses |gamma| because short
        inventory is the case we budget against.
        """
        if spot <= 0:
            return 0.0
        ds = spot * 0.01
        return 0.5 * abs(total_gamma) * ds * ds

    # ─── Current exposure ───────────────────────────────────────────────

    def current_exposure(
        self,
        positions: list[Position],
        chain_builder=None,
        spot: float | None = None,
    ) -> GammaExposure:
        """Compute aggregate gamma × 1%-spot PnL from open positions.

        Uses each position's cached greeks.gamma (already populated by
        PortfolioManager on tick). Falls back to chain_builder lookup if a
        position's greeks are stale.

        Args:
            positions: Open positions with quantity + greeks.
            chain_builder: Optional OptionChainBuilder for live spot/greeks.
            spot: Override spot price (for tests / explicit callers).
        """
        # Resolve spot
        spot_val = float(spot) if spot is not None else 0.0
        if spot_val <= 0 and chain_builder is not None:
            # Fish for any available underlying price — positions share one
            # spot for NIFTY-only trading. If the book ever spans multiple
            # underlyings we'd want per-underlying budgets.
            for underlying, price in getattr(chain_builder, "_spot_prices", {}).items():
                if float(price) > 0:
                    spot_val = float(price)
                    break

        total_gamma = 0.0
        per_position: list[dict] = []

        for pos in positions:
            if pos.quantity == 0:
                continue

            # Prefer cached greeks on the Position (refreshed each tick by
            # PortfolioManager._update_greeks). Fall back to chain lookup.
            gamma = float(pos.greeks.gamma or 0.0)

            if gamma == 0.0 and chain_builder is not None:
                gamma = self._lookup_gamma(pos, chain_builder)

            # Signed contribution: long option = +gamma, short = -gamma.
            # Position.quantity carries the sign (positive=long, negative=short).
            pos_gamma = gamma * pos.quantity
            total_gamma += pos_gamma

            per_position.append({
                "tradingsymbol": pos.tradingsymbol,
                "quantity": pos.quantity,
                "gamma_per_share": gamma,
                "pos_gamma": pos_gamma,
            })

        dollar_gamma_1pct = self._dollar_gamma_1pct(total_gamma, spot_val)
        budget = self.budget
        utilization = (dollar_gamma_1pct / budget) if budget > 0 else 0.0

        breaches: list[str] = []
        if utilization > 1.0:
            breaches.append(
                f"dollar_gamma_1pct={dollar_gamma_1pct:,.0f} > budget={budget:,.0f} "
                f"(util={utilization:.2f})"
            )

        exposure = GammaExposure(
            total_gamma=round(total_gamma, 4),
            abs_gamma=round(abs(total_gamma), 4),
            spot=round(spot_val, 2),
            dollar_gamma_1pct=round(dollar_gamma_1pct, 2),
            utilization=round(utilization, 4),
            budget=round(budget, 2),
            breaches=breaches,
            per_position=per_position,
        )
        self._last_exposure = exposure
        return exposure

    # ─── Pre-trade gate ─────────────────────────────────────────────────

    def can_open(
        self,
        additional_gamma_1pct_pnl: float,
        current_exposure: GammaExposure | None = None,
    ) -> tuple[bool, str | None]:
        """Pre-trade gate.

        Args:
            additional_gamma_1pct_pnl: Incremental |PnL-for-1%-move| that
                would be added by the proposed order. Callers compute this
                using `estimate_incremental_dollar_gamma`.
            current_exposure: Optional pre-computed snapshot; falls back
                to ``last_exposure``.

        Returns:
            (allowed, reason_if_blocked)
        """
        budget = self.budget
        if budget <= 0:
            return True, None

        current = current_exposure or self._last_exposure
        current_pnl = current.dollar_gamma_1pct if current else 0.0
        projected = current_pnl + max(0.0, additional_gamma_1pct_pnl)

        if projected > budget:
            msg = (
                f"add={additional_gamma_1pct_pnl:,.0f} current={current_pnl:,.0f} "
                f"projected={projected:,.0f} > budget={budget:,.0f}"
            )
            return False, msg
        return True, None

    # ─── Emergency flatten helper ───────────────────────────────────────

    def largest_gamma_position(
        self, positions: list[Position], chain_builder=None,
    ) -> Position | None:
        """Return the single position with the largest |pos_gamma|.

        Used by the greeks monitor to target an emergency flatten when
        utilization blows through ``emergency_utilization``. Returns None
        if nothing is open.
        """
        if not positions:
            return None

        best: Position | None = None
        best_mag = 0.0
        for pos in positions:
            if pos.quantity == 0:
                continue
            gamma = float(pos.greeks.gamma or 0.0)
            if gamma == 0.0 and chain_builder is not None:
                gamma = self._lookup_gamma(pos, chain_builder)
            mag = abs(gamma * pos.quantity)
            if mag > best_mag:
                best_mag = mag
                best = pos
        return best

    # ─── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _lookup_gamma(pos: Position, chain_builder) -> float:
        """Best-effort gamma lookup for a position whose cached greeks are zero.

        Walks the registered option chain. Returns 0.0 on miss; callers treat
        that as "no convexity contribution" (safe fallback).
        """
        try:
            token_map = getattr(chain_builder, "_token_map", {})
            mapping = token_map.get(pos.instrument_token)
            if mapping is None:
                return 0.0
            underlying, expiry, strike, option_type = mapping
            chain: OptionChain | None = chain_builder.get_chain(underlying, expiry)
            if chain is None:
                return 0.0
            for entry in chain.strikes:
                if entry.strike != strike:
                    continue
                opt = entry.ce if str(option_type).endswith("CE") else entry.pe
                if opt is None:
                    return 0.0
                return float(opt.greeks.gamma or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0
        return 0.0

    def estimate_incremental_dollar_gamma(
        self,
        order,
        chain_builder=None,
        spot: float | None = None,
    ) -> float:
        """Estimate the incremental dollar_gamma_1pct for a proposed order.

        Looks up the option's per-share gamma via the chain builder, multiplies
        by order quantity, and applies the same 0.5 × Γ × (S×1%)² formula as
        current_exposure(). When the option has no entry (unregistered) returns
        0.0 — don't block orders for missing quotes.

        Note: this is *magnitude*, not signed. A short strangle *reduces* net
        gamma if the book was previously long, which could actually free
        budget. For the first pass we treat incremental |gamma| as always
        additive — i.e. assume worst case "adds to current exposure" — which
        keeps the gate conservative.
        """
        spot_val = float(spot) if spot is not None else self._last_exposure.spot
        if spot_val <= 0 and chain_builder is not None:
            for _underlying, price in getattr(chain_builder, "_spot_prices", {}).items():
                if float(price) > 0:
                    spot_val = float(price)
                    break
        if spot_val <= 0:
            return 0.0

        gamma_per_share = 0.0
        if chain_builder is not None:
            gamma_per_share = self._lookup_gamma_by_token(
                getattr(order, "instrument_token", 0), chain_builder,
            )
        qty = abs(int(getattr(order, "quantity", 0) or 0))
        if gamma_per_share == 0.0 or qty == 0:
            return 0.0
        total_gamma = gamma_per_share * qty
        return self._dollar_gamma_1pct(total_gamma, spot_val)

    @staticmethod
    def _lookup_gamma_by_token(instrument_token: int, chain_builder) -> float:
        if instrument_token <= 0:
            return 0.0
        try:
            token_map = getattr(chain_builder, "_token_map", {})
            mapping = token_map.get(instrument_token)
            if mapping is None:
                return 0.0
            underlying, expiry, strike, option_type = mapping
            chain: OptionChain | None = chain_builder.get_chain(underlying, expiry)
            if chain is None:
                return 0.0
            for entry in chain.strikes:
                if entry.strike != strike:
                    continue
                opt = entry.ce if str(option_type).endswith("CE") else entry.pe
                if opt is None:
                    return 0.0
                return float(opt.greeks.gamma or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0
        return 0.0
