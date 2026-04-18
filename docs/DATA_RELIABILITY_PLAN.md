# Data Reliability, Logging, Replay & Observability Plan

> **Audience**: Engineers operating the FnO trading system in live mode.
> **Goal**: Treat all production data — market ticks, chain snapshots, orders,
> strategy decisions, logs, configs — as a first-class asset with the same
> rigour as the trading code itself.
> **Status**: Draft v1 (2026-04-17). Phases 1–4 of chain reliability are
> complete; this document covers everything else.

---

## Table of Contents

1. [Why this exists](#1-why-this-exists)
2. [Inventory of data classes](#2-inventory-of-data-classes)
3. [Storage architecture (capture → persist → archive → verify)](#3-storage-architecture)
4. [Format decisions per data class](#4-format-decisions-per-data-class)
5. [Process isolation: separate recorder process](#5-process-isolation-separate-recorder-process)
6. [Logging strategy](#6-logging-strategy)
7. [Replay strategy](#7-replay-strategy)
8. [Observability strategy](#8-observability-strategy)
9. [Accuracy contract (5 invariants)](#9-accuracy-contract)
10. [Implementation roadmap (P0 → P5)](#10-implementation-roadmap)
11. [Cost model](#11-cost-model)
12. [Recommended starting sequence (8 weeks)](#12-recommended-starting-sequence)

---

## 1. Why this exists

Until April 2026 the recorded data was treated as "nice to have":

- Chain CSVs were appended to with no atomicity → torn rows on crash.
- The recorder ran inside the trading process → restart killed capture.
- Logs were printed to stdout, not structured, not retained.
- Configs that changed mid-day were not snapshotted.
- The "23-day replay corpus" had 6 weekend CSVs in it (proven Apr 17 audit).
- VIX was an ATM-IV proxy, biased ~50–70% high on weeklies.

After Phases 1–4 (atomic writes, weekday gate, India-VIX recorder, quarantine,
quality gate) the chain feed is trustworthy. **This document covers everything
else** — logs, replay, observability, process isolation, archival.

The triggering insight: **the data is now the source of truth for backtests,
parameter tuning, and the future ML regime classifier**. A day of lost or torn
data is a day of impossible-to-recover ground truth.

---

## 2. Inventory of data classes

| # | Class | Volume / day | Criticality | Today's home | Target home |
|---|-------|--------------|-------------|--------------|-------------|
| 1 | **Tick stream** (every quote) | ~5–20M rows | High | Discarded after EventBus | Parquet+ZSTD per `(date, instrument)` |
| 2 | **Option chain snapshots** (60s) | ~75K rows | **Critical** | `data/chain_snapshots/*.csv` (atomic) | Same + Parquet archive + DB |
| 3 | **India VIX minute bars** | ~375 rows | High | `data/india_vix_recorded/*.csv` (atomic) | Same + DB |
| 4 | **Spot minute bars** | ~375 rows / underlying | High | Reconstructed from chain | Direct from WS, atomic CSV |
| 5 | **Orders & fills** | ~10–50 rows | **Critical** | TimescaleDB `orders`, `trades` | Same + nightly Parquet export |
| 6 | **Strategy decisions** (one row per signal) | ~100–500 rows | **Critical** | `data/decisions/*.csv` | Same + DB consolidation |
| 7 | **AI advisor I/O** (day_bias, audits) | ~2 files | Medium | `data/day_bias.json`, `latest_audit.json` | Append-only JSONL + DB |
| 8 | **Operational logs** | ~50–500MB | High | stdout (lost) | Structured JSONL local + nightly gzip |
| 9 | **Config snapshots** (params, env, instruments) | ~5 files | **Critical** | Git only | Git + per-day frozen snapshot |
| 10 | **Reference data** (holiday cal, lot sizes, expiry map) | ~10 small files | Medium | Hard-coded + JSON | Versioned JSON, snapshotted daily |

"Critical" = if we lose this for one day, that day's backtest/audit is impossible.

---

## 3. Storage architecture

Four stages — every data class moves through them:

```
┌─────────┐     ┌──────────┐     ┌──────────┐     ┌─────────┐
│ CAPTURE │ →→  │ PERSIST  │ →→  │ ARCHIVE  │ →→  │ VERIFY  │
└─────────┘     └──────────┘     └──────────┘     └─────────┘
  in-process       hot store       cold store       audit jobs
  WS handler       (DB / SSD)      (B2 / S3)        (nightly)
```

### 3.1 Capture (in-process, low-latency)

- WebSocket handler → EventBus → recorder subscriber
- **Atomic writes only** (temp file + `fsync()` + `rename()`). No naked appends.
- Recorders never block the trading hot path — they consume from queue.
- Failures are loud: writer error → metric incremented → Telegram on threshold.

### 3.2 Persist (hot store, queryable)

- **TimescaleDB hypertables** for: ticks (downsampled), chain snapshots, orders,
  trades, decisions, advisor I/O.
- Retention: 90 days hot, then drop. Long-term lives in archive.
- Indexes tuned for common queries: `(symbol, ts)`, `(strategy, ts)`,
  `(underlying, expiry, strike, ts)`.

### 3.3 Archive (cold store, immutable)

- **Parquet+ZSTD on local SSD** for last 30 days (fast replay).
- **Backblaze B2** for everything older (cheap: ~$5/TB/mo, S3-compatible API).
- **One file per `(date, instrument_class)`**. Never overwrite — new day = new file.
- **Manifest** (`archive/manifest.jsonl`) records SHA256 + row count + schema
  version of every archived file. The B2 uploader writes a **separate** ledger
  (`archive/manifest_b2.jsonl`) so the upload step can never accidentally mutate
  the archive ledger — discipline locked in by `tests/unit/test_upload_to_b2.py::test_batch_does_not_mutate_archive_manifest`.

**Implementation chain (done Apr 18)**:
1. `scripts/archive_to_parquet.py` — converts eligible chain CSVs (default ≥7 days
   old) to Parquet+ZSTD level 3. Smoke result on a real day: **9.9MB → 2.0MB
   (4.90x ratio), 75,754 rows, lossless round-trip**. Idempotent: re-running on a
   day already in the manifest with matching SHA is a no-op; SHA drift WARNs and
   re-archives.
2. `scripts/upload_to_b2.py` — pushes archived Parquets to remote storage via
   the `RemoteUploader` Protocol. Two backends ship: `B2Uploader` (lazy `b2sdk`
   import, native sha1_sum verification matching B2's `x-bz-content-sha1` header)
   and `LocalDirUploader` (mimics the B2 keyspace under a local dir, used by
   tests + the `cold-storage` Make target so dev/CI exercises the full chain
   without B2 creds). Backend is selected by `ARCHIVE_REMOTE` env var
   (`b2` default, `local-dir:<path>` for dev). **Fails loud** when `b2` is
   selected but creds are missing — a cron that "succeeds" by uploading nothing
   for six weeks is exactly the failure mode this whole chain exists to prevent.
3. `scripts/restore_test.py` — weekly DR proof. Downloads the most recently
   uploaded archive (or a specific `--target-date`), verifies SHA1 round-trip,
   decodes the Parquet, and reconciles row count against the original source CSV.
   Returns six discrete status verdicts (`ok`, `sha1_mismatch`, `missing_remote`,
   `decode_failed`, `row_count_mismatch`, `source_rotated_ok` — the last is
   informational, not failure) and **exits 2** on any real failure so cron
   monitoring fires.

Cron schedule (see `deploy/cron/README.md`):

| Time | Cron | Job |
|------|------|-----|
| 22:00 IST mon–fri | `make archive` | CSV → Parquet+ZSTD on local SSD |
| 23:00 IST mon–fri | `make upload`  | Push to B2 (or `ARCHIVE_REMOTE=local-dir:./fake-b2` in dev) |
| 09:00 IST sun     | `make restore-test` | Weekly DR proof; exits 2 on failure |

Local-dir end-to-end smoke (no B2 creds needed):
```bash
make cold-storage
# → archive → upload (local-dir:./fake-b2) → restore-test (local-dir:./fake-b2)
```

### 3.4 Verify (audit jobs, trust but verify)

Nightly cron at 21:00 IST:
- Row-count plausibility (≥ 80% of expected for the day)
- Schema check (column types, no NaN floods)
- Cross-tier reconciliation (CSV row count == DB row count == Parquet row count)
- SHA256 verify of last 7 days of archive
- Telegram digest with PASS/WARN/FAIL per data class

Weekly cron Sunday 09:00 IST (`scripts/restore_test.py`, see §3.3):
- Pick the most recently uploaded archive (or `--target-date`), download from B2,
  SHA1-verify, decode the Parquet, reconcile row count against the source CSV.
  Exit code 2 on failure (cron-friendly).

---

## 4. Format decisions per data class

| Class | Format | Why |
|-------|--------|-----|
| Tick stream | **Parquet+ZSTD** | 100x smaller than CSV, fast columnar reads for replay |
| Chain snapshots (live) | **CSV (atomic)** | Human-readable for ops, easy ad-hoc inspection. Auto-converted to Parquet at midnight. |
| Chain snapshots (archive) | **Parquet+ZSTD** | 5–10x smaller than CSV, faster replay loads |
| VIX minute bars | **CSV (atomic)** | Tiny, human-readable wins over compression |
| Spot minute bars | **CSV (atomic)** | Same |
| Orders / fills | **TimescaleDB + nightly Parquet** | DB for joins, Parquet for cold archive |
| Decisions | **CSV (atomic) + DB mirror** | CSV for grep-ability, DB for joins with outcome |
| Advisor I/O | **JSONL append-only** | Free-form payloads, append-friendly |
| Logs | **JSONL append-only with rotation** | Structured, greppable, machine-parseable |
| Config snapshots | **YAML frozen + git tag** | Reviewable diffs |

**No format that requires file rewrites** for live writers. Always atomic-append or atomic-rotate.

---

## 5. Process isolation: separate recorder process

### 5.1 Decision

**Yes — split into two processes**, sharing only the EventBus topology and the
data layer.

```
┌─────────────────────────────┐    ┌─────────────────────────────┐
│  src/recorder_main.py       │    │  src/main.py (trading)      │
│  - Kite WS (read-only mode) │    │  - Kite WS (orders+ticks)   │
│  - ChainSnapshotRecorder    │    │  - Strategies               │
│  - IndiaVixRecorder         │    │  - OMS / Risk / KillSwitch  │
│  - TickRecorder             │    │  - AI Advisor               │
│  - SpotRecorder             │    │  - Optional sub to recorder │
│  - LogShipper               │    │    via Redis pub/sub        │
│  - Atomic writes only       │    │                             │
│  - own systemd unit         │    │  - own systemd unit         │
└─────────────────────────────┘    └─────────────────────────────┘
            │                                     │
            └────── shared Redis pub/sub ─────────┘
            └────── shared TimescaleDB ───────────┘
```

### 5.2 Why

| Failure mode today | Effect | After split |
|-------------------|--------|-------------|
| Strategy hot-fix restart | Lose 30–60s ticks + partial chain row | **No data impact** |
| OMS bug → SIGKILL | Data capture dies until ops notices | **Recorder keeps running** |
| Risk-manager kill switch | Same collateral | **Recorder keeps running** |
| `uv sync` deploy | All capture stops during reinstall | **Recorder unaffected** |
| Backtest run on prod box | Memory pressure stalls recorder | **Resource-isolated** |

### 5.3 Trade-offs (honest accounting)

- **Two Kite WS subscriptions.** Kite allows 3,000 instruments per session. We use ~50–100. Bandwidth/latency cost negligible.
- **Two access tokens** OR shared read-only token. `auto_auth.py` already automated.
- **Two systemd units** instead of one. Worth it.
- **Eventual consistency.** If trading process wants to read live chain, it now subscribes to recorder's Redis pub/sub (~1ms hop). Acceptable.

### 5.4 Migration path

**Status: implemented (Apr 17 2026).** Activated by setting
`recorder_split_mode=true` in Settings. Default is `false` so existing
single-process deployments are unchanged.

| Piece | Where | Notes |
|---|---|---|
| Recorder process | `src/recorder_main.py` | Owns Kite WS; runs ChainSnapshotRecorder + IndiaVixRecorder + TickRecorder + Heartbeat |
| Trader process | `src/main.py` | Skips ticker creation when `recorder_split_mode=true`; uses `RedisEventBridge` to receive ticks |
| IPC | Redis pub/sub channel `events:tick` (also `events:connection_lost`/`_restored`) | EventBus.publish() mirrors locally + to Redis; bridge calls publish_local() to avoid loops |
| Heartbeats | `data/heartbeat/recorder.json` + `data/heartbeat/trader.json` | Two files, two `process` names — watchdog distinguishes which is down |
| Make targets | `make recorder`, `make trader` | `make run` still does the legacy single-process mode |
| Systemd units | `deploy/systemd/fno-recorder.service`, `deploy/systemd/fno-trader.service` | No `Requires=` between them — that's the entire point |
| Watchdog | `scripts/verify_system.py` §10a | Reads both heartbeat files, warns per-process |
| Nightly audit | `scripts/nightly_audit.py` §3c-3d | Reports recorder stream counts + flags missing trader heartbeat |

**Activation checklist for production:**
1. Confirm Redis is up and reachable from both processes.
2. Set `RECORDER_SPLIT_MODE=true` in `.env`.
3. Bring up recorder first (`systemctl start fno-recorder`), wait ~5s.
4. Bring up trader (`systemctl start fno-trader`).
5. Verify both heartbeats refreshing every 30s.
6. Test the property: `systemctl restart fno-trader` should NOT cause a
   gap in `data/chain_snapshots/<today>.csv` row count.

**What this still doesn't solve** (deferred to P2):
- Recorder restart during market hours still loses ~5s of ticks (WS reconnect time).
- Trader missed-tick recovery is not implemented — strategies just resume from next tick. Acceptable because most strategies use indicators that absorb single-tick gaps.

### 5.5 Future evolution

- Move recorder to a separate ₹500/mo VM → physical failure isolation.
- Recorder writes directly to TimescaleDB (no CSV middleman) — see roadmap P1.

---

## 6. Logging strategy

### 6.1 Today (the problem)

- Logs go to stdout.
- Format is human-readable strings (`f"..."`).
- Lost on process restart unless captured by systemd journal (which rotates aggressively).
- No structured fields → can't query "all DEGRADED chain snapshots in March".
- Tags exist (`[ENTRY_QUALITY]`, `[FILTER]`, `[ATTRIBUTION]`, `[MONITOR]`, `[DAY_SUMMARY]`) but only as substrings — easy to typo.

### 6.2 Target

**Structured JSONL with retention, queryable locally with `jq`.**

```json
{"ts":"2026-04-17T09:31:14.221+05:30","level":"INFO","logger":"strategy.portfolio",
 "tag":"ENTRY_QUALITY","strategy":"portfolio","underlying":"NIFTY",
 "signal_score":72,"regime":"strangle","vix":13.8,"pcr":0.93,
 "msg":"entry approved"}
```

### 6.3 Components

1. **JSON formatter** in `src/utils/logging_setup.py`
   - One log line = one JSON object on one line
   - Mandatory fields: `ts`, `level`, `logger`, `msg`, `pid`, `process_name`
   - Optional structured fields via `extra={...}` in `logger.info(...)`
   - Tag enum in `src/utils/log_tags.py` (no string literals)

2. **Rotating file handler**
   - Per-day file: `logs/<process>_<YYYY-MM-DD>.jsonl`
   - Rotate at midnight IST, keep 90 days local
   - Compress rotated files (`.jsonl.zst`)

3. **Tag taxonomy** (typed, exhaustive)
   - `ENTRY_QUALITY` — every signal evaluation, including rejections with reason
   - `FILTER` — PCR/max-pain/IV-skew/trend filter pass or block
   - `ATTRIBUTION` — per-leg P&L breakdown at exit
   - `MONITOR` — health checks, recorder heartbeats
   - `DAY_SUMMARY` — end-of-day P&L, win rate, regime distribution
   - `RISK_BLOCK` — risk-manager rejections (NEW — currently silent)
   - `KILL_SWITCH` — kill-switch trips (NEW)
   - `RECONNECT` — Kite WS reconnect events (NEW)
   - `ORDER_LIFECYCLE` — placed → ack → fill → exit (NEW)
   - `DATA_QUALITY` — recorder DEGRADED/PARTIAL/CLEAN (NEW)

4. **Log shipper — deferred indefinitely**
   - Loki/Promtail/Vector add operational surface area for a single-host
     system. `jq` + `grep` against local JSONL files cover the same
     queries with zero new processes to maintain. See §8.1 for the full
     argument. Revisit only when running on multiple hosts.

5. **Sensitive-field redaction**
   - Auto-redact `kite_password`, `kite_api_secret`, `kite_totp_secret`, `telegram_bot_token` from any log line via formatter filter
   - Test in `tests/unit/test_log_redaction.py`

### 6.4 Audit & alerting on logs

Nightly cron parses today's JSONL and Telegrams a digest:
- Total errors / warnings
- Top 5 error messages by count
- Any `KILL_SWITCH` or `RISK_BLOCK` events (should be rare)
- Any `DATA_QUALITY=DEGRADED` events
- Recorder reconnects > 3 → alert

### 6.5 Retention

- Local JSONL: 90 days
- Compressed (`.zst`) archive on B2: 2 years
- TimescaleDB `events` table for the 5 critical tags only (`KILL_SWITCH`, `RISK_BLOCK`, `ORDER_LIFECYCLE`, `DATA_QUALITY`, `DAY_SUMMARY`) — queryable forever

---

## 7. Replay strategy

### 7.1 Today

- `ReplayBacktestEngine` reads chain CSVs, walks through 60s snapshots, asks the strategy "what would you do?"
- Quality gate (Phase 4): skips DEGRADED days, warns on PARTIAL.
- Results: 13 clean / 3 partial / 1 degraded out of 17 days post-quarantine.

### 7.2 Gaps

1. **Tick-level replay not supported.** We only replay 60s chain snapshots, so intra-snapshot decisions (e.g. trail-stop intra-minute) can't be backtested with real data.
2. **No deterministic mode.** Two replays of same day can differ if order seeds change — should be byte-equal.
3. **Replay doesn't load configs from the day-of.** It uses `params.py` *as of today*, not as of the trading day → backtest ≠ live counterfactual.
4. **No outcome attribution table.** We can't answer "which feature most predicted the win?"
5. **No replay of orders** (we only replay strategy *decisions*, not the OMS execution).

### 7.3 Target architecture

```
DayReplay(date) →
  load chain Parquet (or CSV) →
  load ticks Parquet (optional, for tick-level mode) →
  load India VIX recorded →
  load configs frozen for that day →
  load instrument map for that day →
  run strategy(s) →
  emit decisions to in-memory list →
  optionally simulate OMS fills against recorded order book →
  produce DayReplayResult{ decisions, fills, pnl, attribution }
```

Two modes:
- **Snapshot mode** (current): 60s chain snapshots, fast (~1s per day)
- **Tick mode** (new): every WS tick from Parquet, slower (~30s per day), accurate stops

### 7.4 Determinism contract

- Every replay run with same `(date, code_sha, params_sha, seed)` produces byte-identical output.
- Output hash recorded in `replay_runs` table → catches silent regressions.
- CI runs replay on a golden day on every PR; fails if hash changes unexpectedly.

### 7.5 Counterfactual replay

The most important new capability. After every live trading day:

```
nightly_audit.py:
  for strategy in [live, shadow_v_next]:
    result = DayReplay(today).run(strategy)
  diff = compare(live_actual, replay_with_live_params)
  if diff > tolerance:
    alert("backtest-live divergence")
```

This catches:
- Bugs where live behaves differently than backtest (param desync, race conditions, missing filter checks).
- Slippage/fill model errors (replay assumes mid, live got worse).
- Regime detection drift (live VIX read ≠ replayed VIX).

### 7.6 Replay-as-CI

- Every PR runs `make replay-ci` → replays last 5 clean days on the golden config → fails if total P&L changes by > ₹500 unexpectedly.
- This is our defense against "subtle refactor breaks strategy" bugs.

### 7.7 Implementation pieces

| # | Piece | Status |
|---|---|---|
| 1 | `src/backtest/day_replay.py` — single-day replay with deterministic mode | **done Apr 18** |
| 2 | `scripts/replay_day.py` — CLI wrapper | **done Apr 18** |
| 3 | `scripts/replay_counterfactual.py` — runs nightly inside `nightly_audit.py` | **done Apr 18** |
| 4 | `tests/integration/test_replay_determinism.py` — golden-hash test on 2026-04-15 | **done Apr 18** (5 tests, real engine, ~12s) |
| 5 | `replay_runs` table (vanilla PG; promote to TimescaleDB hypertable in single follow-up migration when extension lands) + `data/replay_runs.jsonl` durable mirror | **done Apr 18** (both forms) |
| 6 | Make target `make replay-golden` (default `GOLDEN_DAY=2026-04-15`, override via env) | **done Apr 18** |

The harness writes one summary line to `data/replay_runs.jsonl` per call:
``{run_at, date, strategy, underlying, seed, code_sha, params_sha,
params_source, total_pnl, num_days, snapshot_coverage_pct, replay_hash,
error}``. This is the audit trail referenced by §7.4. The same fields are
mirrored to the `replay_runs` SQL table when `replay_day` is called with a
`db_session_factory` (see §7.7); JSONL stays as the durable, dependency-free
record so DB outages cannot eat audit data. Promotion to a TimescaleDB
hypertable is a single follow-up migration: `SELECT create_hypertable(
'replay_runs', 'run_at', if_not_exists => TRUE);` — no model changes needed.

`scripts/snapshot_config.py` now emits `params.json` alongside `params.yaml`
so the harness can rehydrate frozen-day-of params without depending on a
YAML parser. The `params_source` field on every result records whether the
snapshot was used (`snapshot:<path>`), the live defaults were used (`live`),
or the strategy isn't in the snapshot mapping yet (`live (no mapping)`).

**Snapshot-fallback alert (Apr 18)**: when `params_source` doesn't start with
`snapshot:`, the counterfactual ran against today's live params, not frozen-
day-of, so a "no divergence" reply is potentially a false negative. The
counterfactual now treats this as a third alert flavour: a standalone warning
when nothing else fires, prepended as a banner when the divergence-breach or
replay-error alert also fires. The `params_source_is_fallback: bool` field
on `CounterfactualResult` makes the same signal queryable in the JSONL ledger.

### 7.8 Frozen-day-of configs

For replay to be honest, we need the **exact** state of:
- `params.py` snapshot
- Active strategies list
- Filter thresholds
- Holiday calendar
- Lot sizes
- Active expiry instrument tokens

**Solution**: every day at 09:00 IST, `scripts/snapshot_config.py` writes
`config/snapshots/<date>/{params.yaml, env.yaml, instruments.json, holidays.json}`.
Replay loads from the snapshot, not from current source.

---

## 8. Observability strategy

### 8.1 Why no Prometheus/Grafana (revised Apr 17 2026)

The original draft of this section called for Prometheus + Grafana + Loki +
OpenTelemetry — the standard "three pillars" stack. After implementation
review we deliberately **dropped that plan** for this system. The reasoning
is honest cost/benefit, not laziness:

| Argument for Prometheus/Grafana | Why it doesn't apply here |
|---|---|
| Time-series storage for metrics | TimescaleDB already stores ticks, candles, orders, P&L, advisor decisions. Adding Prometheus duplicates the time-series layer. |
| PromQL for ad-hoc queries | SQL on TimescaleDB answers the same questions, against richer data, with no second store to maintain. |
| Live dashboards for ops | Single-user system. The FastAPI dashboard at `/api/dashboard/summary` already serves live state. Markets are open 6.25 hrs/day; nobody is staring at Grafana at 11:23 AM. |
| Threshold + rate-of-change alerts | Already built as cron + SQL + Telegram (see `alert_chain_dropoff.py`). The pattern scales: one focused script per failure mode. |
| Grafana for visual debugging | Add HTML pages to the existing FastAPI dashboard when you want a chart. Same data, no new infra. |

**Real cost we'd pay**: two more long-running processes (Prometheus scraper +
Grafana), retention policy decisions, dashboard JSON to maintain, another auth
surface on the host. None of that produces a single trade decision.

**Revisit trigger**: stand up Prometheus only when ONE of these is true:
- We're running the trader on more than one host
- A second person needs visibility without grepping logs
- We hit a performance debugging question that grep + SQL genuinely can't answer

Until then, the observability stack is three things: structured logs,
heartbeats, and the FastAPI dashboard.

### 8.2 Pillar 1 — Structured JSON logs (the high-value piece)

**Today's problem.** Logs use ad-hoc `[TAG]` prefixes — `[ENTRY_QUALITY] score=72 vix=14.2 reason=score_below_threshold`. Greppable, but every grep is a fresh regex against a slightly different format. "Show me every blocked entry today grouped by reason" is a 10-minute awk session.

**Target.** One JSONL stream per process: `logs/trader.jsonl`, `logs/recorder.jsonl`. Each line is a self-describing event with stable field names. Same data, but `jq` becomes a real query language:

```sh
# All blocked entries today, grouped by reason
jq -r 'select(.tag=="ENTRY_QUALITY" and .entered==false) | .reason' logs/trader.jsonl | sort | uniq -c

# P&L curve points with VIX context
jq 'select(.tag=="SUMMARY") | {ts, day_pnl, vix}' logs/trader.jsonl

# Every Kite WS reconnect with surrounding context
jq 'select(.tag=="WS_RECONNECT")' logs/recorder.jsonl
```

**Required fields on every record**:
```json
{
  "ts": "2026-04-17T11:43:01+05:30",
  "level": "INFO",
  "process": "trader",
  "module": "src.strategy.runner",
  "tag": "ENTRY_QUALITY",
  "msg": "Signal blocked",
  "trace_id": "ent-9f3c1a"
}
```
Plus arbitrary tag-specific fields (`score`, `vix`, `strategy_id`, `reason`, etc.).

**Backward compat**: keep the existing `[TAG]` text format as a separate human-readable stream (stderr) so live `tail -f` is still readable. The JSONL goes to a file.

**Tags we already emit, formalised**: `[ENTRY_QUALITY]`, `[FILTER]`, `[ATTRIBUTION]`, `[MONITOR]`, `[DAY_SUMMARY]`, `[SUMMARY]`, `[ADVISOR]`, `[TICKER]`. Add `[WS_RECONNECT]`, `[ATOMIC_WRITE_FAIL]`, `[ORDER_LATENCY]` as we identify gaps.

**Retention**: 30 days hot in `logs/`, then gzip to `data/archive/logs/` for 1 year, then drop. Daily `find logs -name '*.jsonl' -mtime +30 -exec gzip {} +` cron.

### 8.3 Pillar 2 — Heartbeats (already built, P0 done)

Each long-running process writes a JSON snapshot every 30s to
`data/heartbeat/<process>.json`. See §8.6 below for schema. Watchdogs
(`scripts/verify_system.py`, `scripts/nightly_audit.py`,
`scripts/alert_chain_dropoff.py`) read these files and Telegram on
staleness. **This is the system's "is it alive?" signal.**

### 8.4 Pillar 3 — FastAPI live dashboard (extend rather than replace)

`/api/dashboard/summary` already exists and serves: open positions, day
P&L, strategy states, circuit breaker state, pending orders, queue depth,
tick rate. Add panels rather than spinning up Grafana:

- **`/api/dashboard/recorder`** — recorder heartbeat freshness, per-stream
  counts, last write age. (Reads `data/heartbeat/recorder.json`.)
- **`/api/dashboard/quality`** — chain priceable %, IV %, dropoff vs
  baseline. (Reads today's chain CSV + recent baseline.)
- **`/api/dashboard/risk`** — risk-manager blocks today, kill-switch
  state, day P&L vs limit. (Already partially in `/summary`.)

These are 1-2 hour endpoints each, on demand, when a specific question
keeps coming up.

### 8.5 Alert pattern (cron + SQL + Telegram)

The template is `scripts/alert_chain_dropoff.py`: focused script, runs on
a cron, queries the relevant data source (heartbeat JSON, chain CSV, or
TimescaleDB), Telegrams if a threshold is breached, dedups via state
file in `data/heartbeat/`. **Add a new script when a new failure mode
becomes worth detecting**, not preemptively.

**Already in production:**
- `scripts/alert_chain_dropoff.py` — hourly during market, chain row count vs 7-day baseline
- `scripts/verify_system.py` — pre-market 06:30, full system check
- `scripts/nightly_audit.py` — 21:00, day summary + advisor scorecard + heartbeat audit

**Likely additions when needed (not built preemptively):**
- `alert_order_latency.py` — if signal-to-fill p95 starts creeping above 500ms
- `alert_slippage.py` — if avg slippage vs LTP exceeds the strategy's assumed cost
- `alert_disk_space.py` — if `data/` partition crosses 80%
- `alert_pnl_drift.py` — if today's live P&L diverges > ₹500 from same-strategy backtest

Each is ≈ 1 hour of work. **Don't pre-build alerts that don't have a real failure history.** The list above is a watchlist, not a TODO.

### 8.6 Heartbeats (schema reference)

```json
{
  "process": "recorder",
  "pid": 12345,
  "ts": "2026-04-17T11:43:01+05:30",
  "started_at": "2026-04-17T09:14:58+05:30",
  "date": "2026-04-17",
  "streams": {
    "chain_recorder": {
      "snapshots_today": 145,
      "rows_in_last_snapshot": 240,
      "priceable_pct_last": 96.2,
      "iv_pct_last": 91.4,
      "last_write": "2026-04-17T11:42:58+05:30",
      "last_update_ts": "2026-04-17T11:43:01+05:30"
    },
    "vix_recorder": {
      "minutes_today": 148,
      "last_value": 13.82,
      "last_write": "2026-04-17T11:42:00+05:30",
      "last_update_ts": "2026-04-17T11:43:01+05:30"
    },
    "tick_recorder": {
      "ticks_today": 2134119,
      "instruments_active": 42,
      "last_flush": "2026-04-17T11:42:30+05:30",
      "last_update_ts": "2026-04-17T11:43:01+05:30"
    }
  },
  "kite_ws_state": "connected"
}
```

In split mode the trader writes a parallel `data/heartbeat/trader.json`
with strategy/risk/OMS state instead of recorder streams.

### 8.7 SLOs (still worth writing down even without Prometheus)

The numbers are the same — what changes is how we measure them.

| SLO | Target | Measured by |
|---|---|---|
| Chain capture availability during market hours | ≥ 99.5% | Daily SQL on chain CSV row counts vs expected (~390 minutes × ~80 strikes) |
| Chain priceable % per snapshot (intraday avg) | ≥ 90% | Nightly audit reads CSV, computes %, alerts if below |
| Order placement latency (signal → broker ack) | p95 < 500ms | `[ORDER_LATENCY]` JSONL events; daily SQL on the file |
| Replay determinism | 100% byte-equal across runs on same `(code_sha, params_sha)` | CI check (P3) |
| Backtest-live P&L divergence | \|Δ\| < ₹500 / day on > 95% of days | Nightly comparison job |

If any SLO breaks, write the alert script for it then. Don't build the
dashboard before the failure.

---

## 9. Accuracy contract

Five invariants that every data writer must obey. **Reviewable in code review.**

1. **Atomic** — all writes use temp file + `fsync()` + `rename()`. No naked appends.
2. **Gated** — no writes during weekends, holidays, or pre-open. Weekday + holiday check before any I/O.
3. **Schema-enforced** — every row validated against an explicit schema (pydantic or pyarrow). Rejects fail loudly to a `_quarantine/` dir.
4. **Quality-tagged** — every file gets a `data_quality` field: `clean | partial | degraded | weekend`. Audit job classifies; replay engine respects.
5. **Reproducible** — every backtest result includes `(code_sha, params_sha, data_sha, seed)` so it can be re-run byte-exact.

---

## 10. Implementation roadmap

### P0 — Do this week (highest leverage, all small)

| Item | Effort | Value |
|------|--------|-------|
| Cron the nightly audit (`scripts/audit_chain_quality.py`) at 21:00 IST | 1h | Catches silent data corruption |
| Daily config snapshotter (`scripts/snapshot_config.py`) | 2h | Replay correctness |
| Daily instrument-map snapshotter | 1h | Replay won't break across expiry rolls |
| Telegram alert when chain CSV row count drops > 30% vs 7-day avg | 1h | Detect WS death |
| `data/heartbeat/recorder.json` heartbeat (already designed §8.5) | 1h | Watchdog hook |

### P1 — Within 2 weeks

| Item | Effort | Value | Status |
|------|--------|-------|--------|
| Split recorder into separate process (§5.4) | 1d | Decouples data uptime from trading uptime | **done Apr 17** |
| Structured JSON logging (§6.2 / §8.2) | 1d | Queryable history (`jq` against JSONL) | next |
| ~~Prometheus `/metrics` endpoint~~ | — | dropped — see §8.1 | n/a |
| ~~Grafana dashboards~~ | — | dropped — see §8.1 | n/a |
| Extend FastAPI dashboard (`/api/dashboard/{recorder,quality,risk}`) | 0.5d | Same need Grafana would address, less infra | when needed |

### P2 — Within 1 month

| Item | Effort | Value | Status |
|------|--------|-------|--------|
| TimescaleDB hypertables for ticks/chain/orders/decisions | 2d | Joinable, queryable, retention policies | partial — `replay_runs` shipped Apr 18 as vanilla PG; promotion to hypertable is a one-line follow-up migration once extension is enabled. ticks/chain/orders/decisions still pending. |
| Nightly Parquet archive job (CSV → Parquet+ZSTD) | 1d | 5–10x storage saved | **done Apr 18** (`scripts/archive_to_parquet.py`; smoke 4.90x ratio on real chain) |
| Backblaze B2 cold archive + manifest | 1d | DR + cheap long-term storage | **done Apr 18** (`scripts/upload_to_b2.py`; `LocalDirUploader` for dev/CI, `B2Uploader` lazy-imports `b2sdk`; separate `manifest_b2.jsonl` ledger) |
| Day-of replay (`day_replay.py`) | 1d | Counterfactual analysis | **done Apr 18** |
| Determinism hash test (unit) | 0.5d | Catch silent strategy regressions | **done Apr 18** (`make replay-ci` ready for post-merge wiring) |

### P3 — Within 2 months

| Item | Effort | Value | Status |
|------|--------|-------|--------|
| Tick-level replay mode | 2d | Accurate intra-minute stop testing | pending |
| Counterfactual replay in nightly audit | 1d | Catches backtest-live divergence | **done Apr 18** (3 alert flavours: divergence / replay-error / snapshot-fallback) |
| ~~Loki log shipping~~ | — | dropped — see §8.1; `jq` on local JSONL is enough | n/a |
| Restore-test cron (weekly B2 round-trip) | 0.5d | Proves DR works | **done Apr 18** (`scripts/restore_test.py`; 6 status verdicts incl. `source_rotated_ok`; exits 2 on failure for cron monitoring; verified end-to-end via `make cold-storage` against `LocalDirUploader`) |
| `events` table in DB for critical log tags | 0.5d → 1.5d realistic | Long-term auditability | **deferred Apr 18** — high-volume firehose; needs JSONL-tail+offset-cursor shipping (different shape from `replay_runs` synchronous insert) and TimescaleDB compression to avoid a storage cliff. Revisit after extension is enabled. The consolidated decision-snapshot table (P4) is the better next data-layer target — narrower schema, ML-aligned. |

### P4 — Within 3 months

| Item | Effort | Value |
|------|--------|-------|
| ~~OpenTelemetry traces~~ | — | dropped — JSONL events with shared `trace_id` cover the same need | n/a |
| Decision-snapshot table (one row = features + outcome) | 2d | Pre-ML feature store |
| Replay-as-CI on every PR | 1d | Block regressions before merge |
| Per-SLO alert script (one cron per SLO that breaches) | 0.5d each | Know when we're off-target |

### P5 — Optional / advanced

- Move recorder to dedicated VM (₹500/mo)
- BigQuery / DuckDB analytics layer over Parquet for ad-hoc analysis
- ML feature store from decision snapshots (after 120 days of clean data)
- Automated A/B test framework using counterfactual replay

---

## 11. Cost model

Annualized estimate, assuming current scale (~75K chain rows/day, ~10M ticks/day, NIFTY+BANKNIFTY).

| Item | Sizing | Cost / year |
|------|--------|-------------|
| Local SSD (last 30d hot Parquet) | ~30GB | already provisioned |
| Backblaze B2 (cold archive, 2y retention) | ~150GB | $9 (~₹750) |
| Backblaze egress (audit/restore tests) | ~5GB/mo | $5.40 (~₹450) |
| TimescaleDB on existing host | ~50GB | already provisioned |
| ~~Prometheus + Grafana + Loki~~ (dropped — see §8.1) | — | — |
| Optional dedicated recorder VM (₹500/mo) | — | ₹6,000 |
| Telegram alerts | — | free |
| Engineering time (P0–P2 ~7 days) | one-off | sunk cost |

**Year-1 marginal cost**: ~₹7,200 (or ~₹1,200 if we skip the dedicated VM).
**Year-2 onwards**: ~₹1,500 / year storage.

vs. **the cost of one corrupted backtest leading to a bad parameter choice**:
likely > ₹50,000 in live losses on a 1-lot run. The infra is essentially free.

---

## 12. Recommended starting sequence

8-week plan, biased to highest-leverage-per-hour first.

### Week 1 — Stop the bleeding (P0, ~6 hours total)
- Cron the nightly audit
- Add row-count alert
- Config + instrument snapshotter
- Recorder heartbeat file

### Week 2 — Process isolation (P1)
- Carve out `src/recorder_main.py`
- Two systemd units
- Verify both processes survive trader restart

### Week 3 — Structured logs (P1)
- JSON logger + tag enum
- Migrate existing `[TAG]` logs to typed extras
- Add `/api/dashboard/recorder` panel reading from heartbeat
- (no Prometheus/Grafana — see §8.1 for why)

### Week 4 — Hot store (P2 start)
- TimescaleDB hypertables for chain + orders + decisions
- Backfill from existing CSVs

### Week 5 — Cold archive (P2)
- Nightly Parquet conversion
- B2 upload + manifest
- First weekly restore test

### Week 6 — Replay v2 (P2)
- `day_replay.py` with frozen configs
- Determinism hash test
- Make `replay-golden` target

### Week 7 — Counterfactual + alerts (P3 start)
- Counterfactual replay in nightly audit
- Backtest-live divergence script (`scripts/alert_pnl_drift.py` pattern)
- Critical alerts → Telegram (cron + SQL, same pattern as `alert_chain_dropoff.py`)

### Week 8 — Polish
- Restore-test cron
- Per-SLO alert scripts as breaches surface
- Document runbooks for each alert

---

## Appendix A: Files this plan creates or modifies

**New files**:
- `src/recorder_main.py` — **done Apr 17**
- `src/utils/logging.py`, `src/utils/log_tags.py` — **done Apr 17** (structured JSON logs)
- `src/observability/heartbeat.py` — **done Apr 17** (no `metrics.py` — see §8.1)
- `src/backtest/day_replay.py` — **done Apr 18**
- `scripts/snapshot_config.py` — **done Apr 17** (Apr 18: also emits `params.json` sidecar)
- `scripts/snapshot_instruments.py` — pending
- `scripts/archive_to_parquet.py` — **done Apr 18** (CSV → Parquet+ZSTD level 3; 4.90x smoke ratio; idempotent on SHA match, WARN on drift)
- `scripts/upload_to_b2.py` — **done Apr 18** (`RemoteUploader` Protocol; `LocalDirUploader` for dev/CI, `B2Uploader` lazy-imports `b2sdk`; selected by `ARCHIVE_REMOTE` env; fail-loud on missing B2 creds; writes `archive/manifest_b2.jsonl`)
- `scripts/restore_test.py` — **done Apr 18** (6 status verdicts; sources picked by most-recent-upload or `--target-date`; exits 2 on failure for cron monitoring)
- `tests/unit/test_archive_to_parquet.py` — **done Apr 18** (16 tests; round-trip, idempotence, SHA drift WARN, batch filters, dry-run)
- `tests/unit/test_upload_to_b2.py` — **done Apr 18** (16 tests; manifest separation locked in; fail-loud on missing creds; remote-key derivation)
- `tests/unit/test_restore_test.py` — **done Apr 18** (15 tests; one per status verdict; uses real `LocalDirUploader` end-to-end)
- `scripts/replay_day.py` — **done Apr 18**
- `scripts/replay_counterfactual.py` — **done Apr 18** (22 unit tests incl. snapshot-fallback flavour; wired into `nightly_audit.py` §3e)
- `scripts/alert_chain_dropoff.py` — **done Apr 17**
- `deploy/systemd/fno-recorder.service`, `deploy/systemd/fno-trader.service` — **done Apr 17**
- `tests/integration/test_replay_determinism.py` — **done Apr 18** (5 tests, real engine on golden 2026-04-15)
- `tests/unit/test_log_redaction.py` — covered by `tests/unit/test_json_logging.py`
- `tests/unit/test_day_replay.py` — **done Apr 18** (30 tests; incl. 6 for opt-in DB-mirror writer)
- `tests/unit/test_replay_counterfactual.py` — **done Apr 18** (22 tests; snapshot-fallback flavour locked in)
- `tests/unit/test_snapshot_freshness.py` — **done Apr 18** (8 tests; ok/warn/fail/recommended-missing levels)
- `src/observability/snapshot_freshness.py` — **done Apr 18** (consumed by `verify_system.py` §10b)
- `src/db/models/replay_run.py` — **done Apr 18** (ORM mirror of `data/replay_runs.jsonl`)
- `alembic/versions/2026_04_18_b2c3d4e5f607_add_replay_runs_table.py` — **done Apr 18** (vanilla PG; promote to hypertable in single follow-up)
- `config/snapshots/<date>/` (auto-populated; now includes `params.json`)
- `data/heartbeat/recorder.json`, `data/heartbeat/trader.json` (auto-populated)
- `data/replay_runs.jsonl` (auto-populated by every `replay_day` call; SQL `replay_runs` table mirrors when DB is reachable)
- `data/logs/trader.jsonl`, `data/logs/recorder.jsonl` (auto-populated by `setup_logging`)
- `archive/manifest.jsonl` — auto-populated by `scripts/archive_to_parquet.py` (Apr 18)
- `archive/manifest_b2.jsonl` — auto-populated by `scripts/upload_to_b2.py` (separate ledger; uploader never mutates the archive ledger)

**Modified**:
- `src/main.py` — recorder instantiation gated on `recorder_split_mode`; `setup_logging` now writes `data/logs/trader.jsonl`
- `src/config.py` — `recorder_split_mode` flag
- `src/core/events.py` — `publish_local()` + `RedisEventBridge`
- `src/db/models/__init__.py` — registers `ReplayRunModel` for Alembic auto-detect (Apr 18)
- `src/backtest/day_replay.py` — opt-in `db_session_factory` kwarg + `_persist_to_db` helper for the SQL mirror (Apr 18)
- `Makefile` — added `recorder`, `trader`, `replay-day`, `replay-golden`, `replay-ci`, `snapshot-config`; cold-storage chain `archive`, `upload`, `restore-test`, `cold-storage` (Apr 18)
- `scripts/verify_system.py` — heartbeat freshness check (recorder + trader); §10b config-snapshot freshness for previous trading day (Apr 18)
- `scripts/nightly_audit.py` — heartbeat audit + per-stream counts; counterfactual replay diff (§3e); shared `db_engine` reused across §3e (replay row) and §4 (confluence_audits row); surfaces `params_source` with FALLBACK marker (Apr 18)
- `scripts/replay_counterfactual.py` — threads `db_session_factory` through to `replay_day` so the counterfactual run also lands in the SQL ledger (Apr 18)
- `deploy/cron/README.md` — documented three-layer snapshot defense-in-depth chain (06:30 verify → 09:00 cron → 21:00 counterfactual) (Apr 18)
- `pyproject.toml` — no new deps; structured JSON logging uses stdlib only (no prometheus_client)

---

## Appendix B: Open questions

1. **Single-host vs split-host for recorder?** Start single-host (free), evaluate after 30 days.
2. ~~**Loki vs grep on JSONL?**~~ Resolved Apr 17: stay on local `jq`/`grep` indefinitely; revisit only if we add a second host. See §8.1.
3. **TimescaleDB compression policy?** Default to compress chunks > 7 days old; revisit when DB > 50GB.
4. **B2 vs S3 vs GCP?** B2 is 4–5x cheaper for our access pattern; pick B2 unless someone has S3 credits.
5. **Golden day for CI?** Pick a clean day with mixed regime (suggest 2026-04-08 or 2026-04-15 once verified clean).

---

**Owner**: tbd
**Review cadence**: monthly until P3 complete, then quarterly
**Last updated**: 2026-04-18
