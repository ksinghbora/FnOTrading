"""Typed tag taxonomy for structured logs (DATA_RELIABILITY_PLAN §6.3 / §8.2).

All structured log events should carry a ``Tag`` so JSONL queries can
filter on a stable, exhaustive set instead of fuzzy substring matches.
Use it via the standard ``logger.info(..., extra={"tag": Tag.X, ...})``
pattern — the JSON formatter pulls it into a top-level ``tag`` field.

Adding a new tag: append below + grep for any place that emits the
informal ``[NAME]`` prefix today and migrate to ``extra={"tag": Tag.NAME}``.
The tag enum is intentionally a closed list so a typo at a call site
fails at import time rather than silently writing a garbage tag value.
"""

from __future__ import annotations

from enum import StrEnum


class Tag(StrEnum):
    # Strategy / decision lifecycle
    ENTRY_QUALITY = "ENTRY_QUALITY"      # Signal evaluated — accepted or rejected with reason
    FILTER = "FILTER"                    # PCR / max-pain / IV-skew / trend filter outcome
    ATTRIBUTION = "ATTRIBUTION"          # Per-leg P&L breakdown at exit
    MONITOR = "MONITOR"                  # Strategy-internal health check
    DAY_SUMMARY = "DAY_SUMMARY"          # End-of-day per-strategy snapshot
    SUMMARY = "SUMMARY"                  # 60s operational summary (positions, queue depth)

    # Risk / safety
    RISK_BLOCK = "RISK_BLOCK"            # Risk manager rejected an order
    KILL_SWITCH = "KILL_SWITCH"          # Kill switch tripped or reset
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"  # Day-loss breaker triggered

    # Orders / OMS
    ORDER_LIFECYCLE = "ORDER_LIFECYCLE"  # placed → ack → fill → exit
    ORDER_LATENCY = "ORDER_LATENCY"      # Signal-to-broker-ack latency for SLO tracking

    # Market data / recorder
    TICKER = "TICKER"                    # KiteTicker connection / subscription events
    WS_RECONNECT = "WS_RECONNECT"        # WS reconnect (heartbeat-driven or auth refresh)
    DATA_QUALITY = "DATA_QUALITY"        # Recorder snapshot quality (priceable %, IV %)
    ATOMIC_WRITE_FAIL = "ATOMIC_WRITE_FAIL"  # Atomic write to disk failed

    # AI advisor
    ADVISOR = "ADVISOR"                  # Morning bias / nightly audit emission

    # System
    STARTUP = "STARTUP"                  # Process startup steps
    SHUTDOWN = "SHUTDOWN"                # Process shutdown steps
