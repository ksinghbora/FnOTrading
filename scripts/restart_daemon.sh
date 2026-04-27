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

REPO="/Users/kundanbora/code/FnOTrading"
# PID file lives OUTSIDE ~/Documents/ (Apr 21 fix). macOS Tahoe TCC denies
# launchd-spawned /bin/bash from reading files under ~/Documents/ even when
# bash has Full Disk Access — see plist comment for why. Symptom: the bash
# `cat "$PIDFILE"` returned EPERM, the script overwrote a fictitious "stale"
# entry, and the new daemon collided with the still-running old one on port
# 8000. The state-dir under ~/Library/Application Support/ is not protected
# by TCC, so launchd-bash can read it without any per-binary grant.
STATE_DIR="$HOME/Library/Application Support/fno-trading"
mkdir -p "$STATE_DIR"
PIDFILE="$STATE_DIR/fno_main.pid"
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
# Subshell-orphan pattern is REQUIRED on macOS:
#   - Plain `nohup ... &` from this script left python orphaned to bash's
#     process group. When bash exited at the end of step 4, python received
#     SIGTERM (the Apr 22 08:50 incident: uvicorn logged "Shutting down" 1s
#     after the script said "Restart complete", and the daemon was dead by
#     09:00 with no trades all morning).
#   - `setsid` would be cleaner but isn't installed on macOS by default.
#   - `(cmd &)` runs in a subshell that exits immediately, leaving the
#     daemon as an orphan owned by launchd/init — which is what we want.
#   - Stdin redirected from /dev/null so the daemon doesn't inherit a tty
#     fd that could deliver SIGHUP later.
mkdir -p "$LOGDIR"
LOG="$LOGDIR/main_$(date +%Y%m%d_%H%M%S).log"
( nohup "$UV" run python -m src.main > "$LOG" 2>&1 < /dev/null & echo $! > "$PIDFILE.tmp" )
# Subshell wrote PID asynchronously; brief poll for the file.
for _ in 1 2 3 4 5; do
    [ -s "$PIDFILE.tmp" ] && break
    sleep 0.2
done
NEW_PID=$(cat "$PIDFILE.tmp" 2>/dev/null || echo "")
rm -f "$PIDFILE.tmp"
if [ -z "$NEW_PID" ]; then
    log "ERROR: failed to capture new daemon PID."
    exit 1
fi
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
