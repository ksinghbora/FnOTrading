# Chain Recorder Corruption — Apr 18 2026 finding

## Symptom

The 13-day chain replay (`scripts/replay_23days.py`) on Apr 17 entered a
TREND debit_spread at spot=22505 followed 28 minutes later by an IC entry
at spot=24268 — a phantom 7.8% intraday move. NIFTY did not actually move
that much. The chain CSV told the strategy it did.

## Concrete evidence

`data/chain_snapshots/chain_2026-04-17.csv` at 11:09:54 vs 11:10:54:
- **11:09**: highest-OI strike = 21500 (deep-ITM CE intrinsic ≈ 988, OI 13K)
- **11:10**: highest-OI strike = 22800 (deep-ITM CE intrinsic ≈ 1466, OI 3.5M)

Two adjacent snapshots one minute apart show the OI distribution
teleporting 1,300 strikes. OI does not redistribute that fast — this is
either two different chains stitched together at the same timestamp, or
the recorder lost its underlying-spot reference and started writing the
wrong strike for ATM.

## Audit pass over all recorded data

`scripts/audit_chain_quality.py` (new ATM-jump check) flags 4 / 14 days:
- Apr 02: 12.5% jump @ 09:40→09:41
- Apr 08: **138.1% jump** @ 09:40→09:41
- Apr 09: 56.4% jump @ 09:28→09:29
- Apr 17: **113.8% jump** @ 11:09→11:10

Common pattern: jumps cluster at session-start (09:15-09:40) or just
before close (15:25-15:30). 10 of 14 days are clean (max OI shift ≤ 9%,
explainable by normal OI rotation).

## What we shipped today (defense, not cure)

1. **Strategy-side guard** (portfolio_strategy.py): `on_tick` rejects
   any spot reading that implies > 2% / minute movement. Even if the
   recorder feeds garbage, the strategy refuses to act on it.
2. **Audit detector** (audit_chain_quality.py): new "corrupt" status
   tier with 10% threshold; quarantines bad days from replay runs.
3. **Replay-mode CSV truncation** (decision_logger.py): replays no
   longer pile duplicate decision rows on top of prior runs.

## What we did NOT do (deferred)

**Root-cause fix in `src/market_data/chain_recorder.py`.** This needs
a multi-hour debug session — likely involves either:
- WebSocket reconnect mid-snapshot losing the underlying spot anchor
- BreezeEnricher swapping in a stale spot from the rate-limit-cached
  call when the live feed stalls
- Two strikes getting deduped on the wrong key

The recorder bug remains live. Until it's fixed, replay results from
Apr 02 / 08 / 09 / 17 should be discarded (audit script will quarantine
them). The strategy-side guard means LIVE trading is protected even if
the recorder itself eventually corrupts a real-time snapshot — but live
mostly reads spot from KiteWebSocket, not the recorder, so the exposure
window in production is narrower than in replay.

## Next session

When picking this up:
1. Read `src/market_data/chain_recorder.py` — focus on snapshot-write
   path and how the underlying spot is captured per snapshot.
2. Reproduce: load `chain_2026-04-17.csv` rows around 11:09-11:10 and
   check whether the recorder logged an exception/reconnect at that
   time (search live logs for that minute window).
3. The fix should ensure a single snapshot writes a single coherent
   chain — likely a per-snapshot transaction or a write-then-rename
   pattern instead of streaming row-by-row.
