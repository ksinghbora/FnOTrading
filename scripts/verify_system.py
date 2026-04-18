"""Pre-market system verification — run before 9:15 AM to catch issues early.

Usage: uv run python scripts/verify_system.py
"""

import asyncio
import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Color codes for terminal
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
CHECK = f"{GREEN}✓{RESET}"
CROSS = f"{RED}✗{RESET}"
WARN = f"{YELLOW}⚠{RESET}"

failures = []
warnings = []


def ok(msg: str):
    print(f"  {CHECK} {msg}")


def fail(msg: str):
    print(f"  {CROSS} {msg}")
    failures.append(msg)


def warn(msg: str):
    print(f"  {WARN} {msg}")
    warnings.append(msg)


async def main():
    from src.config import Settings
    settings = Settings()

    # ─── 1. Config & Credentials ────────────────────────────────
    print("\n1. Configuration & Credentials")

    if settings.kite_api_key and settings.kite_access_token:
        ok(f"Kite credentials present (key={settings.kite_api_key[:8]}...)")
    else:
        fail("Kite credentials missing — no live data")

    if settings.anthropic_api_key:
        ok("Anthropic API key present")
    else:
        warn("No Anthropic API key — advisor disabled")

    if settings.telegram_bot_token:
        ok("Telegram bot token present")
    else:
        warn("No Telegram token — no notifications")

    # ─── 2. Database ─────────────────────────────────────────────
    print("\n2. Database")
    try:
        from src.db.session import create_db_engine, create_session_factory
        engine = create_db_engine(settings.database_url)
        sf = create_session_factory(engine)
        async with sf() as session:
            await session.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
        ok("Database connection OK")
    except Exception as e:
        fail(f"Database connection failed: {e}")
        engine = None

    # ─── 3. Broker Connection ─────────────────────────────────────
    print("\n3. Broker Connection")
    try:
        from src.broker.zerodha.client import ZerodhaClient
        broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
        await broker.connect()

        # Test LTP fetch
        ltp = await broker.get_ltp(["NSE:NIFTY 50"])
        nifty_price = ltp.get("NSE:NIFTY 50", 0)
        if nifty_price > 0:
            ok(f"Kite API working — NIFTY LTP = {nifty_price}")
        else:
            fail("Kite LTP returned 0 — token may be expired")
    except Exception as e:
        fail(f"Broker connection failed: {e}")
        nifty_price = 0

    # ─── 4. Instruments ──────────────────────────────────────────
    print("\n4. Instruments")
    try:
        from src.broker.zerodha.instruments import InstrumentManager
        im = InstrumentManager(broker, sf)
        await im.load_from_db()
        total = len(im._instruments)
        if total > 1000:
            ok(f"Loaded {total} instruments from DB")
        else:
            fail(f"Only {total} instruments — run download_instruments.py")
    except Exception as e:
        fail(f"Instrument load failed: {e}")
        im = None

    # ─── 5. Option Chain for Next Expiry ──────────────────────────
    print("\n5. Option Chain Instruments")
    try:
        from src.core.clock import MarketClock
        clock = MarketClock()
        expiry = clock.next_expiry("NIFTY")
        options = im.get_option_chain_instruments("NIFTY", expiry) if im else []

        if len(options) > 20:
            ok(f"NIFTY expiry={expiry}: {len(options)} option instruments available")
        else:
            warn(f"NIFTY expiry={expiry}: only {len(options)} options — auto-downloading instruments...")
            try:
                count = await im.download_and_store()
                options = im.get_option_chain_instruments("NIFTY", expiry)
                if len(options) > 20:
                    ok(f"Auto-downloaded {count} instruments. NIFTY expiry={expiry}: {len(options)} options now available")
                else:
                    fail(f"After download still only {len(options)} options for {expiry}. Check if expiry date is correct.")
            except Exception as e2:
                fail(f"Auto-download failed: {e2}. Run download_instruments.py manually.")

        # Check if ATM strikes exist
        if nifty_price > 0 and options:
            atm = round(nifty_price / 50) * 50
            atm_strikes = [o for o in options if abs(float(o.strike) - atm) <= 250]
            if len(atm_strikes) >= 10:
                ok(f"ATM±250 strikes: {len(atm_strikes)} instruments (ATM={atm})")
            else:
                fail(f"Only {len(atm_strikes)} strikes near ATM={atm}")

            # Check wing strikes exist (for IC)
            wing_strike = atm + 250  # 5 × 50
            wing_exists = any(float(o.strike) == wing_strike for o in options)
            if wing_exists:
                ok(f"IC wing strike {wing_strike} exists")
            else:
                warn(f"IC wing strike {wing_strike} not in instruments")
        # Validate expiry matches Kite's actual data (catch holiday calendar mismatches)
        if im and nifty_price > 0:
            all_nifty = im.get_fno_instruments(underlying="NIFTY")
            kite_expiries = sorted(set(i.expiry for i in all_nifty if i.expiry and i.expiry >= date.today()))
            if kite_expiries:
                nearest_kite = kite_expiries[0]
                if nearest_kite != expiry:
                    fail(
                        f"EXPIRY MISMATCH: clock says {expiry} but Kite instruments say {nearest_kite}. "
                        f"Holiday calendar in constants.py is likely wrong."
                    )
                else:
                    ok(f"Expiry {expiry} matches Kite instrument data")
    except Exception as e:
        fail(f"Option chain check failed: {e}")

    # ─── 6. Chain Builder Registration ────────────────────────────
    print("\n6. Chain Builder Registration (simulated)")
    try:
        from src.core.events import EventBus
        from src.core.types import OptionType
        from src.market_data.option_chain import OptionChainBuilder

        eb = EventBus()
        cb = OptionChainBuilder(eb, clock)

        # Register spot
        cb.register_spot(256265, "NIFTY")

        # Register options like main.py does
        if nifty_price > 0 and options:
            atm_est = round(nifty_price / 50) * 50
            lo, hi = atm_est - 1000, atm_est + 1000
            registered = 0
            for inst in options:
                if lo <= float(inst.strike) <= hi:
                    cb.register_option(
                        inst.instrument_token, "NIFTY", expiry,
                        inst.strike, OptionType(inst.instrument_type.value),
                        inst.tradingsymbol,
                    )
                    registered += 1

            if registered > 20:
                ok(f"Registered {registered} options in chain builder")
            else:
                fail(f"Only {registered} options registered")

            # Check chain exists
            chain = cb.get_chain("NIFTY", expiry)
            if chain:
                ok(f"Chain created for NIFTY {expiry}")
            else:
                fail("Chain not created after registration")
        else:
            warn("Skipped — no spot price or instruments")
    except Exception as e:
        fail(f"Chain builder test failed: {e}")

    # ─── 7. Paper Broker Symbol Matching ──────────────────────────
    print("\n7. Paper Broker Symbol Format")
    if options:
        sample = options[0]
        sym = sample.tradingsymbol
        ok(f"Sample tradingsymbol format: {sym}")
        # Check it looks reasonable
        if "NIFTY" in sym and any(c.isdigit() for c in sym):
            ok("Symbol format looks correct for paper broker LTP cache")
        else:
            warn(f"Unusual symbol format: {sym}")

    # ─── 8. Advisor / DayBias ─────────────────────────────────────
    print("\n8. AI Advisor")
    from pathlib import Path
    bias_path = Path("data/day_bias.json")
    if bias_path.exists():
        import json
        try:
            with open(bias_path) as f:
                bias = json.load(f)
            bias_date = bias.get("date", "unknown")
            today = date.today().isoformat()
            if bias_date == today:
                ok(f"DayBias for today: risk={bias.get('risk_level')} conf={bias.get('confidence')}")
            else:
                warn(f"DayBias is stale (date={bias_date}, today={today})")
        except Exception as e:
            warn(f"DayBias file corrupt: {e}")
    else:
        warn("No day_bias.json — advisor hasn't run yet (will run at 9:00 AM)")

    # ─── 9. Server Health ─────────────────────────────────────────
    print("\n9. Server Health")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://localhost:8000/api/dashboard/summary")
            if resp.status_code == 200:
                data = resp.json()
                strats = data.get("strategies", {})
                ok(f"API server responding — {strats.get('active', 0)}/{strats.get('total', 0)} strategies active")
            else:
                fail(f"API returned {resp.status_code}")
    except Exception:
        warn("Server not running (expected if running verify before startup)")

    # ─── 10a. Heartbeat freshness (recorder + trader) ─────────────
    # In split mode (recorder_split_mode=True) the recorder and trader each
    # own a heartbeat file. In legacy single-process mode only the recorder
    # one is written. Check whichever exists; warn separately for each
    # process so a half-down system shows up clearly.
    print("\n10a. Process heartbeats")
    try:
        from src.observability.heartbeat import heartbeat_age_seconds, read_heartbeat

        def _check(name: str, path: str) -> None:
            hb = read_heartbeat(path)
            if hb is None:
                # Trader heartbeat is only written in split mode; missing in
                # legacy mode is expected.
                if name == "trader" and not settings.recorder_split_mode:
                    return
                warn(f"{name}: {path} missing — process hasn't run yet today")
                return
            age = heartbeat_age_seconds(path)
            streams = hb.get("streams", {}) or {}
            stream_summary = ", ".join(
                f"{s}: last={info.get('last_update_ts','?')[-8:]}"
                for s, info in streams.items()
            ) or "no streams reported"
            if age is None:
                warn(f"{name}: heartbeat present but timestamp unreadable; {stream_summary}")
            elif age < 90:
                ok(f"{name}: fresh ({age:.0f}s ago, pid={hb.get('pid')}); {stream_summary}")
            elif age < 600:
                warn(f"{name}: stale ({age:.0f}s ago) — may be paused; {stream_summary}")
            else:
                warn(f"{name}: very stale ({age/60:.0f} min ago) — appears down")

        _check("recorder", "data/heartbeat/recorder.json")
        _check("trader", "data/heartbeat/trader.json")
    except Exception as e:
        warn(f"Heartbeat check error: {e}")

    # ─── 10b. Config snapshot freshness ───────────────────────────
    # We run at 06:30 IST, before the 09:00 IST snapshotter cron, so checking
    # *today's* snapshot would always warn and train operators to ignore the
    # message. Instead check the most recent weekday before today — if THAT
    # snapshot is missing or partial, the cron is genuinely broken and the
    # silent-fallback failure mode the counterfactual warns about is real.
    print("\n10b. Config snapshot freshness (yesterday)")
    try:
        from src.observability.snapshot_freshness import check_snapshot_freshness

        # Walk back to the most recent weekday. Holidays are best-effort —
        # they'll show as a one-off warn the morning after a holiday and
        # operators can ignore that day. Worth that cost vs. importing
        # the holiday calendar here.
        target = date.today() - timedelta(days=1)
        while target.weekday() >= 5:  # 5=Sat, 6=Sun
            target -= timedelta(days=1)

        rep = check_snapshot_freshness(target)
        if rep.level == "ok":
            ok(rep.message)
        elif rep.level == "warn":
            warn(rep.message)
        else:
            fail(rep.message)
    except Exception as e:
        warn(f"Snapshot freshness check error: {e}")

    # ─── 10. Telegram Test ────────────────────────────────────────
    print("\n10. Telegram")
    if settings.telegram_bot_token:
        try:
            from src.notifications.telegram import TelegramNotifier
            tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
            await tg.send_message("Pre-market system check: All systems verified.", parse_mode="")
            ok("Telegram test message sent")
        except Exception as e:
            fail(f"Telegram send failed: {e}")

    # ─── Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 50)
    if failures:
        print(f"{RED}FAILED: {len(failures)} critical issues{RESET}")
        for f in failures:
            print(f"  {CROSS} {f}")
    if warnings:
        print(f"{YELLOW}WARNINGS: {len(warnings)}{RESET}")
        for w in warnings:
            print(f"  {WARN} {w}")
    if not failures and not warnings:
        print(f"{GREEN}ALL CHECKS PASSED — system ready for trading{RESET}")
    elif not failures:
        print(f"{GREEN}No critical issues — {len(warnings)} warnings (non-blocking){RESET}")
    print("=" * 50)

    # Cleanup
    if engine:
        await engine.dispose()

    return 1 if failures else 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
