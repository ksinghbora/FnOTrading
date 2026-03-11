"""Strategy payoff diagram computation."""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from src.core.types import OptionType, OrderSide


@dataclass
class PayoffLeg:
    """A single leg in a multi-leg strategy for payoff calculation."""

    strike: float
    option_type: OptionType  # CE or PE
    side: OrderSide  # BUY or SELL
    quantity: int
    premium: float  # Premium paid/received per unit


def compute_payoff(
    legs: list[PayoffLeg],
    spot_range: tuple[float, float] | None = None,
    num_points: int = 200,
) -> dict:
    """Compute payoff at expiry for a multi-leg strategy.

    Args:
        legs: List of option legs.
        spot_range: (min, max) spot price range. Auto-calculated if None.
        num_points: Number of points in the payoff curve.

    Returns:
        Dict with 'spot_prices', 'payoff', 'max_profit', 'max_loss', 'breakevens'.
    """
    if not legs:
        return {"spot_prices": [], "payoff": [], "max_profit": 0, "max_loss": 0, "breakevens": []}

    strikes = [leg.strike for leg in legs]
    if spot_range is None:
        center = sum(strikes) / len(strikes)
        spread = max(strikes) - min(strikes)
        margin = max(spread * 1.5, center * 0.1)
        spot_range = (center - margin, center + margin)

    spot_prices = np.linspace(spot_range[0], spot_range[1], num_points)
    total_payoff = np.zeros(num_points)

    for leg in legs:
        multiplier = leg.quantity if leg.side == OrderSide.BUY else -leg.quantity
        premium_flow = -leg.premium if leg.side == OrderSide.BUY else leg.premium

        if leg.option_type == OptionType.CE:
            intrinsic = np.maximum(spot_prices - leg.strike, 0)
        else:
            intrinsic = np.maximum(leg.strike - spot_prices, 0)

        leg_payoff = (intrinsic * multiplier) + (premium_flow * abs(leg.quantity))
        total_payoff += leg_payoff

    # Find breakevens (where payoff crosses zero)
    breakevens = []
    for i in range(len(total_payoff) - 1):
        if total_payoff[i] * total_payoff[i + 1] < 0:
            # Linear interpolation
            x = spot_prices[i] - total_payoff[i] * (
                spot_prices[i + 1] - spot_prices[i]
            ) / (total_payoff[i + 1] - total_payoff[i])
            breakevens.append(round(float(x), 2))

    return {
        "spot_prices": spot_prices.tolist(),
        "payoff": total_payoff.tolist(),
        "max_profit": round(float(np.max(total_payoff)), 2),
        "max_loss": round(float(np.min(total_payoff)), 2),
        "breakevens": breakevens,
    }
