"""Tests that lock in the per-strategy calibration isolation contract.

May 7 2026 (branch FnO-v5-orchestration-impl).

The contract these tests enforce:

  1. Each strategy in the active V5 roster has its own calibration
     module under ``src/strategy/calibrations/``.

  2. Each calibration module exports the canonical interface:
       - PARAMS_CLASS  (the Pydantic class — checked indirectly via name)
       - REGIME_FAMILY (str — required for orchestrator dispatch)
       - compute_regime_confidence(detector, underlying) -> float
       - <STRATEGY>_CONFIG (when the strategy has a legacy 0-100 score config)

  3. Code-level isolation: each calibration module imports NO OTHER
     strategy's calibration module. The single allowed exception is
     ``iron_butterfly`` importing from ``iron_condor`` (IB inherits IC's
     params class by user-confirmed design).

  4. Strategy implementation classes correctly point at their
     calibration module via the ``calibration`` class attribute.

  5. Recalibrating strategy X (changing one of its params class
     defaults) does NOT change strategy Y's params.

These tests should fail loudly if a future commit re-introduces
cross-strategy coupling — e.g. by sharing a Pydantic class across two
calibration modules, or by adding a forbidden cross-module import.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


CALIBRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "strategy" / "calibrations"

# Active V5 roster — every strategy here MUST have a calibration module
# at ``src/strategy/calibrations/<name>.py``.
ACTIVE_ROSTER: list[str] = [
    "iron_condor",
    "iron_butterfly",
    "short_strangle",
    "short_straddle",
    "long_calendar",
    "long_straddle",
    "trend_daily",
    "trend_itm",
    "trend_debit_spread",
    "orchestrator",
]

# Strategies that publish a regime confidence hook (orchestrator does
# not — it routes across families rather than belonging to one).
HAS_REGIME_HOOK: set[str] = {
    "iron_condor", "iron_butterfly",
    "short_strangle", "short_straddle",
    "long_calendar", "long_straddle",
    "trend_daily", "trend_itm", "trend_debit_spread",
}

# Strategies that publish a legacy 0-100 score config in their module.
HAS_SCORE_CONFIG: dict[str, str] = {
    "iron_condor": "IRON_CONDOR_CONFIG",
    "short_strangle": "SHORT_STRANGLE_CONFIG",
    "short_straddle": "SHORT_STRADDLE_CONFIG",
    "trend_debit_spread": "TREND_DEBIT_SPREAD_CONFIG",
}

# The ONE permitted cross-strategy import. IB's params class inherits
# from IC's, by user-confirmed design (kept inheritance Q2 in the V5
# branch design conversation).
ALLOWED_CROSS_IMPORTS: dict[str, set[str]] = {
    "iron_butterfly": {"iron_condor"},
}


# ── Module presence ────────────────────────────────────────────────


@pytest.mark.parametrize("strategy", ACTIVE_ROSTER)
def test_each_strategy_has_calibration_module(strategy: str):
    """One file per active strategy: src/strategy/calibrations/<strategy>.py"""
    path = CALIBRATIONS_DIR / f"{strategy}.py"
    assert path.exists(), f"missing calibration module: {path}"


# ── Canonical interface: REGIME_FAMILY ─────────────────────────────


@pytest.mark.parametrize("strategy", ACTIVE_ROSTER)
def test_each_calibration_exports_regime_family(strategy: str):
    module = importlib.import_module(f"src.strategy.calibrations.{strategy}")
    assert hasattr(module, "REGIME_FAMILY"), \
        f"calibrations/{strategy}.py must export REGIME_FAMILY"
    family = module.REGIME_FAMILY
    assert family in {"premium_selling", "long_vol", "directional_trend", "unknown"}, \
        f"calibrations/{strategy}.py REGIME_FAMILY={family!r} not canonical"


# ── Canonical interface: compute_regime_confidence ─────────────────


@pytest.mark.parametrize("strategy", sorted(HAS_REGIME_HOOK))
def test_each_strategy_with_regime_family_exports_confidence_hook(strategy: str):
    module = importlib.import_module(f"src.strategy.calibrations.{strategy}")
    assert hasattr(module, "compute_regime_confidence"), \
        f"calibrations/{strategy}.py must export compute_regime_confidence"
    assert callable(module.compute_regime_confidence)


# ── Canonical interface: legacy score config (when applicable) ─────


@pytest.mark.parametrize("strategy,config_name", sorted(HAS_SCORE_CONFIG.items()))
def test_each_strategy_with_score_config_exports_it(strategy: str, config_name: str):
    module = importlib.import_module(f"src.strategy.calibrations.{strategy}")
    assert hasattr(module, config_name), \
        f"calibrations/{strategy}.py must export {config_name}"


# ── Isolation: NO cross-strategy imports ───────────────────────────


def _imports_in_module(module_path: Path) -> set[str]:
    """Static AST scan of `import ...` and `from ... import ...` lines.

    Returns the set of fully-qualified module strings imported. Used to
    detect cross-strategy coupling without actually loading the module.
    """
    tree = ast.parse(module_path.read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


@pytest.mark.parametrize("strategy", ACTIVE_ROSTER)
def test_calibration_module_imports_no_other_strategy(strategy: str):
    """A calibration module imports NO OTHER strategy's calibration module.

    Locks in the user's "without impacting other strategy code" rule:
    if a future commit adds an import like
    ``from src.strategy.calibrations.short_strangle import ...`` inside
    iron_condor.py, this test fails — flagging that recalibrating IC
    would now require touching SS's file too.
    """
    module_path = CALIBRATIONS_DIR / f"{strategy}.py"
    imports = _imports_in_module(module_path)

    forbidden_prefixes = {
        f"src.strategy.calibrations.{other}"
        for other in ACTIVE_ROSTER
        if other != strategy and other not in ALLOWED_CROSS_IMPORTS.get(strategy, set())
    }

    leaked = {imp for imp in imports if imp in forbidden_prefixes}
    assert not leaked, (
        f"calibrations/{strategy}.py imports forbidden cross-strategy modules: {leaked}. "
        f"Allowed cross-imports: {ALLOWED_CROSS_IMPORTS.get(strategy, set())}"
    )


def test_iron_butterfly_does_import_iron_condor():
    """The single allowed exception: IB depends on IC by design."""
    imports = _imports_in_module(CALIBRATIONS_DIR / "iron_butterfly.py")
    assert "src.strategy.calibrations.iron_condor" in imports, \
        "iron_butterfly.py must import from iron_condor.py (inheritance contract)"


# ── Strategy implementation correctly references its calibration ──


@pytest.mark.parametrize("strategy", sorted(HAS_REGIME_HOOK))
def test_strategy_class_points_at_its_calibration_module(strategy: str):
    """Each strategy class declares ``calibration = <its module>``."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import _REGISTRY

    cls = _REGISTRY[strategy]
    cal = getattr(cls, "calibration", None)
    assert cal is not None, f"{cls.__name__} missing calibration class attr"
    expected = f"src.strategy.calibrations.{strategy}"
    assert cal.__name__ == expected, (
        f"{cls.__name__}.calibration is {cal.__name__!r}, expected {expected!r}"
    )


# ── Recalibration isolation: editing X's params does not affect Y ──


def test_changing_ic_param_does_not_change_ss_param():
    """Different params classes — proves field-level isolation."""
    from src.strategy.calibrations.iron_condor import IronCondorParams
    from src.strategy.calibrations.short_strangle import ShortStrangleParams

    ic = IronCondorParams()
    ss = ShortStrangleParams()
    # Mutate IC instance — should not touch SS class defaults
    ic_pt = ic.profit_target_pct
    ss_pt = ss.profit_target_pct

    # Construct a NEW IC with overridden PT
    ic2 = IronCondorParams(profit_target_pct=ic_pt + 10.0)
    ss2 = ShortStrangleParams()  # fresh — should still have original default

    assert ic2.profit_target_pct == ic_pt + 10.0
    assert ss2.profit_target_pct == ss_pt
    # And the IC mutation didn't affect SS's class defaults
    assert ShortStrangleParams().profit_target_pct == ss_pt


def test_iron_butterfly_inherits_from_iron_condor_at_class_level():
    """IB inherits from IC by design — confirm the relationship is real."""
    from src.strategy.calibrations.iron_condor import IronCondorParams
    from src.strategy.calibrations.iron_butterfly import IronButterflyParams

    assert issubclass(IronButterflyParams, IronCondorParams)


def test_calibration_modules_export_their_params_class():
    """Every active strategy's calibration module exports the params class."""
    from src.strategy.calibrations.iron_condor import IronCondorParams
    from src.strategy.calibrations.iron_butterfly import IronButterflyParams
    from src.strategy.calibrations.short_strangle import ShortStrangleParams
    from src.strategy.calibrations.short_straddle import ShortStraddleParams
    from src.strategy.calibrations.long_calendar import LongCalendarParams
    from src.strategy.calibrations.long_straddle import LongStraddleParams
    from src.strategy.calibrations.trend_daily import TrendDailyParams
    from src.strategy.calibrations.trend_itm import TrendITMParams
    from src.strategy.calibrations.trend_debit_spread import TrendDebitSpreadParams
    from src.strategy.calibrations.orchestrator import OrchestratorParams

    # Smoke: each is constructible
    for cls in (IronCondorParams, IronButterflyParams, ShortStrangleParams,
                ShortStraddleParams, LongCalendarParams, LongStraddleParams,
                TrendDailyParams, TrendITMParams, TrendDebitSpreadParams,
                OrchestratorParams):
        cls()  # default construction should not raise


def test_backwards_compat_imports_from_params_module():
    """Legacy ``from src.strategy.params import IronCondorParams`` still works."""
    from src.strategy.params import (
        IronCondorParams, IronButterflyParams, ShortStrangleParams,
        ShortStraddleParams, LongCalendarParams, LongStraddleParams,
        TrendDailyParams, TrendITMParams, TrendDebitSpreadParams,
        OrchestratorParams, BaseStrategyParams,
    )
    # Same identity as the calibration-module export
    from src.strategy.calibrations.iron_condor import IronCondorParams as IC2
    assert IronCondorParams is IC2


def test_backwards_compat_imports_from_scoring_module():
    """Legacy ``from src.strategy.scoring import IRON_CONDOR_CONFIG`` still works."""
    from src.strategy.scoring import (
        IRON_CONDOR_CONFIG, SHORT_STRANGLE_CONFIG,
        SHORT_STRADDLE_CONFIG, TREND_DEBIT_SPREAD_CONFIG,
        ScoreConfig, score_strategy,
    )
    from src.strategy.calibrations.iron_condor import IRON_CONDOR_CONFIG as IC2
    assert IRON_CONDOR_CONFIG is IC2


# ── Recalibration hook actually fires ───────────────────────────────


def test_compute_regime_confidence_hook_called_when_calibration_set(monkeypatch):
    """When a strategy has its calibration class attr, evaluate_regime_confidence
    invokes that calibration module's hook (NOT the family-level fallback)."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    from src.strategy.calibrations import iron_condor as ic_cal

    # Stub the hook to a known sentinel value
    monkeypatch.setattr(ic_cal, "compute_regime_confidence", lambda d, u: 0.873)

    ic = create_strategy("iron_condor", strategy_id="ic_hook")
    # Wire a minimal context with a regime detector stand-in
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx._regime_detector = MagicMock()
    ic.set_context(ctx)
    # _regime is the conventional attribute strategies use to cache the
    # detector; setting either path lets the hook find a non-None detector.
    ic._regime = ctx._regime_detector

    val = ic.evaluate_regime_confidence()
    assert val == pytest.approx(0.873)
