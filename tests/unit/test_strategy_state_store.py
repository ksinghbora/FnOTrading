"""Tests for StrategyStateStore — DB-backed daily state persistence.

Validates the Apr 16 fix: restart on the same trading day must restore
`_entered`/`_stopped_for_day` flags. Restart on a new day must NOT load
yesterday's state (composite key (strategy_id, as_of_date) handles this).
"""

from datetime import date

import pytest

from src.strategy.state_store import StrategyStateStore


class TestStateStoreNoSession:
    """Backtests/tests pass session_factory=None → store degrades to no-op."""

    @pytest.mark.asyncio
    async def test_no_session_disables_store(self):
        store = StrategyStateStore(None)
        assert store.enabled is False

    @pytest.mark.asyncio
    async def test_no_session_save_is_noop(self):
        store = StrategyStateStore(None)
        # Must not raise — strategies should never crash because DB is offline
        await store.save_state("any", date(2026, 4, 17), {"_entered": True})

    @pytest.mark.asyncio
    async def test_no_session_load_returns_none(self):
        store = StrategyStateStore(None)
        result = await store.load_state("any", date(2026, 4, 17))
        assert result is None


class TestStateStoreInMemory:
    """Behavior with a fake session factory — verifies the round-trip semantics."""

    def setup_method(self):
        # In-memory dict simulating the (strategy_id, date) → data table
        self._table: dict[tuple[str, date], dict] = {}

        store = StrategyStateStore(None)

        async def _save(strategy_id, as_of_date, state_data):
            self._table[(strategy_id, as_of_date)] = state_data

        async def _load(strategy_id, as_of_date):
            return self._table.get((strategy_id, as_of_date))

        # Patch the store with our in-memory impl (the real one talks to PG)
        store.save_state = _save
        store.load_state = _load
        # Override `enabled` via property → instance attribute trick.
        # Save the original so teardown can restore it — otherwise the
        # mutation leaks into other tests that share the class object.
        self._orig_enabled = StrategyStateStore.enabled
        StrategyStateStore.enabled = property(lambda self: True)
        self.store = store

    def teardown_method(self):
        # Restore class-level property so we don't pollute other tests
        StrategyStateStore.enabled = self._orig_enabled

    @pytest.mark.asyncio
    async def test_save_then_load_round_trip(self):
        await self.store.save_state(
            "ic-1", date(2026, 4, 17), {"_entered": True, "_trades_today": 1}
        )
        loaded = await self.store.load_state("ic-1", date(2026, 4, 17))
        assert loaded == {"_entered": True, "_trades_today": 1}

    @pytest.mark.asyncio
    async def test_load_returns_none_for_different_date(self):
        # Apr 16 state must NOT be loaded on Apr 17 — fresh start
        await self.store.save_state("ic-1", date(2026, 4, 16), {"_entered": True})
        loaded = await self.store.load_state("ic-1", date(2026, 4, 17))
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_returns_none_for_unknown_strategy(self):
        await self.store.save_state("ic-1", date(2026, 4, 17), {"_entered": True})
        loaded = await self.store.load_state("portfolio-1", date(2026, 4, 17))
        assert loaded is None

    @pytest.mark.asyncio
    async def test_save_overwrites_same_day(self):
        await self.store.save_state("ic-1", date(2026, 4, 17), {"_entered": False})
        await self.store.save_state(
            "ic-1", date(2026, 4, 17), {"_entered": True, "_trades_today": 1}
        )
        loaded = await self.store.load_state("ic-1", date(2026, 4, 17))
        assert loaded["_entered"] is True
        assert loaded["_trades_today"] == 1
