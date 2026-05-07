# Per-Strategy Calibration — Design

May 7 2026 · branch `FnO-v5-orchestration-impl`

## 1. The contract

Each trading strategy can be **recalibrated offline at any time without
requiring code changes in any other strategy's files.**

Concretely: when the operator wants to tune iron_condor's profit
target, stop loss, regime confidence weights, or scoring thresholds —
exactly ONE file is edited:

```
src/strategy/calibrations/iron_condor.py
```

Every other strategy's calibration module, the BaseStrategy, the
RegimeDetector, and the legacy params/scoring modules are untouched.

## 2. What the user explicitly told me

After the first attempt over-engineered this with a live hot-reload
manager:

> I'll not recalibrate the strategy over live trading. I was talking
> about offline recalibration and should not impact other strategy code.

So this is **offline + code-isolated**. Restart the daemon for changes
to take effect; no file watcher.

The user also said: keep `IronButterflyStrategy` inheriting from
`IronCondorStrategy`. So IB's calibration module is allowed to import
IC's params class — that's the one cross-module dependency, by
design.

## 3. Architecture

```
src/strategy/
├── base.py                       # BaseStrategy (abstract)
├── params.py                     # BaseStrategyParams + dormant params
│                                 # + lazy re-exports of migrated classes
├── scoring.py                    # ScoreConfig + score_strategy
│                                 # + lazy re-exports of migrated configs
├── regime.py                     # Family-level regime confidence math
│                                 # (CI, VRP, ADX, VIX, DoW)
├── calibrations/
│   ├── __init__.py               # docs only — no exports (per-strategy
│   │                             # isolation requires per-strategy imports)
│   ├── iron_condor.py            ─┐
│   ├── iron_butterfly.py          │ Each module owns:
│   ├── short_strangle.py          │  - Pydantic params class
│   ├── short_straddle.py          │  - Score config (when applicable)
│   ├── long_calendar.py           ├ - REGIME_FAMILY constant
│   ├── long_straddle.py           │  - compute_regime_confidence hook
│   ├── trend_daily.py             │
│   ├── trend_itm.py               │ Per-module isolation locked in
│   ├── trend_debit_spread.py      │ by tests/unit/test_calibration_
│   ├── orchestrator.py            │ isolation.py
│   └── portfolio.py              ─┘
└── implementations/
    ├── iron_condor.py            # imports calibration via:
    │                             #   from src.strategy.calibrations
    │                             #     import iron_condor as iron_condor_calibration
    │                             # then declares: calibration = iron_condor_calibration
    └── ...
```

## 4. What each calibration module exports

```python
from src.strategy.params import BaseStrategyParams
from src.strategy.scoring import ScoreConfig

class XxxParams(BaseStrategyParams):
    """The strategy's Pydantic params — defaults are the calibration."""

XXX_CONFIG = ScoreConfig(
    # Legacy 0-100 scoring weights (only when the strategy uses them)
)

REGIME_FAMILY = "premium_selling"  # | "long_vol" | "directional_trend" | "unknown"

def compute_regime_confidence(detector, underlying) -> float:
    """0.0-1.0 strategy-specific regime confidence.

    Default: dispatch to the family-level RegimeDetector method
    (regime_confidence_for_premium_selling, etc.). Override here when
    a strategy needs different weights — that change CANNOT affect
    any other strategy because each calibration module owns its hook.
    """
    return float(detector.regime_confidence_for_premium_selling(underlying))
```

Strategies without scoring configs (e.g. long_calendar) just omit
`XXX_CONFIG`. Strategies that aren't in a regime family (orchestrator,
portfolio) set `REGIME_FAMILY = "unknown"` and don't define the hook.

## 5. How the strategy class wires it up

Each strategy implementation declares a `calibration` class attribute
pointing at its calibration module:

```python
# src/strategy/implementations/iron_condor.py
from src.strategy.calibrations import iron_condor as iron_condor_calibration
from src.strategy.calibrations.iron_condor import IRON_CONDOR_CONFIG, IronCondorParams

@register_strategy("iron_condor", IronCondorParams)
class IronCondorStrategy(BaseStrategy):
    regime_family: str = "premium_selling"
    calibration = iron_condor_calibration  # ← V5 isolation contract
```

`BaseStrategy.evaluate_regime_confidence` checks for `cls.calibration`
and routes to its `compute_regime_confidence` hook:

```python
def evaluate_regime_confidence(self) -> float:
    cal = type(self).calibration
    if cal is not None and hasattr(cal, "compute_regime_confidence"):
        return float(cal.compute_regime_confidence(regime, underlying))
    # Falls back to family-level dispatch when no calibration declared
    ...
```

So an operator override of `compute_regime_confidence` in
`calibrations/iron_butterfly.py` immediately takes effect for IB on
the next daemon start, without any other code edits.

## 6. The one allowed cross-module dependency

`IronButterflyStrategy(IronCondorStrategy)` — IB extends IC's strategy
class. By the user-confirmed design, IB also inherits IC's params
class:

```python
# src/strategy/calibrations/iron_butterfly.py
from src.strategy.calibrations.iron_condor import (
    IronCondorParams,
    compute_regime_confidence as _ic_compute_regime_confidence,
)

class IronButterflyParams(IronCondorParams):
    short_call_delta: float = 0.5         # ATM (vs IC's 0.15 OTM)
    wing_width_strikes: int = 2           # tighter wings
    expected_margin_per_lot_lakhs: float = 1.5
```

This is the ONLY permitted cross-module import. The isolation test
(`test_calibration_module_imports_no_other_strategy`) explicitly
allows it via:

```python
ALLOWED_CROSS_IMPORTS = {"iron_butterfly": {"iron_condor"}}
```

Any future addition of a forbidden cross-strategy import will fail
that test.

## 7. Backward compatibility (no breaking changes)

Both `params.py` and `scoring.py` keep working as import sources for
the migrated classes via PEP 562 `__getattr__`:

```python
# Legacy code keeps working unchanged:
from src.strategy.params import IronCondorParams
from src.strategy.scoring import IRON_CONDOR_CONFIG
```

These resolve to the calibration module's class/object via
`module.__getattr__`, which lazily imports on first access (avoiding
circular imports at module-init time).

New code SHOULD import directly from the calibration module to make
the per-strategy isolation contract visible at the import site:

```python
from src.strategy.calibrations.iron_condor import IronCondorParams
```

## 8. Recalibration workflows

### 8a. Tune IC's profit target after a Friday loss

Edit `src/strategy/calibrations/iron_condor.py`:

```python
class IronCondorParams(BaseStrategyParams):
    profit_target_pct: float = 30.0    # was 25.0
```

Restart daemon. IC takes effect with new PT. **No other file
touched.** SS, IB, LC, etc. continue with their existing params.

### 8b. Add IB-specific VIX-band weighting in regime confidence

IB's gamma exposure is higher than IC's, so IB might want stricter
VIX band scoring. Edit `src/strategy/calibrations/iron_butterfly.py`:

```python
def compute_regime_confidence(detector, underlying):
    # Custom IB-specific math — strictly tighter VIX peak than IC
    ci = detector.compute_choppiness_index(underlying)
    vrp = detector.compute_vrp(underlying)
    vix = detector._get_vix()
    if ci is None or vrp is None:
        return 0.0
    # IB's tighter VIX peak: only 16-19 = 1.0
    if 16.0 <= vix <= 19.0:
        vix_factor = 1.0
    elif 13.0 <= vix < 16.0 or 19.0 < vix <= 22.0:
        vix_factor = 0.5
    else:
        vix_factor = 0.0
    ci_factor = max(0.0, min(1.0, (ci - 38.2) / (61.8 - 38.2)))
    vrp_factor = max(0.0, min(1.0, (vrp + 2.0) / 4.0))
    return (ci_factor * vrp_factor * vix_factor) ** (1.0 / 3.0)
```

Restart daemon. IB now uses its custom confidence math; IC, SS, SST
keep the family default. **`regime.py` was NOT touched.**

### 8c. Sweep IB's PT/SL parameters

Operator runs `scripts/sweep_ib_pt_sl.py` (a backtesting script).
Each iteration constructs `IronButterflyParams(profit_target_pct=X,
stop_loss_pct=Y)` directly, runs the backtest. The sweep script lives
in `scripts/`; the calibration module is unchanged during the sweep.
Once the operator picks a winner, they update
`calibrations/iron_butterfly.py` with the chosen defaults. Restart
daemon.

## 9. Test enforcement

`tests/unit/test_calibration_isolation.py` (62 tests) locks in the
contract:

| Test class | What it enforces |
|---|---|
| `test_each_strategy_has_calibration_module` | One file per active strategy at `calibrations/<name>.py` |
| `test_each_calibration_exports_regime_family` | `REGIME_FAMILY` constant present and canonical |
| `test_each_strategy_with_regime_family_exports_confidence_hook` | `compute_regime_confidence` callable present |
| `test_each_strategy_with_score_config_exports_it` | Score config present where expected |
| `test_calibration_module_imports_no_other_strategy` | **Cross-strategy isolation** — fails on forbidden imports |
| `test_iron_butterfly_does_import_iron_condor` | Pins the one allowed exception |
| `test_strategy_class_points_at_its_calibration_module` | Implementation class declares correct `calibration` attr |
| `test_changing_ic_param_does_not_change_ss_param` | Field-level isolation between strategies |
| `test_iron_butterfly_inherits_from_iron_condor_at_class_level` | IB→IC inheritance preserved |
| `test_calibration_modules_export_their_params_class` | Smoke-construct each params class |
| `test_backwards_compat_imports_from_params_module` | Legacy import shim still works |
| `test_backwards_compat_imports_from_scoring_module` | Legacy scoring import shim still works |
| `test_compute_regime_confidence_hook_called_when_calibration_set` | Hook actually fires from BaseStrategy |

If a future commit accidentally couples two strategies' calibrations,
one or more of these tests fail.

## 10. What's NOT in this design

- **Live hot-reload.** Out of scope per user clarification. Restart
  daemon for changes to take effect.
- **Calibration file format on disk.** Calibration is Python code
  (the calibration module itself), not JSON/YAML. Operator edits the
  Python file directly.
- **Cross-strategy dependencies (other than IB→IC).** Forbidden by
  the isolation test.
- **Migration of dormant strategies** (delta_neutral, momentum,
  mean_reversion, calendar_spread, expiry_scalper). They stay in
  `params.py` until they enter active recalibration cycles.

## 11. File index

Files added or modified for this branch:

```
src/strategy/calibrations/__init__.py       NEW
src/strategy/calibrations/iron_condor.py    NEW
src/strategy/calibrations/iron_butterfly.py NEW (imports from iron_condor — allowed)
src/strategy/calibrations/short_strangle.py NEW
src/strategy/calibrations/short_straddle.py NEW
src/strategy/calibrations/long_calendar.py  NEW
src/strategy/calibrations/long_straddle.py  NEW
src/strategy/calibrations/trend_daily.py    NEW
src/strategy/calibrations/trend_itm.py      NEW
src/strategy/calibrations/trend_debit_spread.py NEW
src/strategy/calibrations/orchestrator.py   NEW
src/strategy/calibrations/portfolio.py      NEW
src/strategy/params.py                      Reduced to BaseStrategyParams + dormant
                                            classes + lazy re-exports for migrated
src/strategy/scoring.py                     Reduced to ScoreConfig + score_strategy
                                            + lazy re-exports for migrated configs
src/strategy/base.py                        +calibration class attr
                                            +evaluate_regime_confidence routes via hook
src/strategy/implementations/{iron_condor,iron_butterfly,short_strangle,
  short_straddle,long_calendar,long_straddle,trend_daily,trend_itm,
  trend_debit_spread}.py                    Imports from calibrations/ +
                                            declares calibration class attr
scripts/snapshot_config.py                  Walks calibrations/ subpackage too
tests/unit/test_calibration_isolation.py    NEW (62 tests)
docs/CALIBRATION_DESIGN.md                  NEW (this file)
```
