#!/usr/bin/env bash
# May 2 2026 — IC strong-signal filter ablation runner.
# Sequential execution to avoid the parallel-decisions race; each
# config produces its own validation report. After all 7 runs land,
# the comparison summary is produced by parse_ic_ablation.py.
#
# Each smoke uses --skip-cpcv (~50% faster than CPCV+WF runs) since
# WF is the primary OOS verdict post-May-2 refactor.
#
# Usage:  scripts/run_ic_ablation.sh
# Output: reports/standalone_post_sebi/ablation/{config}_smoke.{md,log}
#
# Total runtime: ~7 × ~15 min = ~100 min sequential.

# NOTE: validate_strategy.py exit code semantics:
#   0 = PASS (all gates passed, report written)
#   1 = FAIL (gates failed, but report was still written — useful for us!)
#   2 = ERROR (harness couldn't run, no report)
# We want to CONTINUE on FAIL (each ablation can produce a usable
# report even if its strategy fails), but HALT on ERROR. So we don't
# use ``set -e`` and instead inspect rc per config.
set -uo pipefail

ABL_DIR="reports/standalone_post_sebi/ablation"

# If you re-run mid-ablation, set SKIP_BASELINE=1 to skip 00_baseline:
#   SKIP_BASELINE=1 ./scripts/run_ic_ablation.sh
ALL_CONFIGS=(
    "00_baseline"
    "01_drop_score"
    "02_drop_pcr"
    "03_drop_max_pain"
    "04_drop_adj_threshold"
    "05_drop_intraday_vix_spike"
    "06_drop_vol_scaled_exits"
)
if [ "${SKIP_BASELINE:-0}" = "1" ]; then
    CONFIGS=("${ALL_CONFIGS[@]:1}")
    echo "SKIP_BASELINE=1 — skipping 00_baseline (assumed already complete)"
else
    CONFIGS=("${ALL_CONFIGS[@]}")
fi

START=$(date +%s)
echo "=== IC ablation start: $(date) ==="

for cfg in "${CONFIGS[@]}"; do
    cfg_start=$(date +%s)
    echo
    echo ">>> Running ${cfg} ..."
    .venv/bin/python scripts/validate_strategy.py \
        --strategy iron_condor \
        --train-end 2025-05-30 --val-end 2025-07-31 --holdout-end 2026-02-27 \
        --parquet-dir data/gdfl_v2 --underlying NIFTY \
        --corpus-from 2024-11-20 \
        --workers 1 \
        --cpcv-folds 5 --cpcv-max-paths 5 --cpcv-n-test-folds 2 \
        --wf-train-days 60 --wf-test-days 20 --wf-step-days 20 --wf-workers 1 \
        --skip-cpcv \
        --baseline-params-json "${ABL_DIR}/${cfg}.json" \
        --out "${ABL_DIR}/${cfg}_smoke.md" \
        --log-level INFO > "${ABL_DIR}/${cfg}_smoke.log" 2>&1
    rc=$?
    cfg_end=$(date +%s)
    if [ $rc -eq 0 ]; then
        verdict="PASS"
    elif [ $rc -eq 1 ]; then
        verdict="FAIL (continuing — report still written)"
    else
        verdict="ERROR rc=$rc — HALTING"
    fi
    echo "    ${cfg} done in $((cfg_end - cfg_start)) sec — verdict=$verdict"
    if [ $rc -ge 2 ]; then
        echo "Halting on harness error."
        exit 1
    fi
done

END=$(date +%s)
echo
echo "=== IC ablation done: $(date) ==="
echo "Total elapsed: $((END - START)) sec ($((($END - $START) / 60)) min)"
echo
echo "Reports written to: ${ABL_DIR}/"
ls -1 "${ABL_DIR}"/*_smoke.md
