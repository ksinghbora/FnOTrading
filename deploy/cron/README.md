# Operational cron jobs

Schedules every recurring job the FnO trading system needs. See
`docs/DATA_RELIABILITY_PLAN.md` §10 for why each one exists.

## Install

1. Edit `crontab.example` — replace `/path/to/FnOTrading` with the actual
   repo root on this host.
2. **Confirm the host timezone is `Asia/Kolkata`**:
   ```sh
   timedatectl  # should show "Time zone: Asia/Kolkata (IST, +0530)"
   ```
   If it isn't, either `sudo timedatectl set-timezone Asia/Kolkata` or shift
   every hour in the file by your local offset.
3. Install:
   ```sh
   crontab deploy/cron/crontab.example
   ```
   Or merge into your existing crontab manually.
4. Verify:
   ```sh
   crontab -l
   ```

## Logs

Each job appends to `logs/cron_<name>.log`. Rotate these once they get
chatty (logrotate config TBD — for now `find logs -name "cron_*.log" -mtime +30 -delete`
in a weekly cron is fine).

## Adding a new cron

When you add a new job, also:
- Append it to `crontab.example` with a comment explaining what + why.
- Mention it in `docs/DATA_RELIABILITY_PLAN.md` so the plan stays current.
- If it Telegrams, include `--quiet` for hourly/sub-hourly jobs to keep
  log files small.

## Snapshot freshness — defense in depth

The 09:00 IST `snapshot_config.py` job is the most replay-critical entry in
this file: the counterfactual replay (run by `nightly_audit.py` at 21:00 IST)
silently falls back to live params if today's `config/snapshots/<date>/` is
missing, which makes the divergence number meaningless. Three independent
checks now guard against that silent failure:

1. **06:30 IST `verify_system.py` §10b** — asserts that **yesterday's**
   snapshot is on disk and complete. If not, the cron is broken and you
   have until 09:00 IST to fix it before today's snapshot is also lost.
2. **09:00 IST `snapshot_config.py`** — the cron itself. Logs to
   `logs/cron_snapshot.log`; tail it after install to confirm a clean run.
3. **21:00 IST `nightly_audit.py` §3e** — counterfactual emits a third
   alert flavour ("Snapshot-fallback warning") with the banner prepended
   to any divergence/replay-error alert when `params_source` doesn't start
   with `snapshot:`. Operator sees the comparison was degraded *before*
   reading the divergence number.

If you only have time to install one cron from this file, install the
snapshotter — without it, every counterfactual is degraded.

## Cold-storage chain — install all three or none

The cold-storage chain (`docs/DATA_RELIABILITY_PLAN.md` §3.3) has three crons
that only make sense as a unit:

| Time | Cron | Job | Failure mode if skipped |
|------|------|-----|-------------------------|
| 22:00 IST mon–fri | `archive_to_parquet.py` | CSV → Parquet+ZSTD on local SSD | hot disk fills up; no Parquet to upload |
| 23:00 IST mon–fri | `upload_to_b2.py`       | Push Parquet to Backblaze B2     | local-only archive; one bad disk → total data loss |
| 09:00 IST sun     | `restore_test.py`       | DR proof: download + SHA1 + decode + row count | upload "succeeds" but is corrupt; nobody finds out until restore is needed |

The third one is the one teams skip and regret. **Install it the same week
you install the first two.** It exits 2 on any real failure so the cron MTA
emails you (or your monitoring picks it up).

Required env for the upload step (in `.env` on the box where the cron runs):
- `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET` for production (`b2sdk` is in the
  base deps, no extra install needed).
- For dev/CI, set `ARCHIVE_REMOTE=local-dir:./fake-b2` to exercise the full
  chain without B2 creds — `make cold-storage` does exactly this.

The uploader **fails loud** when `ARCHIVE_REMOTE` is `b2` (or unset, which
defaults to `b2`) but creds are missing. A cron that "succeeds" by uploading
nothing for six weeks is the failure mode this whole chain exists to prevent.
