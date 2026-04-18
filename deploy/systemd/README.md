# Systemd units for the split recorder + trader processes

Why split? See `docs/DATA_RELIABILITY_PLAN.md` §5. TL;DR: the recorder
must keep capturing market data even when the trader restarts for a
config change, hot-reload, or crash. Splitting them into two systemd
services with no `Requires=` between them gives us that property at the
process level — `systemctl restart fno-trader` does not stop
`fno-recorder`, and a trader OOM-kill does not interrupt CSV writes.

## Files

- `fno-recorder.service` — owns the Kite WebSocket; writes
  `data/chain_snapshots/`, `data/india_vix/`, `data/ticks/`
- `fno-trader.service` — runs strategies, OMS, risk manager, and the
  dashboard API; subscribes to ticks via Redis pub/sub from the recorder

Both depend on Redis being up (the trader's `RedisEventBridge` and the
recorder's `EventBus.publish()` both go through the same broker).

## Prereqs

1. **Redis running** — `systemctl status redis`
2. **Postgres/TimescaleDB running** — only the trader needs it
3. **Repo deployed** at `/opt/fnotrading` (or edit the unit files)
4. **Python venv** at `/opt/fnotrading/.venv` (created by `make setup`)
5. **`.env`** populated and valid:
   - `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN`
   - `RECORDER_SPLIT_MODE=true` ← **required** for the split to take effect
   - DB + Redis URLs reachable from the systemd-managed user
6. **Token freshness** — Kite tokens expire daily; the trader runs an
   8:55 AM auto-auth loop. If you start the recorder before that and
   the token is stale, the recorder will fail fast on first WS connect.
   Run `scripts/auto_auth.py` manually before the first install or
   stage the install for after 9:00 AM.

## Install

```sh
# 1. Edit unit files for your install path / OS user
sed -i 's|/opt/fnotrading|/your/path|g' deploy/systemd/*.service
sed -i 's|fno-trader|your_user|g' deploy/systemd/*.service

# 2. Drop into systemd
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Bring up recorder first (so the Redis tick stream is flowing
#    when the trader subscribes)
sudo systemctl enable --now fno-recorder
sleep 5
sudo systemctl enable --now fno-trader

# 4. Verify both processes
systemctl status fno-recorder fno-trader
cat data/heartbeat/recorder.json | jq '.streams | keys'
cat data/heartbeat/trader.json   | jq '.process'
```

## Common ops

| Goal | Command |
|---|---|
| Restart trader (zero data loss) | `sudo systemctl restart fno-trader` |
| Restart recorder (loses ~5s of ticks) | `sudo systemctl restart fno-recorder` |
| Tail recorder logs live | `journalctl -fu fno-recorder` |
| Stop everything cleanly | `sudo systemctl stop fno-trader fno-recorder` |
| Disable on boot | `sudo systemctl disable fno-recorder fno-trader` |

## Health checks

- `data/heartbeat/recorder.json` — `ts` should refresh every ~30s
- `data/heartbeat/trader.json` — `ts` should refresh every ~30s
- `scripts/verify_system.py` (run via cron at 06:30) checks both

## Why no `Requires=fno-recorder.service` on the trader unit?

A `Requires=` would cause `systemctl restart fno-trader` to also restart
the recorder, which is exactly what the split is meant to prevent.
`After=` is enough for boot ordering; `Requires=` is too tight a coupling
for a runtime dependency.
