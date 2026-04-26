"""Iron Butterfly strategy — Iron Condor with ATM body.

Same defined-risk 4-leg structure as Iron Condor (sells short body, buys
wings) but the short legs are sold at-the-money (delta ~0.5) instead of
OTM. Larger credit, tighter break-even zone, more gamma/vega exposure.
All entry, adjustment, and exit logic is inherited unchanged from
IronCondorStrategy; only the strike-selection deltas differ via params.
"""

from src.strategy.implementations.iron_condor import IronCondorStrategy
from src.strategy.params import IronButterflyParams
from src.strategy.registry import register_strategy


@register_strategy("iron_butterfly", IronButterflyParams)
class IronButterflyStrategy(IronCondorStrategy):
    params: IronButterflyParams
