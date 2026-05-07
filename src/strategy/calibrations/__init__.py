"""Per-strategy calibration modules.

May 7 2026 (branch FnO-v5-orchestration-impl).

## Why this directory exists

Each trading strategy's tunable knobs — Pydantic params class defaults,
score-config thresholds, regime-confidence weights — lives in ONE
strategy-specific module here. Recalibrating strategy X means editing
exactly one file: ``src/strategy/calibrations/X.py``. No other strategy's
code is touched.

This is the **offline, code-level isolation** version of recalibration.
It is NOT a hot-reload system — restart the daemon for changes to take
effect.

## Layout

```
src/strategy/calibrations/
├── __init__.py             ← this file (helpers + isolation contract)
├── iron_condor.py          ← IronCondorParams + IRON_CONDOR_CONFIG
│                              + REGIME_FAMILY + compute_regime_confidence
├── iron_butterfly.py       ← IronButterflyParams (inherits IC's class)
│                              + IB-specific overrides for the regime hook
├── short_strangle.py
├── short_straddle.py
├── long_calendar.py
├── long_straddle.py
├── trend_daily.py
├── trend_itm.py
├── trend_debit_spread.py
├── orchestrator.py         ← OrchestratorParams (no scoring config)
└── portfolio.py            ← PortfolioParams (legacy unified strategy)
```

## What each module exports

```python
PARAMS_CLASS = <PydanticParamsClass>      # Strategy's params type
SCORE_CONFIG = <ScoreConfig | None>       # Legacy 0-100 scoring config (None when unused)
REGIME_FAMILY = "premium_selling" | "long_vol" | "directional_trend" | "unknown"

def compute_regime_confidence(
    detector: RegimeDetector, underlying: str
) -> float:
    '''Strategy-specific 0.0-1.0 regime confidence.

    Default implementation dispatches to the family-level method on
    RegimeDetector (regime_confidence_for_premium_selling, etc.).
    Override here if strategy X needs different weights without
    affecting any other strategy's confidence math.
    '''
```

## The recalibration contract

When the operator wants to tune strategy X:

  1. Edit `src/strategy/calibrations/X.py` ONLY
  2. Restart the daemon
  3. Strategy X picks up new values; A/B/C/D unaffected

Forbidden: editing two strategies' calibrations in one commit (defeats
the isolation purpose). If a change DOES touch two strategies (e.g.,
re-tuning the family-level regime confidence math in `regime.py`), that
is a family-level change, not a single-strategy recalibration, and
should be discussed/reviewed accordingly.

## The one allowed cross-module dependency

``iron_butterfly.py`` imports its params base class from
``iron_condor.py`` because IronButterflyParams inherits from
IronCondorParams (and IronButterflyStrategy inherits from
IronCondorStrategy). This dependency is intentional — IB is
documented as the capital-efficient cousin of IC. To recalibrate IC
in a way that should NOT affect IB, define IB-specific overrides in
``iron_butterfly.py``.

## Backward compatibility

``src/strategy/params.py`` re-exports each migrated params class for
existing imports (`from src.strategy.params import IronCondorParams`
keeps working). New code should import directly from the calibration
module:

```python
from src.strategy.calibrations.iron_condor import IronCondorParams
```
"""

# No public exports here — each strategy imports its own calibration
# module by name. Listing them here would create the cross-module
# coupling we are explicitly trying to avoid.
