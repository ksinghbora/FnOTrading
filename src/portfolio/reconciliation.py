"""EOD reconciliation — compare internal positions with broker."""

import logging
from decimal import Decimal

from src.broker.base import BrokerClient
from src.core.exceptions import ReconciliationError
from src.core.structured_logger import get_structured_logger
from src.portfolio.positions import PositionTracker

logger = logging.getLogger(__name__)


class Reconciler:
    """Reconciles internal position tracking with broker positions.

    Run at EOD (3:35 PM IST) and on application startup.
    """

    def __init__(self, position_tracker: PositionTracker, broker: BrokerClient):
        self._positions = position_tracker
        self._broker = broker

    async def reconcile(self) -> dict:
        """Compare positions and flag discrepancies.

        Returns:
            Dict with 'status' ('ok' or 'mismatch'), 'discrepancies' list.
        """
        broker_positions = await self._broker.get_positions()
        if not broker_positions or not isinstance(broker_positions, dict):
            raise ReconciliationError("Broker returned invalid position data")
        net_positions = broker_positions.get("net", [])

        # Build broker position map: tradingsymbol -> quantity
        broker_map: dict[str, int] = {}
        for bp in net_positions:
            symbol = bp.get("tradingsymbol", "")
            qty = bp.get("quantity", 0)
            if qty != 0:
                broker_map[symbol] = qty

        # Build internal position map
        internal_positions = self._positions.get_open_positions()
        internal_map: dict[str, int] = {}
        for pos in internal_positions:
            if pos.quantity != 0:
                existing = internal_map.get(pos.tradingsymbol, 0)
                internal_map[pos.tradingsymbol] = existing + pos.quantity

        # Compare
        discrepancies = []
        all_symbols = set(broker_map.keys()) | set(internal_map.keys())

        for symbol in all_symbols:
            broker_qty = broker_map.get(symbol, 0)
            internal_qty = internal_map.get(symbol, 0)

            if broker_qty != internal_qty:
                discrepancies.append({
                    "tradingsymbol": symbol,
                    "broker_qty": broker_qty,
                    "internal_qty": internal_qty,
                    "difference": broker_qty - internal_qty,
                })

        slog = get_structured_logger()

        if discrepancies:
            logger.warning(f"Reconciliation found {len(discrepancies)} discrepancies")
            for d in discrepancies:
                logger.warning(
                    f"  {d['tradingsymbol']}: broker={d['broker_qty']} "
                    f"internal={d['internal_qty']} diff={d['difference']}"
                )
            slog.log(
                "RECONCILE",
                status="mismatch",
                broker_positions=len(broker_map),
                internal_positions=len(internal_map),
                discrepancy_count=len(discrepancies),
                discrepancies=discrepancies,
            )
            return {"status": "mismatch", "discrepancies": discrepancies}
        else:
            logger.info("Reconciliation: all positions match")
            slog.log(
                "RECONCILE",
                status="ok",
                broker_positions=len(broker_map),
                internal_positions=len(internal_map),
                discrepancy_count=0,
            )
            return {"status": "ok", "discrepancies": []}
