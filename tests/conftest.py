"""Shared test fixtures."""

import pytest

from src.core.clock import MarketClock
from src.core.events import EventBus


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def clock():
    return MarketClock()
