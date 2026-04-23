"""Integration smoke test for scripts/validate_strategy.py.

Skips when GDFL data isn't available. When it is, runs a minimal CPCV
+ walk-forward over a tiny window and asserts the report is written
with a final verdict line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GDFL_DIR = REPO_ROOT / "data" / "gdfl_snapshots"


@pytest.mark.skipif(
    not GDFL_DIR.exists() or not any(GDFL_DIR.glob("*.parquet")),
    reason="GDFL parquet data not available",
)
def test_validate_strategy_cli_smoke(tmp_path: Path) -> None:
    out_md = tmp_path / "validation_smoke.md"
    script = REPO_ROOT / "scripts" / "validate_strategy.py"

    cmd = [
        sys.executable,
        str(script),
        "--strategy", "portfolio",
        "--train-end", "2025-02-28",
        "--val-end", "2025-03-31",
        "--holdout-end", "2025-04-30",
        "--parquet-dir", str(GDFL_DIR),
        "--cpcv-folds", "5",
        "--cpcv-max-paths", "10",
        "--wf-train-days", "30",
        "--wf-test-days", "15",
        "--out", str(out_md),
        "--log-level", "WARNING",
    ]

    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    # Exit code can be 0 (PASS), 1 (FAIL), or 2 (error). The smoke test
    # just wants to prove the pipeline runs end-to-end; 0/1 are both fine.
    assert proc.returncode in (0, 1), (
        f"CLI exited with {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )

    assert out_md.exists(), (
        f"Report not written\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )

    text = out_md.read_text()
    assert "Final Verdict" in text
    assert "## 1. Executive Summary" in text
