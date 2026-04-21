"""Alert when today's chain CSV row count drops sharply vs the 7-day average.

Why this exists (DATA_RELIABILITY_PLAN §10 P0):
  Silent WebSocket death is the #1 way we lose recording days. The recorder
  process keeps running, the file keeps existing, but its row count plateaus
  at noon while the broker session is dead. Nightly audit catches it 9
  hours later when the day is unrecoverable.

  This script runs hourly during market hours and Telegrams the moment
  today's row count is materially below the recent baseline (configurable,
  default 30 % drop). Cheap, low false-positive, gives ops a chance to
  intervene before the trading day ends.

Detection logic:
  1. Compute the rolling 7-day average of row counts at the *same hour
     bucket* as now (so we compare 12:00 vs the last 7 weekdays' 12:00,
     not vs end-of-day totals that haven't accrued yet).
  2. If today's count at this hour is below baseline * (1 - drop_pct),
     emit an alert. Use absolute floor too — below 100 rows is always
     an alert regardless of baseline.
  3. Skip the alert outside market hours (no point pinging at 22:00).
  4. Suppress duplicate alerts for the same day via a tiny state file.

Usage:
    uv run python scripts/alert_chain_dropoff.py
    uv run python scripts/alert_chain_dropoff.py --drop-pct 50 --dry-run
    uv run python scripts/alert_chain_dropoff.py --date 2026-04-15  # backfill check

Cron (recommended):
    0 10-15 * * 1-5  uv run python scripts/alert_chain_dropoff.py
    (top of every hour 10–15 IST, weekdays only)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.clock import IST, MarketClock
from src.core.constants import MARKET_CLOSE, MARKET_OPEN

logger = logging.getLogger(__name__)

CHAIN_DIR = Path("data/chain_snapshots")
ALERT_STATE = Path("data/heartbeat/chain_dropoff_state.json")

# Below this absolute row count at any in-market hour we always alert.
# Calibrated from current baseline: the recorder writes ~600 legs/snapshot
# × ~10 snapshots/hour = ~6000 rows/hour. 100 in an hour means almost
# nothing landed.
ABSOLUTE_FLOOR = 100


@dataclass
class HourBucket:
    """Snapshot of how many chain rows had landed by this hour on a given day."""
    day: date
    hour: int
    rows: int


def _count_rows_up_to_hour(path: Path, cutoff_hour: int) -> int:
    """Count rows in the chain CSV whose timestamp hour is <= cutoff_hour.

    A "row" here is one option leg; we sum across underlyings. The CSV may
    contain multiple underlyings' rows interleaved, so we don't assume
    sorted order — we just filter by the time field's hour component.
    """
    if not path.exists():
        return 0
    n = 0
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("time", "")
            # Format: "2026-04-17T11:42:58+05:30" -> hour at chars 11-13
            if len(ts) < 13:
                continue
            try:
                hr = int(ts[11:13])
            except ValueError:
                continue
            if hr <= cutoff_hour:
                n += 1
    return n


def _load_baseline(today: date, hour: int, lookback_days: int = 7) -> tuple[float, list[HourBucket]]:
    """Average row count at `hour` over the previous `lookback_days` trading days.

    Skips weekends / holidays and any day whose CSV is missing or has zero
    rows up to that hour (a missing past day shouldn't drag the baseline
    to zero — that would suppress alerts on the very day we need them).
    """
    clock = MarketClock()
    samples: list[HourBucket] = []
    cursor = today - timedelta(days=1)
    days_inspected = 0
    # Walk back at most 14 calendar days to find lookback_days trading days
    while len(samples) < lookback_days and days_inspected < 14:
        days_inspected += 1
        if not clock.is_trading_holiday(cursor):
            path = CHAIN_DIR / f"chain_{cursor.isoformat()}.csv"
            n = _count_rows_up_to_hour(path, hour)
            if n > 0:
                samples.append(HourBucket(cursor, hour, n))
        cursor -= timedelta(days=1)

    if not samples:
        return 0.0, []
    return sum(s.rows for s in samples) / len(samples), samples


def _load_alert_state() -> dict:
    if not ALERT_STATE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_alert_state(state: dict) -> None:
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ALERT_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(ALERT_STATE)


def _format_alert(today: date, hour: int, today_rows: int, baseline: float,
                  samples: list[HourBucket], drop_pct: float) -> str:
    actual_drop = 0.0
    if baseline > 0:
        actual_drop = 100.0 * (1 - today_rows / baseline)
    sample_summary = ", ".join(
        f"{s.day.strftime('%a')} {s.rows:,}" for s in samples[:5]
    )
    return (
        f"⚠️ *Chain recorder dropoff alert*\n\n"
        f"`{today}` {hour:02d}:00 — only *{today_rows:,}* rows so far.\n"
        f"7-day baseline at this hour: *{baseline:,.0f}* rows "
        f"(*{actual_drop:.0f}%* below).\n"
        f"Threshold: drop > {drop_pct:.0f}% or absolute < {ABSOLUTE_FLOOR}.\n\n"
        f"Recent samples: {sample_summary}\n\n"
        f"Check Kite WS connection and recorder process — file:\n"
        f"`{CHAIN_DIR}/chain_{today}.csv`"
    )


async def _send_telegram(msg: str) -> bool:
    try:
        from src.config import Settings
        from src.notifications.telegram import TelegramNotifier

        settings = Settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram not configured — printing alert instead")
            print(msg)
            return False
        notifier = TelegramNotifier(
            settings.telegram_bot_token, settings.telegram_chat_id
        )
        await notifier.send_message(msg)
        return True
    except Exception as e:
        logger.exception("Telegram send failed: %s", e)
        return False


async def run(
    today: date,
    *,
    drop_pct: float = 30.0,
    dry_run: bool = False,
    force_alert: bool = False,
    skip_market_hours_gate: bool = False,
    now: datetime | None = None,
) -> int:
    """Returns exit code: 0 if healthy or alert sent, 1 if alert needed but suppressed.

    `now` is injectable so tests can pin the wall clock to the hour where
    their fixture data actually lives. Production callers leave it None and
    get `datetime.now(IST)`.
    """
    clock = MarketClock()
    if now is None:
        now = datetime.now(IST)

    if not skip_market_hours_gate:
        if clock.is_trading_holiday(today):
            logger.info("Today is not a trading day — no alert needed")
            return 0
        # Only alert during market hours; outside that window, the nightly
        # audit covers the day's final state.
        if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
            logger.info("Outside market hours — no alert needed")
            return 0

    hour = now.hour
    chain_path = CHAIN_DIR / f"chain_{today.isoformat()}.csv"
    today_rows = _count_rows_up_to_hour(chain_path, hour)
    baseline, samples = _load_baseline(today, hour)

    print(
        f"  {today} hour={hour:02d}  today={today_rows:,} rows  "
        f"baseline={baseline:,.0f} (n={len(samples)})"
    )

    # Decide whether to alert
    needs_alert = False
    reason = ""
    if today_rows < ABSOLUTE_FLOOR:
        needs_alert = True
        reason = f"absolute floor ({today_rows} < {ABSOLUTE_FLOOR})"
    elif baseline > 0 and today_rows < baseline * (1 - drop_pct / 100.0):
        needs_alert = True
        reason = f"drop > {drop_pct:.0f}% vs 7-day avg"
    elif baseline == 0 and today_rows == 0:
        needs_alert = True
        reason = "no rows yet and no baseline"

    if not needs_alert and not force_alert:
        print(f"  OK: {reason or 'within bounds'}")
        return 0

    # Suppress duplicate alerts for the same day:hour pair to avoid spamming
    state = _load_alert_state()
    key = f"{today.isoformat()}:{hour}"
    if state.get(key) and not force_alert:
        print(f"  Already alerted at {key} — suppressing")
        return 1

    msg = _format_alert(today, hour, today_rows, baseline, samples, drop_pct)
    print(f"\n  ALERT ({reason}):\n{msg}\n")

    if dry_run:
        return 0

    sent = await _send_telegram(msg)
    if sent:
        state[key] = {"sent_at": now.isoformat(), "rows": today_rows, "baseline": baseline}
        _save_alert_state(state)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Chain CSV row-count dropoff alert")
    p.add_argument("--date", type=str, default=None,
                   help="Override 'today' (YYYY-MM-DD), useful for backfill checks")
    p.add_argument("--drop-pct", type=float, default=30.0,
                   help="Alert when today drops more than this %% vs 7-day avg")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute + print but don't send Telegram or update state")
    p.add_argument("--force", action="store_true",
                   help="Send alert even if suppressed by state")
    p.add_argument("--anytime", action="store_true",
                   help="Skip the market-hours gate (for backfill / manual runs)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    today = date.fromisoformat(args.date) if args.date else date.today()
    return asyncio.run(run(
        today,
        drop_pct=args.drop_pct,
        dry_run=args.dry_run,
        force_alert=args.force,
        skip_market_hours_gate=args.anytime,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
