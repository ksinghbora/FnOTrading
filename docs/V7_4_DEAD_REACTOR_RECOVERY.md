# V7.4 — Dead-reactor self-recovery (May 22 2026)

## Symptom

On 6 of 7 trading days (May 11, 12, 13, 18, 20, 22) the daemon's
KiteTicker stopped delivering ticks shortly after `connection lost`
or auto-auth events. The reconnect path ran every few seconds but
never restored ticks. Only fix: kill the process and restart.

May 22 morning: 358 failed reconnect attempts logged, zero ticks for
hours, manual restart required.

## Root cause

`kiteconnect==5.0.1` runs the WebSocket inside a Twisted reactor.
The reactor is a **process-wide singleton** — once it has been
started (or torn down) it cannot be re-started in the same Python
process. Empirically:

- `_on_connect` callback fires **exactly once** per process lifetime
- All subsequent reconnect attempts call `connectWS()` which returns
  silently without producing a new WebSocket
- Source confirmation:
  `.venv/lib/python3.13/site-packages/kiteconnect/ticker.py:528-537`
  branches on `if not reactor.running:` — false on every reconnect
  after the first connect.

So the V7.2 / V7.3 "force-reconnect harder, retune the schedule"
attempts were treating a symptom. Within the process, recovery is
impossible.

## Fix

`TickerManager` now tracks failed reconnects since the last real
`_on_connect` callback. When that counter passes a threshold AND
no tick has arrived for a sustained gap, the heartbeat loop calls
`os.execv(sys.executable, [sys.executable, "-m", "src.main"])` to
replace the process image **in place** — same PID, same PPID,
fresh Python interpreter, fresh Twisted reactor.

Key constants ([src/broker/zerodha/ticker.py](src/broker/zerodha/ticker.py)):

| Constant | Value | Reason |
|---|---|---|
| `DEAD_REACTOR_THRESHOLD` | 3 | Three failed reconnects with no `_on_connect` between is a strong signal the reactor is dead. Two could be transient network. |
| `DEAD_REACTOR_TICK_GAP_SECONDS` | 90 | Must also have gone 90s without a tick. Market microstructure can produce 30-60s tick gaps during quiet periods; 90s rules those out. |

Counter lifecycle:
- starts at 0 in `__init__`
- `_force_reconnect` increments it **up front**
- `_on_connect` resets it to 0 (only fires on real WebSocket success)

## Why `os.execv` and not `launchd KeepAlive`

The daemon is a **grandchild** of launchd — `restart_daemon.sh`
double-forks (orphans the worker to PPID=1) so the launchd job
exits cleanly. launchd cannot track grandchildren, so `KeepAlive`
on the launchd job is a no-op for the worker. `os.execv` keeps the
.pid file valid and avoids all the race conditions of an
"exit-then-respawn-from-outside" pattern.

## Verification

Unit tests pin the contract: [tests/unit/test_ticker_dead_reactor_recovery.py](tests/unit/test_ticker_dead_reactor_recovery.py)

```
.venv/bin/python -m pytest tests/unit/test_ticker_dead_reactor_recovery.py -v
```

10 tests covering:
- counter initial state
- counter increments per `_force_reconnect`
- counter resets on `_on_connect`
- accumulation across multiple reconnects
- reset persists when reconnects happen after success
- threshold constants present and within sane bounds
- execv fires at threshold + gap
- execv does NOT fire below threshold
- execv does NOT fire below gap
- execv args target `python -m src.main`

## Deploy procedure

```bash
# 1. Source of truth — already in repo:
src/broker/zerodha/ticker.py            # V7.4 dead-reactor logic
scripts/com.fno.daemon-restart.plist    # plist (no KeepAlive needed)
scripts/restart_daemon.sh               # launcher

# 2. Sync to active locations:
cp scripts/restart_daemon.sh "$HOME/Library/Application Support/fno-trading/restart-daemon.sh"
cp scripts/com.fno.daemon-restart.plist ~/Library/LaunchAgents/com.fno.daemon-restart.plist

# 3. Reload launchd job:
launchctl unload ~/Library/LaunchAgents/com.fno.daemon-restart.plist
launchctl load   ~/Library/LaunchAgents/com.fno.daemon-restart.plist

# 4. Bounce daemon to load V7.4 ticker code:
bash "$HOME/Library/Application Support/fno-trading/restart-daemon.sh"
```

## Post-deploy verification

```bash
# Confirm V7.4 constants loaded:
.venv/bin/python -c \
  "from src.broker.zerodha.ticker import TickerManager; \
   print(TickerManager.DEAD_REACTOR_THRESHOLD, \
         TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS)"
# expected: 3 90

# Confirm worker is alive:
ps -p "$(cat ~/Library/Application\ Support/fno-trading/fno_main.pid)" -o pid,etime
```

If the dead-reactor state ever recurs, you will see in the daemon log:

```
ERROR ... REACTOR DEAD — replacing process via os.execv()
```

immediately followed by a new daemon line in the next log file
(same PID, new log timestamp).
