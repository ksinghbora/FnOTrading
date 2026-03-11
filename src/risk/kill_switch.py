"""Kill switch — emergency flatten all positions and cancel all orders."""

import asyncio
import logging

from src.broker.base import BrokerClient
from src.core.events import Event, EventBus, EventType
from src.core.types import OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)


class KillSwitch:
    """Emergency kill switch — nuclear option for risk management.

    Actions:
    1. Cancel ALL open orders
    2. Close ALL open positions at market
    3. Halt ALL strategies (stop_all via StrategyRunner)
    """

    def __init__(self, broker: BrokerClient, event_bus: EventBus):
        self._broker = broker
        self._event_bus = event_bus
        self._activated = False
        self._strategy_runner = None  # Injected after creation

    def set_strategy_runner(self, runner) -> None:
        """Inject strategy runner for halting strategies on kill switch."""
        self._strategy_runner = runner

    @property
    def is_activated(self) -> bool:
        return self._activated

    async def activate(self, reason: str = "Manual activation") -> dict:
        """Activate the kill switch.

        Returns summary of actions taken.
        """
        self._activated = True
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

        results = {
            "reason": reason,
            "orders_cancelled": 0,
            "positions_closed": 0,
            "errors": [],
        }

        # Step 1: Cancel all open orders
        try:
            orders = await self._broker.get_orders()
            for order in orders:
                status = order.get("status", "")
                if status in ("OPEN", "TRIGGER PENDING", "OPEN PENDING"):
                    try:
                        await self._broker.cancel_order(str(order["order_id"]))
                        results["orders_cancelled"] += 1
                    except Exception as e:
                        results["errors"].append(f"Cancel order {order['order_id']}: {e}")
        except Exception as e:
            results["errors"].append(f"Fetch orders: {e}")

        # Step 2: Close all open positions at market (retry up to 3 times)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                positions = await self._broker.get_positions()
                remaining = []
                for pos in positions.get("net", []):
                    qty = pos.get("quantity", 0)
                    if qty == 0:
                        continue
                    remaining.append(pos)

                    side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                    try:
                        await self._broker.place_order(
                            tradingsymbol=pos["tradingsymbol"],
                            exchange=pos.get("exchange", "NFO"),
                            side=side,
                            quantity=abs(qty),
                            order_type=OrderType.MARKET,
                            product=ProductType(pos.get("product", "NRML")),
                        )
                        results["positions_closed"] += 1
                    except Exception as e:
                        results["errors"].append(
                            f"Close {pos['tradingsymbol']} ({qty}) attempt {attempt}: {e}"
                        )

                if not remaining:
                    break
                if attempt < max_retries:
                    logger.warning(f"Kill switch: {len(remaining)} positions remaining, retrying...")
                    await asyncio.sleep(1)
            except Exception as e:
                results["errors"].append(f"Fetch positions attempt {attempt}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(1)

        # Step 3: Halt all strategies
        if self._strategy_runner:
            try:
                await self._strategy_runner.stop_all()
                logger.info("All strategies halted by kill switch")
            except Exception as e:
                results["errors"].append(f"Halt strategies: {e}")

        # Publish kill switch event
        await self._event_bus.publish(
            Event.create(
                EventType.KILL_SWITCH,
                source="kill_switch",
                reason=reason,
                results=results,
            )
        )

        if results["errors"]:
            results["status"] = "partial_failure"
            logger.critical(
                f"KILL SWITCH INCOMPLETE: cancelled={results['orders_cancelled']} "
                f"closed={results['positions_closed']} errors={len(results['errors'])}. "
                f"MANUAL INTERVENTION REQUIRED! Errors: {results['errors']}"
            )
        else:
            results["status"] = "success"
            logger.critical(
                f"Kill switch complete: cancelled={results['orders_cancelled']} "
                f"closed={results['positions_closed']}"
            )
        return results

    def reset(self) -> None:
        """Reset kill switch (re-enable trading)."""
        self._activated = False
        logger.info("Kill switch deactivated")
