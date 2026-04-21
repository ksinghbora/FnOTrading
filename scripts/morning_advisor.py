"""Pre-market AI advisor — generates DayBias for today's trading session.

Run before market open (~9:00 AM IST). The strategy picks up
data/day_bias.json on start or daily reset.

Usage:
    uv run python scripts/morning_advisor.py
    uv run python scripts/morning_advisor.py --dry-run
    uv run python scripts/morning_advisor.py --skip-external
    uv run python scripts/morning_advisor.py --date 2026-03-20

Cron: 0 9 * * 1-5
"""

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.advisor.analyzer import analyze_with_claude
from src.advisor.collector import collect_today_data
from src.advisor.context import fetch_external_context
from src.advisor.formatter import format_advisory_telegram
from src.advisor.store import save_advisory_json, save_day_bias_json, save_advisory_db
from src.config import Settings


async def run(
    target_date: date,
    *,
    dry_run: bool = False,
    skip_external: bool = False,
    skip_telegram: bool = False,
):
    settings = Settings()

    print(f"Morning Advisor for {target_date}")
    print("=" * 50)

    # 1. Collect yesterday's data (for context)
    yesterday = target_date - timedelta(days=1)
    log_dir = Path("logs")
    today_data = await collect_today_data(
        target_date=yesterday,
        log_dir=log_dir if log_dir.exists() else None,
        settings=settings,
    )
    print(f"  Yesterday's P&L: {today_data.total_pnl:+,.0f}")
    print(f"  Trades: {today_data.trades_count}")

    # 2. Fetch external context for today
    calendar_path = Path("data/economic_calendar.json")
    context = await fetch_external_context(
        target_date=target_date,
        calendar_path=calendar_path,
        skip_external=skip_external,
    )
    print(f"  News headlines: {len(context.news_headlines)}")
    print(f"  FII/DII: {'available' if context.fii_dii else 'unavailable'}")
    print(f"  Events: {len(context.events)}")
    print(f"  Sources: {context.available_sources}")

    # 3. Analyze with Claude
    advisory = await analyze_with_claude(today_data, context, settings)
    bias = advisory.day_bias

    print()
    print(f"  Risk Level: {bias.risk_level}")
    print(f"  Confidence: {bias.confidence:.0%}")
    print(f"  Mode Bias: {bias.mode_bias}")
    print(f"  Sizing: {bias.sizing_multiplier:.1f}x")
    print(f"  Premium Adj: {bias.premium_score_adj:+d} (conf={bias.premium_confidence:.0%})")
    print(f"  Trend Adj: {bias.trend_score_adj:+d} (conf={bias.trend_confidence:.0%})")
    if advisory.token_usage:
        print(f"  Tokens: {advisory.token_usage}")

    if bias.reasoning:
        print(f"\n  Reasoning: {bias.reasoning[:300]}")

    # 4. Save results
    if not dry_run:
        save_day_bias_json(advisory)
        save_advisory_json(advisory)
        print("\n  Saved: data/day_bias.json + data/latest_advisory.json")

        # Persist to DB for historical analysis
        try:
            from src.db.session import create_db_engine, create_session_factory
            engine = create_db_engine(settings.database_url)
            session_factory = create_session_factory(engine)
            await save_advisory_db(advisory, session_factory)
            await engine.dispose()
            print("  Saved to DB: advisories table")
        except Exception as e:
            print(f"  DB save failed (non-critical): {e}")

        # Send to Telegram
        if not skip_telegram and settings.telegram_bot_token:
            try:
                from src.notifications.telegram import TelegramNotifier
                notifier = TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id
                )
                msg = format_advisory_telegram(advisory)
                await notifier.send_message(msg)
                print("  Sent to Telegram")
            except Exception as e:
                print(f"  Telegram send failed: {e}")
    else:
        print("\n  [DRY RUN] Not saving or sending anything")
        print()
        print(format_advisory_telegram(advisory))

    print("\n" + "=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Morning AI Advisor")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't save or send")
    parser.add_argument("--skip-external", action="store_true", help="Skip news/FII fetching")
    parser.add_argument("--skip-telegram", action="store_true", help="Don't send to Telegram")
    parser.add_argument("--date", type=str, default=None, help="Target date (YYYY-MM-DD)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target = date.fromisoformat(args.date) if args.date else date.today()

    asyncio.run(run(
        target_date=target,
        dry_run=args.dry_run,
        skip_external=args.skip_external,
        skip_telegram=args.skip_telegram,
    ))


if __name__ == "__main__":
    main()
