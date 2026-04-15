"""Post-market audit — compare AI advisor vs rule-based decisions.

Run after market close (~4:00 PM IST). Builds the confluence
scorecard and sends summary to Telegram.

Usage:
    uv run python scripts/nightly_audit.py
    uv run python scripts/nightly_audit.py --dry-run
    uv run python scripts/nightly_audit.py --date 2026-03-20

Cron: 0 16 * * 1-5
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.advisor.collector import collect_today_data
from src.advisor.formatter import format_audit_telegram
from src.advisor.shadow import build_audit
from src.advisor.store import load_advisory_json, save_audit_json, save_audit_db
from src.config import Settings


async def run(
    target_date: date,
    *,
    dry_run: bool = False,
    skip_telegram: bool = False,
):
    settings = Settings()

    print(f"Nightly Audit for {target_date}")
    print("=" * 50)

    # 1. Collect today's actual trading data
    log_dir = Path("logs")
    today_data = await collect_today_data(
        target_date=target_date,
        log_dir=log_dir if log_dir.exists() else None,
        settings=settings,
    )
    print(f"  Today's P&L: {today_data.total_pnl:+,.0f}")
    print(f"  Premium: {today_data.premium_leg.pnl:+,.0f} ({today_data.premium_leg.trades} trades)")
    print(f"  Trend: {today_data.trend_leg.pnl:+,.0f} ({today_data.trend_leg.trades} trades)")

    # 2. Load today's advisory (if generated this morning)
    advisory = load_advisory_json()
    if advisory and advisory.date == target_date:
        bias = advisory.day_bias
        print(f"\n  Morning Advisory: risk={bias.risk_level} conf={bias.confidence:.0%}")
        print(f"  Prem adj: {bias.premium_score_adj:+d}  Trend adj: {bias.trend_score_adj:+d}")
    else:
        print("\n  No advisory was generated for today")

    # 3. Build confluence audit
    audit = build_audit(
        target_date=target_date,
        log_dir=log_dir if log_dir.exists() else None,
        actual_pnl=today_data.total_pnl,
    )

    print(f"\n  Confluence Decisions: {len(audit.decisions)}")
    print(f"  Agree: {audit.agree_count}  Disagree: {audit.disagree_count}")
    if audit.disagree_count > 0:
        print(f"  AI right: {audit.ai_right_count}  Rules right: {audit.rule_right_count}")
    print(f"  AI Alpha: {audit.ai_alpha:+,.0f}")

    # 4. Save results
    if not dry_run:
        save_audit_json(audit)
        print("\n  Saved: data/latest_audit.json")

        # Persist to DB for historical analysis
        try:
            from src.db.session import create_db_engine, create_session_factory
            engine = create_db_engine(settings.database_url)
            session_factory = create_session_factory(engine)
            await save_audit_db(audit, session_factory)
            await engine.dispose()
            print("  Saved to DB: confluence_audits table")
        except Exception as e:
            print(f"  DB save failed (non-critical): {e}")

        # Send to Telegram
        if not skip_telegram and settings.telegram_bot_token:
            try:
                from src.notifications.telegram import TelegramNotifier
                notifier = TelegramNotifier(settings)
                msg = format_audit_telegram(audit)
                await notifier.send_message(msg)
                print("  Sent audit to Telegram")
            except Exception as e:
                print(f"  Telegram send failed: {e}")
    else:
        print("\n  [DRY RUN] Not saving or sending anything")
        print()
        print(format_audit_telegram(audit))

    print("\n" + "=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Nightly Confluence Audit")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't save or send")
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
        skip_telegram=args.skip_telegram,
    ))


if __name__ == "__main__":
    main()
