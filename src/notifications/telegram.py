"""Telegram notifier — sends alerts via Telegram Bot API.

Uses httpx async client with rate limiting to avoid hitting
Telegram's 30 messages/second limit.
"""

import logging

import httpx

from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Sends notifications to a Telegram chat via Bot API.

    Rate limited to avoid Telegram's per-chat rate limits
    (roughly 1 message/second per chat for bots).
    """

    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._base_url = f"{TELEGRAM_API_BASE}/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=10.0)
        self._rate_limiter = RateLimiter(rate=1.0, burst=3)
        self._enabled = bool(bot_token and chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a text message to the configured chat.

        Returns True if sent successfully, False otherwise.
        """
        if not self._enabled:
            logger.debug("Telegram not configured, skipping message")
            return False

        await self._rate_limiter.acquire()

        try:
            response = await self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Telegram API error {e.response.status_code}: {e.response.text}")
            return False
        except httpx.RequestError as e:
            logger.error(f"Telegram request error: {e}")
            return False

    async def send_trade_alert(
        self,
        tradingsymbol: str,
        side: str,
        quantity: int,
        price: float | str,
        strategy_id: str,
    ) -> bool:
        """Send a formatted trade execution alert."""
        from src.notifications.templates import trade_executed
        from decimal import Decimal

        text = trade_executed(
            tradingsymbol=tradingsymbol,
            side=side,
            quantity=quantity,
            price=Decimal(str(price)),
            strategy_id=strategy_id,
        )
        return await self.send_message(text)

    async def send_risk_alert(self, alert_type: str, details: str) -> bool:
        """Send a risk-related alert."""
        from src.notifications.templates import risk_breach

        text = risk_breach(breach_type=alert_type, details=details)
        return await self.send_message(text)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
