"""Tests for cross-process IPC: EventBus.publish_local() + RedisEventBridge.

These cover the recorder/trader process split (DATA_RELIABILITY_PLAN §5).
The contracts we're protecting:
  1. publish() mirrors to Redis; publish_local() does NOT.
  2. RedisEventBridge subscribes to specified channels, deserializes the
     JSON payload, and re-emits the event onto the local bus.
  3. NO LOOP: the bridge must use publish_local() so a forwarded event
     doesn't echo back through Redis to itself.
  4. Bridge survives malformed Redis messages without crashing the loop.
  5. Bridge start/stop is idempotent.

We use a tiny in-memory fake Redis instead of pulling in the `fakeredis`
dep. Only the methods we actually call are implemented — pubsub() +
publish() + listen() — so this stays under 100 lines.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from src.core.events import Event, EventBus, EventType, RedisEventBridge


# ── Fake Redis (just enough for pub/sub) ───────────────────────────


class _FakePubSub:
    def __init__(self, redis: "_FakeRedis"):
        self._redis = redis
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._closed = False

    async def subscribe(self, *channels: str) -> None:
        for ch in channels:
            self._channels.add(ch)
            self._redis._subscribers.setdefault(ch, []).append(self)
            # Mimic real Redis: deliver a 'subscribe' confirmation msg first
            await self._queue.put({"type": "subscribe", "channel": ch, "data": 1})

    async def unsubscribe(self, *channels: str) -> None:
        for ch in channels:
            self._channels.discard(ch)
            subs = self._redis._subscribers.get(ch, [])
            if self in subs:
                subs.remove(self)

    async def close(self) -> None:
        self._closed = True

    async def listen(self):
        while not self._closed:
            msg = await self._queue.get()
            yield msg

    def _deliver(self, channel: str, data: str) -> None:
        # Called by FakeRedis.publish() to fan out a message to this subscriber
        self._queue.put_nowait({"type": "message", "channel": channel, "data": data})


class _FakeRedis:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[_FakePubSub]] = {}
        self.published: list[tuple[str, str]] = []  # (channel, payload) — for assertions

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        for sub in self._subscribers.get(channel, []):
            sub._deliver(channel, payload)
        return len(self._subscribers.get(channel, []))

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)


# ── Helpers ────────────────────────────────────────────────────────


def _make_tick_event(token: int = 12345) -> Event:
    return Event(
        type=EventType.TICK,
        timestamp=datetime(2026, 4, 17, 10, 30, 0),
        source="test",
        payload={"tick": {"instrument_token": token, "ltp": "100.5"}},
    )


# ── publish() vs publish_local() ───────────────────────────────────


@pytest.mark.asyncio
async def test_publish_mirrors_to_redis():
    """publish() must put on local queue AND publish to Redis."""
    redis = _FakeRedis()
    bus = EventBus(redis_client=redis)
    await bus.publish(_make_tick_event())
    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == "events:tick"
    # Round-trips back to a valid Event
    Event.model_validate_json(payload)


@pytest.mark.asyncio
async def test_publish_local_skips_redis():
    """publish_local() must NOT touch Redis — that's the whole point.

    If the bridge used publish() instead of publish_local() to re-emit a
    forwarded event, every tick would echo forever between processes.
    """
    redis = _FakeRedis()
    bus = EventBus(redis_client=redis)
    await bus.publish_local(_make_tick_event())
    assert redis.published == [], "publish_local must not mirror to Redis"


@pytest.mark.asyncio
async def test_publish_handles_no_redis():
    """publish() with no Redis client must still dispatch locally."""
    bus = EventBus(redis_client=None)
    received: list[Event] = []

    async def _handler(ev: Event) -> None:
        received.append(ev)

    bus.subscribe(EventType.TICK, _handler)
    await bus.start()
    try:
        await bus.publish(_make_tick_event())
        # Give the dispatch loop a tick to run
        await asyncio.sleep(0.05)
        assert len(received) == 1
    finally:
        await bus.stop()


# ── RedisEventBridge ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_forwards_redis_messages_to_local_bus():
    """A message published on Redis must arrive at a local-bus subscriber."""
    redis = _FakeRedis()
    # NOTE: bus has no redis_client — it's the trader-side bus that
    # receives via the bridge, not via Redis mirroring.
    bus = EventBus(redis_client=None)

    received: list[Event] = []

    async def _handler(ev: Event) -> None:
        received.append(ev)

    bus.subscribe(EventType.TICK, _handler)
    await bus.start()

    bridge = RedisEventBridge(redis, bus, [EventType.TICK])
    await bridge.start()

    try:
        # Publisher side (recorder process): emit a tick event
        evt = _make_tick_event()
        await redis.publish("events:tick", evt.model_dump_json())

        # Wait briefly for: bridge.listen → bus.publish_local → dispatch loop
        for _ in range(20):
            await asyncio.sleep(0.05)
            if received:
                break

        assert len(received) == 1, "bridge must forward exactly once"
        assert received[0].type == EventType.TICK
        assert received[0].payload["tick"]["instrument_token"] == 12345
    finally:
        await bridge.stop()
        await bus.stop()


@pytest.mark.asyncio
async def test_bridge_does_not_loop_to_redis():
    """The bridge must NOT cause forwarded events to echo back to Redis.

    Setup mirrors production: the trader-side bus has redis_client set
    (so the trader's own publish() calls would mirror), AND the bridge
    is forwarding incoming events. If the bridge used publish() instead
    of publish_local(), every forwarded event would land on Redis again
    and cause an infinite loop.
    """
    redis = _FakeRedis()
    bus = EventBus(redis_client=redis)  # trader-side bus DOES have Redis attached
    await bus.start()

    bridge = RedisEventBridge(redis, bus, [EventType.TICK])
    await bridge.start()

    try:
        # Publisher (simulating the recorder): one tick event onto Redis
        evt = _make_tick_event()
        await redis.publish("events:tick", evt.model_dump_json())

        # Let the bridge process it
        await asyncio.sleep(0.3)

        # We should see exactly ONE publish on Redis: the original. The
        # bridge's re-emit must have used publish_local(), so no echo.
        assert len(redis.published) == 1, (
            f"bridge looped — Redis saw {len(redis.published)} publishes, "
            f"expected 1 (original only)"
        )
    finally:
        await bridge.stop()
        await bus.stop()


@pytest.mark.asyncio
async def test_bridge_survives_malformed_message():
    """Garbage on the channel must not crash the bridge — keep listening."""
    redis = _FakeRedis()
    bus = EventBus(redis_client=None)

    received: list[Event] = []

    async def _handler(ev: Event) -> None:
        received.append(ev)

    bus.subscribe(EventType.TICK, _handler)
    await bus.start()

    bridge = RedisEventBridge(redis, bus, [EventType.TICK])
    await bridge.start()

    try:
        # First a garbage message — bridge must log + skip, not crash
        await redis.publish("events:tick", "{not valid json")
        await asyncio.sleep(0.1)

        # Then a valid one — must still arrive
        await redis.publish("events:tick", _make_tick_event().model_dump_json())
        for _ in range(20):
            await asyncio.sleep(0.05)
            if received:
                break

        assert len(received) == 1, "bridge crashed after garbage message"
    finally:
        await bridge.stop()
        await bus.stop()


@pytest.mark.asyncio
async def test_bridge_start_is_idempotent():
    """start() twice must be a no-op, not double-launch the listener."""
    redis = _FakeRedis()
    bus = EventBus(redis_client=None)
    bridge = RedisEventBridge(redis, bus, [EventType.TICK])

    await bridge.start()
    task1 = bridge._task  # type: ignore[attr-defined]
    await bridge.start()
    task2 = bridge._task  # type: ignore[attr-defined]
    assert task1 is task2, "second start() must not replace the listener task"
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_only_subscribes_to_requested_channels():
    """An event type NOT in the bridge's list must not be forwarded.

    The bridge takes an explicit channel list rather than auto-subscribing
    to events:* so the trader doesn't burn CPU deserializing event types
    it has no handlers for (e.g. ORDER_PLACED — that's trader-internal).
    """
    redis = _FakeRedis()
    bus = EventBus(redis_client=None)

    received: list[Event] = []

    async def _handler(ev: Event) -> None:
        received.append(ev)

    bus.subscribe(EventType.TICK, _handler)
    bus.subscribe(EventType.ORDER_PLACED, _handler)
    await bus.start()

    bridge = RedisEventBridge(redis, bus, [EventType.TICK])  # ORDER_PLACED NOT included
    await bridge.start()

    try:
        # Publish an ORDER_PLACED on Redis — bridge isn't listening on that channel
        order_evt = Event(
            type=EventType.ORDER_PLACED,
            timestamp=datetime(2026, 4, 17, 10, 0, 0),
            source="test",
            payload={"order_id": "X1"},
        )
        await redis.publish("events:order_placed", order_evt.model_dump_json())
        await asyncio.sleep(0.2)

        assert received == [], (
            f"bridge forwarded an event from a channel it didn't subscribe to: {received}"
        )
    finally:
        await bridge.stop()
        await bus.stop()
