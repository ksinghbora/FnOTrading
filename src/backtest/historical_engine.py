"""Deprecated — compat shim retained only for ``replay_engine.py``.

The original :class:`HistoricalBacktestEngine` (Black-Scholes over real
spot/VIX CSVs) was removed in Phase 1 of the BS-removal refactor. The
two CSV loaders it hosted — ``_load_spot_csv`` and ``_load_vix_csv`` —
moved to :mod:`src.backtest.common` alongside the rest of the shared
backtest plumbing.

This module remains as a pure re-export surface so that the replay
engine, which we intentionally do not modify in Phase 1, keeps
resolving its legacy import:

    from src.backtest.historical_engine import _load_spot_csv, _load_vix_csv

Do not add new imports to this module. Phase 2 should either retire
the replay engine's BS fallback or port its import to ``common``,
at which point this shim can be deleted.
"""

from src.backtest.common import _load_spot_csv, _load_vix_csv  # noqa: F401

__all__ = ["_load_spot_csv", "_load_vix_csv"]
