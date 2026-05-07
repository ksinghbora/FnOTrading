"""Real-time P&L calculation — realized + unrealized."""

import logging
from datetime import datetime
from decimal import Decimal

from src.core.clock import IST
from src.core.models import PnL, Position
from src.portfolio.positions import PositionTracker

logger = logging.getLogger(__name__)


class PnLCalculator:
    """Calculates real-time P&L across all strategies.

    - Realized P&L from closed positions
    - Unrealized P&L (mark-to-market) from open positions
    - P&L curve for intraday visualization
    """

    def __init__(self, position_tracker: PositionTracker):
        self._positions = position_tracker
        self._total_charges: dict[str, Decimal] = {}  # strategy_id -> total charges
        self._pnl_curve: list[dict] = []  # [{timestamp, pnl}]

    def add_charges(self, strategy_id: str, charges: Decimal) -> None:
        """Record charges from a trade."""
        current = self._total_charges.get(strategy_id, Decimal("0"))
        self._total_charges[strategy_id] = current + charges
        logger.debug(
            f"[CHARGES] strategy={strategy_id} charges={charges} "
            f"cumulative={self._total_charges[strategy_id]}"
        )

    def get_pnl(self, strategy_id: str | None = None) -> PnL:
        """Get current P&L summary.

        Aggregates over ``strategy_id`` AND any orchestrator-child ids
        (i.e. those starting with ``f"{strategy_id}/"``). See
        ``PositionTracker.get_positions`` for the rationale: V5
        orchestrator children carry composite ids like
        ``"orchestrator_1/iron_condor"`` and we want the parent's PnL
        query to roll up across them.
        """
        positions = self._positions.get_positions(strategy_id)

        realized = Decimal("0")
        unrealized = Decimal("0")

        for pos in positions:
            realized += pos.pnl
            if pos.quantity != 0 and pos.ltp > 0 and pos.average_price > 0:
                if pos.quantity > 0:
                    unrealized += pos.quantity * (pos.ltp - pos.average_price)
                else:
                    unrealized += abs(pos.quantity) * (pos.average_price - pos.ltp)

        if strategy_id:
            # Sum charges across the parent id AND any child ids that
            # start with ``f"{strategy_id}/"`` (orchestrator-child rollup).
            prefix = strategy_id + "/"
            charges = sum(
                (chg for sid, chg in self._total_charges.items()
                 if sid == strategy_id or sid.startswith(prefix)),
                Decimal("0"),
            )
        else:
            charges = sum(self._total_charges.values(), Decimal("0"))

        gross = realized + unrealized
        net = gross - charges

        return PnL(
            gross=gross.quantize(Decimal("0.01")),
            realized=realized.quantize(Decimal("0.01")),
            unrealized=unrealized.quantize(Decimal("0.01")),
            charges=charges.quantize(Decimal("0.01")),
            net=net.quantize(Decimal("0.01")),
        )

    def get_day_pnl(self) -> Decimal:
        """Get overall day P&L (net of charges)."""
        return self.get_pnl().net

    MAX_PNL_CURVE_POINTS = 2000  # ~6.5 hours at 12s interval

    def snapshot_pnl(self) -> None:
        """Take a P&L snapshot for the intraday curve."""
        pnl = self.get_pnl()
        self._pnl_curve.append({
            "timestamp": datetime.now(IST).isoformat(),
            "gross": float(pnl.gross),
            "realized": float(pnl.realized),
            "unrealized": float(pnl.unrealized),
            "charges": float(pnl.charges),
            "net": float(pnl.net),
        })
        if len(self._pnl_curve) > self.MAX_PNL_CURVE_POINTS:
            self._pnl_curve = self._pnl_curve[-self.MAX_PNL_CURVE_POINTS:]

    def get_pnl_curve(self) -> list[dict]:
        """Get intraday P&L curve data."""
        return self._pnl_curve

    def reset_daily(self) -> None:
        """Reset daily P&L tracking (call at start of each trading day)."""
        total_charges = sum(self._total_charges.values(), Decimal("0"))
        logger.info(
            f"[RESET] daily_pnl_reset charges_cleared={total_charges} "
            f"curve_points_cleared={len(self._pnl_curve)}"
        )
        self._pnl_curve.clear()
        self._total_charges.clear()
        self._positions.reset_daily()
