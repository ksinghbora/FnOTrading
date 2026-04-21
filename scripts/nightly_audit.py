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

# Apr 17 audit: chain CSV quality check piggybacks on the nightly cron so
# we hear about a corrupted recording day THE SAME EVENING, not 3 weeks
# later when we try to replay.
from scripts.audit_chain_quality import audit_file as audit_chain_file
from src.observability.heartbeat import read_heartbeat


async def run(
    target_date: date,
    *,
    dry_run: bool = False,
    skip_telegram: bool = False,
):
    settings = Settings()

    print(f"Nightly Audit for {target_date}")
    print("=" * 50)

    # Open one DB engine for the whole run — the confluence_audits writer
    # (§4) AND the counterfactual replay's DB mirror (§3e) both need it.
    # Best-effort: if the DB is down, both writers degrade gracefully but
    # the rest of the audit (JSONL + Telegram) still ships.
    db_engine = None
    db_session_factory = None
    if not dry_run:
        try:
            from src.db.session import create_db_engine, create_session_factory
            db_engine = create_db_engine(settings.database_url)
            db_session_factory = create_session_factory(db_engine)
        except Exception as e:
            print(f"  DB engine init failed (non-critical): {e}")

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

    # 3b. Chain CSV quality check for today's recording.
    # If today's CSV is missing or degraded, surface it immediately —
    # otherwise the bad data sits on disk until the next replay run weeks
    # later, by which point the broker session is gone and we can't recover.
    chain_path = Path("data/chain_snapshots") / f"chain_{target_date.isoformat()}.csv"
    chain_quality_msg: str | None = None
    if chain_path.exists():
        rep = audit_chain_file(chain_path)
        print(
            f"\n  Chain CSV: {rep.rows} rows / {rep.unique_minutes} mins  "
            f"price={rep.priceable_pct:.0f}%  iv={rep.iv_pct:.0f}%  "
            f"bidask={rep.bidask_pct:.0f}%  → {rep.status.upper()}"
        )
        if rep.status in ("degraded", "partial"):
            chain_quality_msg = (
                f"⚠️ Chain CSV quality {rep.status.upper()} for {target_date}: "
                f"price={rep.priceable_pct:.0f}% iv={rep.iv_pct:.0f}% "
                f"bidask={rep.bidask_pct:.0f}%"
            )
    else:
        print(f"\n  Chain CSV: MISSING for {target_date}")
        chain_quality_msg = f"⚠️ Chain CSV MISSING for {target_date} — recorder did not write today"

    # 3c. Recorder heartbeat / per-stream coverage check.
    # The nightly run is the natural place to call out streams that under-
    # delivered today (e.g. tick recorder saw 0 instruments because the
    # subscription list never expanded). Don't fail the audit on this —
    # it's informational. The hourly dropoff alert is the real-time arm.
    heartbeat_msg: str | None = None
    hb = read_heartbeat()
    if hb:
        streams = hb.get("streams", {}) or {}
        chain_today = streams.get("chain_recorder", {}).get("snapshots_today", 0)
        vix_today = streams.get("vix_recorder", {}).get("minutes_today", 0)
        ticks_today = streams.get("tick_recorder", {}).get("ticks_today", 0)
        instruments = streams.get("tick_recorder", {}).get("instruments_active", 0)
        print(
            f"\n  Heartbeat: chain_snaps={chain_today}  vix_min={vix_today}  "
            f"ticks={ticks_today:,} ({instruments} insts)  pid={hb.get('pid')}"
        )
        # Empty-stream alert: any 0 during a known trading day is suspicious.
        empty_streams = []
        if chain_today == 0:
            empty_streams.append("chain")
        if vix_today == 0:
            empty_streams.append("vix")
        if ticks_today == 0:
            empty_streams.append("ticks")
        if empty_streams:
            heartbeat_msg = (
                f"⚠️ Recorder streams empty today ({target_date}): "
                f"{', '.join(empty_streams)} — check WS subscription / process logs"
            )
    else:
        print("\n  Heartbeat: MISSING (data/heartbeat/recorder.json not found)")
        heartbeat_msg = f"⚠️ Recorder heartbeat MISSING for {target_date}"

    # 3d. Trader heartbeat (split mode only). Recorder being healthy doesn't
    # mean strategies ran — the trader may have crashed independently.
    if settings.recorder_split_mode:
        trader_hb = read_heartbeat("data/heartbeat/trader.json")
        if trader_hb:
            print(f"  Trader heartbeat OK (pid={trader_hb.get('pid')})")
        else:
            print("  Trader heartbeat: MISSING (data/heartbeat/trader.json not found)")
            trader_msg = f"⚠️ Trader heartbeat MISSING for {target_date}"
            heartbeat_msg = f"{heartbeat_msg}\n{trader_msg}" if heartbeat_msg else trader_msg

    # 3e. Counterfactual replay (DATA_RELIABILITY_PLAN §7.5).
    # Replay today through the backtest engine with frozen-day-of params,
    # compare against live actual P&L. A divergence > ₹500 (SLO §8.7) means
    # backtest and production no longer agree — usually a param desync, a
    # filter bug, or a slippage gap. Surfacing it the same evening keeps the
    # debug loop short. Runs after the audit/heartbeat sections so a replay
    # crash can never wipe the rest of the report.
    counterfactual_msg: str | None = None
    try:
        from scripts.replay_counterfactual import compare_live_vs_replay
        cf = await compare_live_vs_replay(
            target_date=target_date,
            strategy_name="portfolio",
            settings=settings,
            db_session_factory=db_session_factory,
        )
        print(
            f"\n  Counterfactual: live={cf.live_pnl:+,.0f}  replay={cf.replay_pnl:+,.0f}  "
            f"div={cf.divergence:,.0f} (tol {cf.tolerance:,.0f})  "
            f"breached={cf.breached}  hash={cf.replay_hash[:12] if cf.replay_hash else 'n/a'}"
        )
        # Surface the params_source so the operator sees at a glance whether
        # this comparison used frozen-day-of params (snapshot:...) or the
        # current live defaults (live / live (no mapping)). The latter means
        # the divergence number is only meaningful if params haven't drifted
        # since the trading day.
        fallback_marker = " ⚠️ FALLBACK" if cf.params_source_is_fallback else ""
        print(f"  Counterfactual params_source: {cf.params_source}{fallback_marker}")
        if cf.replay_error:
            print(f"  Counterfactual replay error: {cf.replay_error}")
        if cf.telegram_message:
            counterfactual_msg = cf.telegram_message
    except Exception as e:
        # Don't let the counterfactual block shipping the rest of the audit.
        print(f"\n  Counterfactual: SKIPPED ({type(e).__name__}: {e})")

    # 3f. Trend score shadow metrics — Phase B of memory/score_validation_plan.md.
    # The Apr 18 trend score rebalance lands in the "needs shadow data" band,
    # so the operator wants a daily readout of (a) trend entry-rate vs the
    # historical median and (b) per-factor activation health, with a hard
    # rollback trigger on 5 consecutive zero-entry days. Wraps in try/except
    # because the script imports cleanly but the underlying decision-log
    # readers can fail on schema drift — and the rest of the audit shouldn't
    # block on a diagnostic.
    shadow_msg: str | None = None
    try:
        # Import lazily so test environments without scripts/ on path don't choke.
        # The sys.modules registration before exec_module is REQUIRED on
        # Python 3.14+: dataclass's _is_type reads sys.modules[cls.__module__]
        # to resolve KW_ONLY context, and a dynamically-loaded module that
        # isn't registered hits AttributeError on the second @dataclass.
        import importlib.util
        import sys as _sys
        spec = importlib.util.spec_from_file_location(
            "trend_shadow_metrics",
            Path(__file__).parent / "trend_shadow_metrics.py",
        )
        tsm = importlib.util.module_from_spec(spec)
        _sys.modules["trend_shadow_metrics"] = tsm
        spec.loader.exec_module(tsm)

        from datetime import timedelta as _td
        # 30-day trailing window ending on the audit's target_date so a
        # backfill run (--date) reflects what the operator would have seen
        # on that evening, not today's full corpus.
        since = target_date - _td(days=30)
        phase_a, pre_a, _skipped = tsm.collect_rows(since, target_date, strategy_id=None)
        if phase_a or pre_a:
            metrics = tsm.build_metrics(since, target_date, phase_a, pre_a, _skipped)
            band_emoji = {"PASS": "✅", "WARN": "🟧", "FAIL": "🟥"}.get(metrics.band, "❓")
            print(
                f"\n  Trend shadow ({since}→{target_date}): "
                f"entries={metrics.total_entries} "
                f"median/day={metrics.median_entries_per_day} "
                f"phase_a={metrics.phase_a_entries} "
                f"band={band_emoji} {metrics.band}"
            )
            if metrics.band in ("WARN", "FAIL"):
                shadow_msg = (
                    f"{band_emoji} Trend shadow band={metrics.band} for {target_date} "
                    f"(30d median={metrics.median_entries_per_day}/day, "
                    f"max consec zero days={metrics.consecutive_fail_days}). "
                    f"See memory/score_validation_plan.md decision tree."
                )
            # Persist the JSON for later trending (one file per month).
            if not dry_run:
                output = Path("data") / f"trend_shadow_{since.strftime('%Y-%m')}.json"
                tsm.write_json(metrics, output)
        else:
            print(f"\n  Trend shadow: no decision logs in {since}→{target_date}")
    except Exception as e:
        print(f"\n  Trend shadow: SKIPPED ({type(e).__name__}: {e})")

    # 4. Save results
    if not dry_run:
        save_audit_json(audit)
        print("\n  Saved: data/latest_audit.json")

        # Persist to DB for historical analysis. Reuses the engine opened
        # at the top of the run (also feeding §3e counterfactual's DB mirror).
        if db_session_factory is not None:
            try:
                await save_audit_db(audit, db_session_factory)
                print("  Saved to DB: confluence_audits table")
            except Exception as e:
                print(f"  DB save failed (non-critical): {e}")
        else:
            print("  DB save skipped (engine init failed earlier)")

        # Send to Telegram
        if not skip_telegram and settings.telegram_bot_token:
            try:
                from src.notifications.telegram import TelegramNotifier
                notifier = TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id
                )
                msg = format_audit_telegram(audit)
                if chain_quality_msg:
                    msg = f"{msg}\n\n{chain_quality_msg}"
                if heartbeat_msg:
                    msg = f"{msg}\n\n{heartbeat_msg}"
                if counterfactual_msg:
                    msg = f"{msg}\n\n{counterfactual_msg}"
                if shadow_msg:
                    msg = f"{msg}\n\n{shadow_msg}"
                await notifier.send_message(msg)
                print("  Sent audit to Telegram")
            except Exception as e:
                print(f"  Telegram send failed: {e}")
    else:
        print("\n  [DRY RUN] Not saving or sending anything")
        print()
        print(format_audit_telegram(audit))

    # Dispose the shared engine — both §3e and §4 are done with it by now.
    if db_engine is not None:
        try:
            await db_engine.dispose()
        except Exception as e:
            print(f"  DB engine dispose failed: {e}")

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
