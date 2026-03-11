"""Retry decorator with exponential backoff."""

import asyncio
import functools
import logging
from typing import Any, Callable, Type

logger = logging.getLogger(__name__)


def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[Exception, int], None] | None = None,
):
    """Decorator for retrying async functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exceptions: Tuple of exception types to catch.
        on_retry: Optional callback on each retry (exception, attempt_number).
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}. "
                        f"Waiting {delay:.1f}s"
                    )
                    if on_retry:
                        on_retry(e, attempt + 1)
                    await asyncio.sleep(delay)

            raise last_exception  # type: ignore[misc]

        return wrapper

    return decorator
