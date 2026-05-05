"""Parse the IC ablation reports + emit a comparison summary.

Reads each ``{config}_smoke.md`` in ``reports/standalone_post_sebi/ablation/``
and extracts the WF gate values (mean_test_sharpe, frac_positive,
median_decay), the WF window-level test sharpes, trade count, and the
gate-pass-fail count. Writes ``ABLATION_SUMMARY.md`` next to the inputs.

Decision rule per filter: if dropping it leaves WF mean_test_sharpe
within 0.1 of the baseline, that filter contributes no measurable
signal — drop it. If dropping it materially DEGRADES WF
mean_test_sharpe (>0.2 worse), the filter is real signal — keep it.
"""
from __future__ import annotations

import re
from pathlib import Path

ABL = Path("reports/standalone_post_sebi/ablation")

CONFIGS = [
    ("00_baseline", "Baseline (all 6 filters tightened)"),
    ("01_drop_score", "Drop score 85→60"),
    ("02_drop_pcr", "Drop PCR 0.85-1.20→0.70-1.50"),
    ("03_drop_max_pain", "Drop max_pain 1.5%→3.0%"),
    ("04_drop_adj_threshold", "Drop adj 85→60"),
    ("05_drop_intraday_vix_spike", "Drop intraday VIX spike filter"),
    ("06_drop_vol_scaled_exits", "Drop vol-scaled exits"),
    ("07_minimal", "Minimal (drop ALL 3 noise filters together)"),
]


def parse_report(path: Path) -> dict:
    """Extract headline metrics from one validation .md report."""
    if not path.exists():
        return {"missing": True}
    text = path.read_text()
    out: dict = {"missing": False}

    # WF section header + summary line
    wf_match = re.search(
        r"Windows: (\d+) \| median_decay=([\-\d\.]+) \| frac_positive=([\-\d\.]+) \| mean_test_sharpe=([\-\d\.]+)",
        text,
    )
    if wf_match:
        out["windows"] = int(wf_match.group(1))
        out["median_decay"] = float(wf_match.group(2))
        out["frac_positive"] = float(wf_match.group(3))
        out["mean_test_sharpe"] = float(wf_match.group(4))

    # Per-window test Sharpes from the WF table
    test_sharpes: list[float] = []
    for line in text.splitlines():
        # Table rows: "| 0 | date..date | date..date | train | test | decay | n |"
        m = re.match(
            r"^\| \d+ \| [\d\-]+\.\.[\d\-]+ \| [\d\-]+\.\.[\d\-]+ \| ([\-\d\.]+) \| ([\-\d\.]+) \| ([\-\d\.]+) \| (\d+) \|",
            line.strip(),
        )
        if m:
            test_sharpes.append(float(m.group(2)))
    out["test_sharpes"] = test_sharpes

    # Final verdict
    verdict = re.search(r"Final Verdict:\s*(\w+)", text)
    out["verdict"] = verdict.group(1) if verdict else "?"

    # Trade count from full-window section / capacity table
    trades_match = re.search(r"collected\s+(\d+)\s+trades", text)
    if trades_match:
        out["trades"] = int(trades_match.group(1))

    # Gate count: rough — find "PASS" markers in section 1 table
    section_1 = text.split("## 2.")[0] if "## 2." in text else text
    out["pass_count"] = section_1.count("| PASS |")
    out["fail_count"] = section_1.count("| FAIL |")

    return out


def main() -> None:
    rows: list[dict] = []
    for cfg, label in CONFIGS:
        report_path = ABL / f"{cfg}_smoke.md"
        log_path = ABL / f"{cfg}_smoke.log"
        d = parse_report(report_path)
        d["cfg"] = cfg
        d["label"] = label
        # Pull trade count from log if not in report
        if "trades" not in d and log_path.exists():
            for line in log_path.read_text().splitlines():
                m = re.search(r"collected\s+(\d+)\s+trades", line)
                if m:
                    d["trades"] = int(m.group(1))
                    break
        rows.append(d)

    baseline = rows[0]
    base_mean = baseline.get("mean_test_sharpe")

    out_lines: list[str] = [
        "# IC Strong-Signal Filter Ablation — Summary",
        "",
        "May 2 2026. Each row drops ONE filter from the strong-signal IC config "
        "back to its default value, runs a 5-window walk-forward (CPCV skipped), "
        "and compares WF mean_test_sharpe to the baseline. Methodology = WF-primary "
        "(post-May-2 refactor); CPCV demoted to diagnostic-only.",
        "",
        "## Results",
        "",
        "| Config | WF mean_test_sharpe | Δ vs baseline | frac_positive | median_decay | trades | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("missing"):
            out_lines.append(
                f"| {r['label']} | (report missing) | | | | | |"
            )
            continue
        mean = r.get("mean_test_sharpe", float("nan"))
        delta = mean - base_mean if base_mean is not None else 0.0
        delta_str = f"{delta:+.3f}" if r["cfg"] != "00_baseline" else "—"
        out_lines.append(
            f"| {r['label']} | {mean:+.3f} | {delta_str} | "
            f"{r.get('frac_positive', 0):.2f} | {r.get('median_decay', 0):.3f} | "
            f"{r.get('trades', '?')} | {r.get('verdict', '?')} |"
        )

    out_lines.extend([
        "",
        "## Decision Rule",
        "",
        "* **|Δ| ≤ 0.10** → filter is NOISE — safe to drop without loss",
        "* **−0.20 ≤ Δ < −0.10** → filter is MARGINAL — borderline, keep on inertia",
        "* **Δ < −0.20** → filter is REAL SIGNAL — keep it; dropping degrades generalization",
        "* **Δ > +0.10** → filter HURTS performance — actively drop it (rare but possible)",
        "",
        "## Per-window Test Sharpe Distribution",
        "",
        "| Config | Window 0 | Window 1 | Window 2 | Window 3 | Window 4 |",
        "|---|---|---|---|---|---|",
    ])
    for r in rows:
        if r.get("missing"):
            continue
        ts = r.get("test_sharpes", [])
        cells = [f"{x:+.2f}" for x in ts] + ["—"] * (5 - len(ts))
        out_lines.append(
            f"| {r['label']} | {' | '.join(cells[:5])} |"
        )

    out_lines.extend([
        "",
        "## Recommendation",
        "",
        "Filters where dropping them leaves WF mean_test_sharpe materially unchanged "
        "are noise — drop them and re-evaluate the simpler config. Filters where "
        "dropping them degrades WF significantly are pulling real weight — keep them.",
        "",
        "The simplest config that matches baseline WF performance is the right config — "
        "fewer parameters = less curve-fit risk = more likely to hold up out of sample.",
    ])

    out_path = ABL / "ABLATION_SUMMARY.md"
    out_path.write_text("\n".join(out_lines))
    print(f"Summary written: {out_path}")
    # Also print the table to stdout
    print()
    for line in out_lines:
        if line.startswith("|") or line.startswith("##"):
            print(line)


if __name__ == "__main__":
    main()
