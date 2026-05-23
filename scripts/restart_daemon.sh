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

# ─── 1.5. Autonomous calibration executor ────────────────────────────
# V7.3 (May 18 2026) — apply daily calibration plan BEFORE daemon
# restart so the fresh daemon picks up the new config in one cycle.
# The executor is idempotent (no-op if today's stage already applied)
# and respects `data/AUTO_HALT` for operator manual halt.
log "Running auto-calibration executor..."
"$UV" run python scripts/auto_calibration_executor.py 2>&1 | sed 's/^/    /' || \
    log "auto_calibration_executor returned non-zero — proceeding with daemon restart anyway"

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
# Detach via double-fork + setsid-equivalent so the daemon survives this
# script's exit. The May 11 2026 incident: the prior subshell pattern
# `( nohup uv run python ... & )` LEFT THE DAEMON in the parent bash's
# process group on macOS Tahoe. When this script exited at the end of
# step 4, the daemon got SIGTERM and shut down within milliseconds
# (uvicorn logged "Shutting down" at exactly the same second the script
# logged "Daemon healthy. Restart complete.").
#
# Fix: use the python double-fork idiom. The intermediate fork() becomes
# a session leader via os.setsid(), severing the controlling-terminal
# linkage; the grandchild is fully detached and inherits PPID=1 (init)
# the moment the intermediate exits. This is the canonical Unix daemon
# pattern that nohup alone doesn't deliver on macOS launchd-spawned bash.
mkdir -p "$LOGDIR"
LOG="$LOGDIR/main_$(date +%Y%m%d_%H%M%S).log"

# Write a tiny launcher python that double-forks then execs uv. Avoids
# shelling out twice (which would still leave the parent shell in the
# session and could pull the daemon back into its pgrp on TCC reset).
NEW_PID=$("$UV" run python - <<EOF
import os, sys, time
# First fork
pid = os.fork()
if pid > 0:
    # Parent: wait for intermediate's child PID, print it, exit
    time.sleep(0.5)  # let grandchild write its PID
    try:
        with open("$PIDFILE.tmp") as f:
            print(f.read().strip())
    except FileNotFoundError:
        sys.exit(2)
    sys.exit(0)
# Intermediate
os.setsid()  # become session leader, lose controlling tty
pid = os.fork()
if pid > 0:
    # Intermediate: write grandchild PID, exit. Grandchild is now orphaned
    # to init (PPID=1) and survives our bash exiting.
    with open("$PIDFILE.tmp", "w") as f:
        f.write(f"{pid}\n")
    sys.exit(0)
# Grandchild: redirect stdio + raise FD limit + exec uv run python -m src.main
# V7.1 May 12 2026 fix: launchd-spawned bash inherits maxfiles=256 by default
# ('launchctl limit maxfiles' shows 256 soft). The May 12 incident: daemon
# accumulated >256 FDs from chain recorder + heartbeat + per-position
# tracking, then asyncio.socket.accept() and even reading the .env file for
# token refresh failed with OSError [Errno 24] Too many open files. The
# daemon was alive but useless. Raise the soft limit to the system hard
# cap (kern.maxfilesperproc=61440 on this Mac) before exec so the new
# daemon can actually run a full trading day without FD starvation.
import resource
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # Try the system hard limit first; fall back to 16384 if too high
    target = min(hard, 65536)
    if target <= soft:
        target = max(soft, 16384)
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
except Exception:
    pass  # If we can't raise, daemon will still start (with lower limit)
log = open("$LOG", "ab", buffering=0)
os.dup2(log.fileno(), 1)
os.dup2(log.fileno(), 2)
os.dup2(os.open("/dev/null", os.O_RDONLY), 0)
os.execv("$UV", ["$UV", "run", "python", "-m", "src.main"])
EOF
)
rm -f "$PIDFILE.tmp"
if [ -z "$NEW_PID" ]; then
    log "ERROR: failed to capture new daemon PID via double-fork."
    exit 1
fi
echo "$NEW_PID" > "$PIDFILE"
log "Started daemon PID=$NEW_PID, log=$(basename "$LOG")"

# ─── 4. Liveness check ───────────────────────────────────────────────
# 15s for imports + DB connect + strategy on_start (7+4 children).
# The Telegram startup ping gives second-channel confirmation when this
# passes. Sleep is longer than before because BOTH orchestrators need
# to complete warmup before we trust the boot.
sleep 15
if ! kill -0 "$NEW_PID" 2>/dev/null; then
    log "ERROR: daemon died within 15s. Tail of log:"
    tail -60 "$LOG" || true
    exit 1
fi
# Verify PPID is 1 (init/launchd) — if not, the daemon is still in our
# pgrp and will die when we exit. This is the early-detection version
# of the May 11 2026 bug.
DAEMON_PPID=$(ps -o ppid= -p "$NEW_PID" 2>/dev/null | tr -d ' ')
if [ "$DAEMON_PPID" != "1" ]; then
    log "WARNING: daemon PID $NEW_PID has PPID=$DAEMON_PPID (expected 1)."
    log "         Daemon may be killed when this script exits."
fi
log "Daemon healthy (PID=$NEW_PID PPID=$DAEMON_PPID). Restart complete."
