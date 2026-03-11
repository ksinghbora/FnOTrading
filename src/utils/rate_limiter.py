"""Token bucket rate limiter for API calls."""

import asyncio
import time


class RateLimiter:
    """Token bucket rate limiter.

    Ensures we don't exceed broker API rate limits (e.g., Kite's 10 req/sec).
    """

    def __init__(self, rate: float, burst: int | None = None):
        """
        Args:
            rate: Maximum sustained requests per second.
            burst: Maximum burst size (defaults to rate).
        """
        self._rate = rate
        self._burst = burst or int(rate)
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                # Wait for next token
                wait_time = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """Add tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens
