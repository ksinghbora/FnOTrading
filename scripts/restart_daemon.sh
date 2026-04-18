#!/usr/bin/env bash
# Restart the FnO trading daemon. Driven by
# ~/Library/LaunchAgents/com.fno.daemon-restart.plist at 08:50 IST every weekday.
#
# WHY THIS EXISTS
# ---------------
# src/main.py:830 has a one-shot holiday guard checked at startup ONLY. A daemon
# that boots on Sat/Sun/holiday sits in recorder-only mode and never auto-loads
# strategies, even after the calendar rolls into a trading day. We bounce the
# process before each market open so it re-evaluates the gate against today.
#
# Internal NSE-holiday check makes this a safe no-op on Holi/Eid/Diwali/etc that
# fall on weekdays — the launchd schedule alone can't filter those out.
#
# Idempotent: not-a-trading-day → exit 0 without touching anything.
#             dead PID file     → overwrite.
#             healthy daemon    → SIGTERM, wait up to 30s, then SIGKILL fallback.

set -euo pipefail

REPO="/Users/kundanbora/Documents/FnOTrading"
PIDFILE="$REPO/.fno_main.pid"
LOGDIR="$REPO/logs"
UV="/opt/homebrew/bin/uv"

cd "$REPO"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ─── 1. NSE trading-day gate ─────────────────────────────────────────
# Exits 0 if today is a NSE trading day, 1 otherwise. We do this in Python
# so the holiday calendar (MarketClock) is the single source of truth — no
# duplicating the holiday list in shell.
if ! "$UV" run python -c "
import sys
from datetime import date
from src.core.clock import MarketClock
d = date.today()
mc = MarketClock()
trading = (d.weekday() < 5) and (not mc.is_trading_holiday(d))
sys.exit(0 if trading else 1)
" 2>/dev/null; then
    log "Not an NSE trading day ($(date +%A)) — skipping restart."
    exit 0
fi

# ─── 2. Graceful stop of existing daemon ─────────────────────────────
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        log "Sending SIGTERM to PID $OLD_PID..."
        kill -TERM "$OLD_PID"
        for i in $(seq 1 30); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                log "PID $OLD_PID exited gracefully after ${i}s."
                break
            fi
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            log "Graceful shutdown timeout — sending SIGKILL."
            kill -KILL "$OLD_PID" || true
            sleep 1
        fi
    else
        log "Stale PID file ($OLD_PID is dead) — overwriting."
    fi
fi

# ─── 3. Launch fresh daemon ──────────────────────────────────────────
mkdir -p "$LOGDIR"
LOG="$LOGDIR/main_$(date +%Y%m%d_%H%M%S).log"
nohup "$UV" run python -m src.main > "$LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
log "Started daemon PID=$NEW_PID, log=$(basename "$LOG")"

# ─── 4. Liveness check ───────────────────────────────────────────────
# 10s is enough for the early imports + DB connect to either succeed or
# crash. The Telegram startup ping (verify_system shows it sends one)
# gives us a second-channel confirmation when this passes.
sleep 10
if ! kill -0 "$NEW_PID" 2>/dev/null; then
    log "ERROR: daemon died within 10s. Tail of log:"
    tail -40 "$LOG" || true
    exit 1
fi
log "Daemon healthy. Restart complete."
