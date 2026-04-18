#!/usr/bin/env bash
# Install (or re-sync) the launchd daemon-restart job.
#
# Why this script exists
# ----------------------
# The launchd job that bounces the FnO daemon at 08:50 IST every weekday
# CANNOT live under ~/Documents/. macOS Tahoe (26+) TCC denies launchd-
# spawned bash from reading scripts under the user's protected dirs
# (Documents / Desktop / Downloads), even when /bin/bash has Full Disk
# Access granted in System Settings. SIP-protected system binaries don't
# fully honor FDA grants for protected user folders — confirmed with
# `last exit code = 126` and `bash: ... Operation not permitted` until
# the script was moved out of ~/Documents/.
#
# So the canonical script lives in the repo (auditable, version-
# controlled) and a copy is deployed to ~/Library/Application Support/
# (outside the TCC-protected zone) where launchd can actually exec it.
#
# Run this script:
#   * After cloning the repo for the first time on a new Mac
#   * Whenever scripts/restart_daemon.sh changes (re-syncs the copy)
#   * Whenever scripts/com.fno.daemon-restart.plist changes (re-loads
#     the launchd job)
#
# Idempotent: safe to re-run any time. Will bootout-then-bootstrap an
# existing job rather than failing on "service already loaded".

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_SCRIPT="$REPO_ROOT/scripts/restart_daemon.sh"
SRC_PLIST="$REPO_ROOT/scripts/com.fno.daemon-restart.plist"

BOOT_DIR="$HOME/Library/Application Support/fno-trading"
BOOT_SCRIPT="$BOOT_DIR/restart-daemon.sh"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.fno.daemon-restart.plist"
JOB_LABEL="com.fno.daemon-restart"

log() { echo "[deploy-launchd] $*"; }

# ─── 1. Sync bootstrap script (outside ~/Documents/) ─────────────────
mkdir -p "$BOOT_DIR"
if [ -f "$BOOT_SCRIPT" ] && cmp -s "$SRC_SCRIPT" "$BOOT_SCRIPT"; then
    log "Bootstrap script already in sync: $BOOT_SCRIPT"
else
    cp "$SRC_SCRIPT" "$BOOT_SCRIPT"
    chmod +x "$BOOT_SCRIPT"
    log "Synced bootstrap → $BOOT_SCRIPT"
fi

# ─── 2. Install / refresh the launchd plist ──────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
if [ -f "$LAUNCHD_PLIST" ] && cmp -s "$SRC_PLIST" "$LAUNCHD_PLIST"; then
    log "Plist already in sync: $LAUNCHD_PLIST"
else
    cp "$SRC_PLIST" "$LAUNCHD_PLIST"
    log "Installed plist → $LAUNCHD_PLIST"
fi

# ─── 3. (Re)load the job so launchd reads the latest plist ───────────
# bootout is no-op if the service isn't loaded; we capture and ignore.
launchctl bootout "gui/$UID/$JOB_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$LAUNCHD_PLIST"
log "Job loaded: $JOB_LABEL"

# ─── 4. Show schedule for confirmation ───────────────────────────────
log "Scheduled firings:"
launchctl print "gui/$UID/$JOB_LABEL" 2>/dev/null \
    | awk '/StartCalendarInterval/,/^[[:space:]]*}[[:space:]]*$/' \
    | grep -E "Weekday|Hour|Minute" \
    | paste - - - \
    | sed 's/^/    /'

cat <<EOF

Done. The daemon will be bounced Mon-Fri at 08:50 IST so the
src/main.py:830 holiday gate re-evaluates against today's date.

To smoke-test (will no-op on weekends/holidays, restart on trading days):
    launchctl kickstart -p gui/\$UID/$JOB_LABEL
    tail -5 $REPO_ROOT/logs/restart_daemon.stdout.log

To uninstall:
    launchctl bootout gui/\$UID/$JOB_LABEL
    rm $LAUNCHD_PLIST $BOOT_SCRIPT
EOF
