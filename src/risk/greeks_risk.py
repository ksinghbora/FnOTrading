"""Portfolio Greeks risk monitoring."""

import logging
from dataclasses import dataclass

from src.core.models import Position

logger = logging.getLogger(__name__)


@dataclass
class GreeksRiskLimits:
    """Configurable limits for portfolio Greeks."""

    max_abs_delta: float = 500      # Max absolute portfolio delta
    max_abs_gamma: float = 100      # Max absolute portfolio gamma
    max_neg_theta: float = -5000    # Max negative theta (daily)
    max_abs_vega: float = 5000      # Max absolute portfolio vega


@dataclass
class PortfolioGreeks:
    """Aggregate portfolio-level Greeks."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


class GreeksRiskMonitor:
    """Monitors portfolio-level Greeks and flags breaches."""

    def __init__(self, limits: GreeksRiskLimits | None = None):
        self._limits = limits or GreeksRiskLimits()
        self._current = PortfolioGreeks()

    @property
    def current_greeks(self) -> PortfolioGreeks:
        return self._current

    def update(self, positions: list[Position]) -> PortfolioGreeks:
        """Recalculate portfolio Greeks from all open positions."""
        total_delta = 0.0
        total_gamma = 0.0
        total_theta = 0.0
        total_vega = 0.0

        for pos in positions:
            if pos.quantity == 0:
                continue
            qty = pos.quantity  # Positive for long, negative for short
            total_delta += pos.greeks.delta * qty
            total_gamma += pos.greeks.gamma * qty
            total_theta += pos.greeks.theta * qty
            total_vega += pos.greeks.vega * qty

        self._current = PortfolioGreeks(
            delta=round(total_delta, 2),
            gamma=round(total_gamma, 4),
            theta=round(total_theta, 2),
            vega=round(total_vega, 2),
        )
        return self._current

    def check_limits(self) -> list[str]:
        """Check if any Greeks limits are breached.

        Returns list of breach descriptions (empty = all OK).
        """
        breaches = []

        if abs(self._current.delta) > self._limits.max_abs_delta:
            breaches.append(
                f"Delta breach: {self._current.delta:.1f} "
                f"(limit: +/-{self._limits.max_abs_delta})"
            )

        if abs(self._current.gamma) > self._limits.max_abs_gamma:
            breaches.append(
                f"Gamma breach: {self._current.gamma:.2f} "
                f"(limit: +/-{self._limits.max_abs_gamma})"
            )

        if self._current.theta < self._limits.max_neg_theta:
            breaches.append(
                f"Theta breach: {self._current.theta:.1f} "
                f"(limit: {self._limits.max_neg_theta})"
            )

        if abs(self._current.vega) > self._limits.max_abs_vega:
            breaches.append(
                f"Vega breach: {self._current.vega:.1f} "
                f"(limit: +/-{self._limits.max_abs_vega})"
            )

        return breaches
