"""Counterfactual variants framework (Apr 17 trader-analysis #15).

The Apr 17 review flagged that we tune parameters by intuition + a single
backtest sweep, then forget the alternatives. This module records what
*other* parameter choices would have decided at every live decision point,
so that 30 days later a reviewer can answer:

  "Would entry-threshold 70 (vs current 65) have skipped this losing trade
   without missing winners?"
  "Would VIX min 14 (vs 13) have caught the Apr 8 chop day?"

It does NOT change live trading. The variants are pure decision-replay over
identical market features. CSV one row per (decision, variant) pair lands
in `data/counterfactuals/YYYY-MM-DD.csv` for offline analysis.

Wiring (caller's responsibility):
    >>> evaluator = CounterfactualEvaluator([
    ...     VariantSpec("threshold_70", score_threshold=70),
    ...     VariantSpec("vix_min_14", vix_entry_min=14.0),
    ...     VariantSpec("trail_after_11", trail_stop_activate_after_time=time(11, 0)),
    ... ])
    >>> # At each ENTRY decision, after computing rule_score:
    >>> evaluator.record_entry_decision(
    ...     base_decision="ENTER",
    ...     base_score=68,
    ...     features={"vix": 14.5, "spot": 24500, "pcr_oi": 1.0, "iv_skew": 0.95, ...},
    ...     base_threshold=65,
    ... )

The evaluator never raises — IO failures are logged and swallowed so a
counterfactual bug can't break live trading.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from src.core.clock import now_ist

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path("data/counterfactuals")


@dataclass(frozen=True)
class VariantSpec:
    """A counterfactual parameter set.

    Each field is an OPTIONAL override. Fields left as the sentinel default
    (`_UNSET`) inherit the live config — only specified fields differ.
    Specify only the dimensions you actually want to compare; leaving 95%
    of fields blank keeps variants readable.
    """

    name: str
    # Score gating
    score_threshold: int | None = None        # Override entry score threshold

    # VIX gating
    vix_entry_min: float | None = None
    vix_entry_max: float | None = None

    # Trail-stop gates (Apr 17 fix)
    trail_stop_activate_after_time: time | None = None
    trail_stop_min_decay_pct: float | None = None

    # Exit-side knobs
    profit_target_pct: float | None = None
    stop_loss_pct: float | None = None
    trail_stop_pct: float | None = None

    # Filter toggles
    pcr_filter_enabled: bool | None = None
    skip_entry_on_expiry_day: bool | None = None

    def evaluate_entry(
        self,
        base_score: int,
        base_threshold: int,
        features: dict[str, Any],
        is_expiry: bool,
    ) -> str:
        """Return ENTER / SKIP-<reason> for this variant given features.

        Hierarchy of skip reasons (first match wins):
          1. expiry-day block (if variant disabled the override)
          2. VIX outside band
          3. PCR filter (if enabled)
          4. Score below variant threshold
        """
        # Expiry block
        skip_expiry = (
            self.skip_entry_on_expiry_day
            if self.skip_entry_on_expiry_day is not None
            else True
        )
        if is_expiry and skip_expiry:
            return "SKIP-expiry"

        # VIX gate
        vix = float(features.get("vix", 0.0) or 0.0)
        vmin = self.vix_entry_min
        vmax = self.vix_entry_max
        if vix > 0 and vmin is not None and vix < vmin:
            return f"SKIP-vix_low({vix:.1f}<{vmin})"
        if vix > 0 and vmax is not None and vix > vmax:
            return f"SKIP-vix_high({vix:.1f}>{vmax})"

        # PCR filter
        if self.pcr_filter_enabled:
            pcr = float(features.get("pcr_oi", 0.0) or 0.0)
            if pcr > 0 and (pcr < 0.7 or pcr > 1.5):
                return f"SKIP-pcr({pcr:.2f})"

        # Score threshold
        threshold = self.score_threshold if self.score_threshold is not None else base_threshold
        if base_score < threshold:
            return f"SKIP-score({base_score}<{threshold})"

        return "ENTER"

    def evaluate_trail_activation(
        self,
        decay_pct: float,
        now_t: time,
        base_after_time: time,
        base_min_decay: float,
    ) -> bool:
        """Return True if this variant would have activated the trail-stop now."""
        after_time = (
            self.trail_stop_activate_after_time
            if self.trail_stop_activate_after_time is not None
            else base_after_time
        )
        if now_t < after_time:
            return False
        min_decay = (
            self.trail_stop_min_decay_pct
            if self.trail_stop_min_decay_pct is not None
            else base_min_decay
        )
        return decay_pct >= min_decay


@dataclass
class CounterfactualEvaluator:
    """Records what each variant would have decided at every live decision."""

    variants: list[VariantSpec]
    out_dir: Path = DEFAULT_OUT_DIR
    _writer_initialised: dict[Path, bool] = field(default_factory=dict)

    HEADER = (
        "timestamp", "strategy_id", "decision_type",
        "base_decision", "base_score", "base_threshold",
        "variant_name", "variant_decision", "variant_threshold",
        "vix", "spot", "pcr_oi", "is_expiry",
    )

    def record_entry_decision(
        self,
        strategy_id: str,
        base_decision: str,
        base_score: int,
        base_threshold: int,
        features: dict[str, Any],
        is_expiry: bool = False,
    ) -> None:
        """Log how each variant would have decided this entry vs the live one.

        Never raises — IO failures are logged and swallowed.
        """
        try:
            ts = now_ist().isoformat()
            rows: list[tuple] = []
            for v in self.variants:
                vdecision = v.evaluate_entry(
                    base_score=base_score,
                    base_threshold=base_threshold,
                    features=features,
                    is_expiry=is_expiry,
                )
                vthresh = v.score_threshold if v.score_threshold is not None else base_threshold
                rows.append((
                    ts, strategy_id, "ENTRY",
                    base_decision, base_score, base_threshold,
                    v.name, vdecision, vthresh,
                    features.get("vix", 0.0), features.get("spot", 0.0),
                    features.get("pcr_oi", 0.0), is_expiry,
                ))
            self._write_rows(rows)
        except Exception as e:  # noqa: BLE001 — must never break live path
            logger.warning(f"[COUNTERFACTUAL] record_entry failed: {e}")

    def _write_rows(self, rows: list[tuple]) -> None:
        if not rows:
            return
        out_file = self.out_dir / f"{now_ist().date().isoformat()}.csv"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        new_file = not out_file.exists()
        with out_file.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(self.HEADER)
            for row in rows:
                w.writerow(row)


def diff_summary(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Aggregate counterfactual rows: per-variant agree/disagree counts.

    Used by `scripts/weekly_review.py` and ad-hoc analysis. Pure function —
    pass in dict-rows from csv.DictReader.

    Returns: {variant_name: {"agree": N, "disagree": N, "stricter": N, "looser": N}}
      - agree:    variant decision == base decision
      - disagree: variant decision != base decision
      - stricter: variant SKIPped where base ENTERed (would have avoided trade)
      - looser:   variant ENTERed where base SKIPped (would have taken extra trade)
    """
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        name = r.get("variant_name", "")
        if not name:
            continue
        bucket = out.setdefault(name, {"agree": 0, "disagree": 0, "stricter": 0, "looser": 0})
        base = r.get("base_decision", "")
        var = r.get("variant_decision", "")
        if base == var or (base == "ENTER" and var == "ENTER"):
            bucket["agree"] += 1
        else:
            bucket["disagree"] += 1
            if base == "ENTER" and var.startswith("SKIP"):
                bucket["stricter"] += 1
            elif base.startswith("SKIP") and var == "ENTER":
                bucket["looser"] += 1
    return out
