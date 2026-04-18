"""Tests for scripts/replay_counterfactual.py.

Contracts this file locks in:

  1. **No alert when divergence is within tolerance** — quiet days don't
     wake anyone up.
  2. **Alert fires when divergence exceeds tolerance** — the whole point.
  3. **Direction is reflected in the message** — operator sees at a glance
     whether replay > live or vice versa.
  4. **Replay error produces its own alert flavour** — failure to compute a
     counterfactual is itself a signal worth surfacing.
  5. **Function never raises** — even on collector / engine error, returns
     a CounterfactualResult so the nightly audit always completes.
  6. **Tolerance is honoured boundary-cleanly** — exactly-at-tolerance does
     NOT alert (alerting only on STRICTLY-greater).
  7. **Collector and replay are dependency-injected** — the real Settings
     object is never required for unit testing.
  8. **Snapshot-fallback is its own alert flavour** — a "no divergence"
     reply that secretly used live params is a false negative; surface it
     instead of going silent.

These are pure unit tests against the comparison logic. The integration of
the real engine on real data is covered by
``tests/integration/test_replay_determinism.py``.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.advisor.models import PremiumLegData, TodayData, TrendLegData  # noqa: E402
from src.backtest.day_replay import DayReplayResult  # noqa: E402

import replay_counterfactual as rc  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

def _today_data(total_pnl: float = 0.0) -> TodayData:
    """Build a TodayData with the given total_pnl. Other fields don't matter
    for the counterfactual — we only compare the topline number."""
    return TodayData(
        date=date(2026, 4, 15),
        total_pnl=total_pnl,
        premium_leg=PremiumLegData(),
        trend_leg=TrendLegData(),
    )


def _replay_result(
    *, total_pnl: float = 0.0, error: str | None = None,
    replay_hash: str = "abc123def456" + "0" * 52,
    params_source: str = "snapshot:config/snapshots/2026-04-15/params.json",
    snapshot_coverage_pct: float = 100.0,
) -> DayReplayResult:
    return DayReplayResult(
        date="2026-04-15",
        strategy="portfolio",
        underlying="NIFTY",
        seed=0,
        code_sha="deadbeef",
        params_sha="cafef00d",
        params_source=params_source,
        snapshot_dir="config/snapshots/2026-04-15",
        total_pnl=total_pnl,
        num_days=1,
        snapshot_coverage_pct=snapshot_coverage_pct,
        fills_via_bid_ask=8,
        fills_via_ltp_slip=0,
        replay_hash=replay_hash,
        run_at="2026-04-15T16:00:00Z",
        error=error,
    )


def _patched(*, live_pnl: float, replay: DayReplayResult):
    """Build the (collect_fn, replay_fn) pair to inject into compare_live_vs_replay."""
    collect = AsyncMock(return_value=_today_data(total_pnl=live_pnl))
    replay_fn = AsyncMock(return_value=replay)
    return collect, replay_fn


# ────────────────────────────────────────────────────────────────────
# 1. No alert within tolerance
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_alert_when_within_tolerance(monkeypatch):
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(total_pnl=1100.0),  # ₹100 divergence
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        settings=_FakeSettings(),
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.divergence == pytest.approx(100.0)
    assert cf.breached is False
    assert cf.telegram_message is None


# ────────────────────────────────────────────────────────────────────
# 2. Alert when over tolerance
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_when_divergence_breaches_tolerance(monkeypatch):
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(total_pnl=2500.0),  # ₹1500 divergence
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        settings=_FakeSettings(),
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.divergence == pytest.approx(1500.0)
    assert cf.breached is True
    assert cf.telegram_message is not None
    assert "Backtest-live divergence" in cf.telegram_message
    assert "1,500" in cf.telegram_message  # divergence in message
    assert "abc123def456" in cf.telegram_message  # truncated hash


# ────────────────────────────────────────────────────────────────────
# 3. Direction is reflected in the message
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_reports_replay_higher_direction():
    collect, replay_fn = _patched(
        live_pnl=500.0,
        replay=_replay_result(total_pnl=2000.0),  # replay > live
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert "should have made" in cf.telegram_message


@pytest.mark.asyncio
async def test_alert_reports_live_outperformed_direction():
    collect, replay_fn = _patched(
        live_pnl=2000.0,
        replay=_replay_result(total_pnl=500.0),  # live > replay
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert "live *outperformed*" in cf.telegram_message


# ────────────────────────────────────────────────────────────────────
# 4. Replay error → its own alert flavour
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_error_produces_distinct_alert():
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(total_pnl=0.0, error="No spot data loaded"),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    # Replay error means we couldn't compute a counterfactual; breached is
    # False (no comparison possible) but message still fires.
    assert cf.breached is False
    assert cf.replay_error == "No spot data loaded"
    assert cf.telegram_message is not None
    assert "Counterfactual replay failed" in cf.telegram_message
    assert "No spot data loaded" in cf.telegram_message


# ────────────────────────────────────────────────────────────────────
# 5. Tolerance boundary
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exactly_at_tolerance_does_not_alert():
    """Boundary case: divergence == tolerance is NOT a breach. We only
    alert on STRICTLY greater so the threshold has clean semantics."""
    collect, replay_fn = _patched(
        live_pnl=0.0,
        replay=_replay_result(total_pnl=500.0),  # exactly tolerance
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.divergence == 500.0
    assert cf.breached is False
    assert cf.telegram_message is None


@pytest.mark.asyncio
async def test_just_over_tolerance_does_alert():
    collect, replay_fn = _patched(
        live_pnl=0.0,
        replay=_replay_result(total_pnl=500.01),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.breached is True
    assert cf.telegram_message is not None


# ────────────────────────────────────────────────────────────────────
# 6. Result shape
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_carries_metadata_for_audit():
    collect, replay_fn = _patched(
        live_pnl=100.0,
        replay=_replay_result(
            total_pnl=200.0,
            params_source="snapshot:foo",
            snapshot_coverage_pct=95.5,
        ),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        strategy_name="portfolio",
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.date == "2026-04-15"
    assert cf.strategy == "portfolio"
    assert cf.live_pnl == 100.0
    assert cf.replay_pnl == 200.0
    assert cf.params_source == "snapshot:foo"
    assert cf.snapshot_coverage_pct == 95.5

    # Round-trip through as_jsonable for the future DB write path
    blob = cf.as_jsonable()
    assert blob["live_pnl"] == 100.0
    assert blob["replay_pnl"] == 200.0
    assert blob["divergence"] == 100.0
    assert blob["breached"] is False
    assert blob["params_source_is_fallback"] is False  # snapshot:foo wins


# ────────────────────────────────────────────────────────────────────
# 7. _format_alert as a pure function (synchronous probe)
# ────────────────────────────────────────────────────────────────────

def test_format_alert_returns_none_when_under_tolerance():
    msg = rc._format_alert(
        date(2026, 4, 15), "portfolio",
        live_pnl=100.0,
        replay=_replay_result(total_pnl=400.0),
        tolerance=500.0,
    )
    assert msg is None


def test_format_alert_returns_string_when_breached():
    msg = rc._format_alert(
        date(2026, 4, 15), "portfolio",
        live_pnl=100.0,
        replay=_replay_result(total_pnl=10000.0),
        tolerance=500.0,
    )
    assert msg
    assert "9,900" in msg or "9900" in msg


def test_format_alert_handles_replay_error_first():
    """If replay errored, error path wins regardless of divergence number."""
    msg = rc._format_alert(
        date(2026, 4, 15), "portfolio",
        live_pnl=1000.0,
        replay=_replay_result(total_pnl=0.0, error="kaboom"),
        tolerance=500.0,
    )
    assert msg
    assert "failed" in msg
    assert "kaboom" in msg


# ────────────────────────────────────────────────────────────────────
# 8. Snapshot-fallback flavour — silence is the failure mode
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "params_source, expected_fallback",
    [
        ("snapshot:config/snapshots/2026-04-15/params.json", False),
        ("snapshot:foo", False),
        ("live", True),
        ("live (no mapping)", True),
        ("", True),
        (None, True),
    ],
)
def test_params_source_is_fallback_classifier(params_source, expected_fallback):
    """The loader labels snapshot-backed sources as ``snapshot:<path>``;
    everything else means we ran against live params."""
    assert rc._params_source_is_fallback(params_source) is expected_fallback


@pytest.mark.asyncio
async def test_fallback_alone_emits_standalone_warning():
    """No divergence breach + no replay error, but params_source is 'live'.
    The function MUST still emit a message — silence here would be a false
    negative (we'd think the day was clean when we couldn't actually compare)."""
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(
            total_pnl=1100.0,         # ₹100 divergence, well under tolerance
            params_source="live",      # the failure mode we're alerting on
        ),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.breached is False
    assert cf.params_source_is_fallback is True
    assert cf.telegram_message is not None
    assert "Snapshot-fallback warning" in cf.telegram_message
    # Standalone fallback message should NOT pretend there was a divergence breach.
    assert "Backtest-live divergence" not in cf.telegram_message


@pytest.mark.asyncio
async def test_fallback_prepends_banner_to_divergence_alert():
    """When BOTH a fallback AND a divergence breach apply, the divergence
    alert should still fire, with the fallback banner prepended so the
    operator knows the comparison was degraded before they read the number."""
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(
            total_pnl=2500.0,           # ₹1500 divergence > ₹500 tolerance
            params_source="live (no mapping)",
        ),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.breached is True
    assert cf.params_source_is_fallback is True
    msg = cf.telegram_message
    assert msg is not None
    assert "Snapshot-fallback warning" in msg
    assert "Backtest-live divergence" in msg
    # Banner must come BEFORE the divergence header so it isn't lost in tail-truncated alerts.
    assert msg.index("Snapshot-fallback warning") < msg.index("Backtest-live divergence")


@pytest.mark.asyncio
async def test_fallback_prepends_banner_to_replay_error_alert():
    """Replay-error path also gets the fallback banner prepended."""
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(
            total_pnl=0.0,
            error="No spot data loaded",
            params_source="live",
        ),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.params_source_is_fallback is True
    msg = cf.telegram_message
    assert msg is not None
    assert "Snapshot-fallback warning" in msg
    assert "Counterfactual replay failed" in msg


@pytest.mark.asyncio
async def test_fallback_flag_is_false_for_snapshot_source():
    """Sanity: a normal snapshot-backed run must NOT be flagged as fallback."""
    collect, replay_fn = _patched(
        live_pnl=1000.0,
        replay=_replay_result(
            total_pnl=1100.0,
            params_source="snapshot:config/snapshots/2026-04-15/params.json",
        ),
    )
    cf = await rc.compare_live_vs_replay(
        target_date=date(2026, 4, 15),
        tolerance_inr=500.0,
        collect_fn=collect, replay_fn=replay_fn,
    )
    assert cf.params_source_is_fallback is False
    assert cf.telegram_message is None  # under tolerance + not fallback → quiet


# ────────────────────────────────────────────────────────────────────
# Module-level constants
# ────────────────────────────────────────────────────────────────────

def test_default_tolerance_matches_slo():
    """Plan §8.7 SLO: |Δ| < ₹500/day on > 95% of days. Lock the default
    so a casual edit doesn't drift away from the documented threshold."""
    assert rc.DEFAULT_TOLERANCE_INR == 500.0


# ────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────

class _FakeSettings:
    """Stand-in for src.config.Settings — counterfactual only reads
    ``settings`` to forward into the collector. Unit tests don't need
    a real Pydantic load."""

    def __getattr__(self, name: str):
        return None
