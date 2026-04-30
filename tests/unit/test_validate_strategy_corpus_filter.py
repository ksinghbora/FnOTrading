"""Tests for the Apr 30 2026 SEBI corpus-split filter.

The 227-day GDFL corpus (Sep 2024 → Jul 2025) straddles the Nov 20 2024
SEBI regime change: NIFTY lot 25→75, options-sell STT 0.0625%→0.1%,
BANKNIFTY weekly expiry discontinued. The current backtest engine
uses ``LOT_SIZES["NIFTY"] = 75`` for ALL dates — so pre-break dates
in the corpus simulate at ~3× the actual lot size, distorting P&L
magnitude correspondingly.

``filter_corpus_window`` lets the operator clip the corpus to the
post-break (or pre-break) regime so the harness only sees days for
which the constant is correct. Date-aware lot-size + STT lookup is
tracked as a deeper Indian-calibration follow-up.

These tests pin the filter contract so a future refactor can't
quietly silently change which days pass through.
"""
from __future__ import annotations

from datetime import date


# Pull the helper. Importing scripts/ as a module relies on the script
# being syntactically clean — a smoke check on top of the unit tests.
import sys
from pathlib import Path
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _import_filter():
    from scripts.validate_strategy import filter_corpus_window
    return filter_corpus_window


# ── Empty / no-op cases ─────────────────────────────────────────────


def test_no_filter_returns_input_unchanged():
    """Both bounds None → identity. The validator's wrapper short-
    circuits this path so the helper isn't actually called, but the
    helper itself must still behave correctly on the boundary."""
    f = _import_filter()
    days = [date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 1)]
    out = f(days, cut_from=None, cut_to=None)
    assert out == days


def test_empty_input_returns_empty():
    f = _import_filter()
    assert f([], cut_from=date(2024, 11, 20), cut_to=None) == []
    assert f([], cut_from=None, cut_to=date(2024, 12, 31)) == []


# ── SEBI use case: validate post-break only ────────────────────────


def test_corpus_from_keeps_dates_on_or_after_cutoff():
    """The recommended SEBI cutoff is 2024-11-20. Filtering with
    cut_from=2024-11-20 must KEEP that date and everything after,
    DROP everything before."""
    f = _import_filter()
    days = [
        date(2024, 11, 18),  # pre-break — drop
        date(2024, 11, 19),  # pre-break — drop (Nov 20 is the cutoff)
        date(2024, 11, 20),  # cutoff day — keep
        date(2024, 11, 21),  # post-break — keep
        date(2024, 12, 1),   # post-break — keep
    ]
    out = f(days, cut_from=date(2024, 11, 20), cut_to=None)
    assert out == [date(2024, 11, 20), date(2024, 11, 21), date(2024, 12, 1)]


# ── Counter-test use case: validate pre-break only ─────────────────


def test_corpus_to_drops_dates_on_or_after_cutoff():
    """``cut_to`` is half-open (exclusive). With cut_to=2024-11-20 the
    filter keeps everything strictly BEFORE Nov 20."""
    f = _import_filter()
    days = [
        date(2024, 11, 18),  # pre-break — keep
        date(2024, 11, 19),  # pre-break — keep
        date(2024, 11, 20),  # cutoff — drop (half-open)
        date(2024, 11, 21),  # post-break — drop
    ]
    out = f(days, cut_from=None, cut_to=date(2024, 11, 20))
    assert out == [date(2024, 11, 18), date(2024, 11, 19)]


# ── Both bounds: window-clip use case ──────────────────────────────


def test_both_bounds_create_half_open_window():
    """[cut_from, cut_to) — keep on or after cut_from AND strictly
    before cut_to. Adjacent ranges with the same boundary date won't
    double-count."""
    f = _import_filter()
    days = [
        date(2024, 11, 1),
        date(2024, 11, 15),
        date(2024, 11, 20),
        date(2024, 12, 1),
        date(2024, 12, 15),
        date(2025, 1, 1),
    ]
    out = f(
        days,
        cut_from=date(2024, 11, 15),
        cut_to=date(2024, 12, 15),
    )
    assert out == [
        date(2024, 11, 15),
        date(2024, 11, 20),
        date(2024, 12, 1),
    ]


def test_no_overlap_between_pre_and_post_filters_with_same_cutoff():
    """The half-open semantics ensure that running the SAME corpus
    through ``cut_to=X`` and ``cut_from=X`` produces two disjoint
    sets that union to the original. Important because a user might
    do two separate validations (pre vs post SEBI) and expect the
    sums to be coherent."""
    f = _import_filter()
    days = [
        date(2024, 11, 1),
        date(2024, 11, 15),
        date(2024, 11, 20),
        date(2024, 12, 1),
    ]
    cutoff = date(2024, 11, 20)
    pre = f(days, cut_from=None, cut_to=cutoff)
    post = f(days, cut_from=cutoff, cut_to=None)
    # Disjoint
    assert set(pre).isdisjoint(set(post))
    # Union covers the original
    assert sorted(pre + post) == days


# ── Type / order preservation ──────────────────────────────────────


def test_preserves_input_order():
    """Even if the input list is unsorted (it shouldn't be — the
    validator passes sorted dates — but be defensive), the helper
    keeps input order on the output."""
    f = _import_filter()
    days = [date(2024, 12, 1), date(2024, 11, 15), date(2024, 11, 25)]
    out = f(days, cut_from=date(2024, 11, 20), cut_to=None)
    # Nov 15 dropped; the others kept IN ORDER (not sorted by helper).
    assert out == [date(2024, 12, 1), date(2024, 11, 25)]


def test_returns_list_not_generator():
    """Materialise to list — callers index into the result."""
    f = _import_filter()
    out = f([date(2024, 11, 20)], cut_from=date(2024, 11, 1), cut_to=None)
    assert isinstance(out, list)


# ── Empty result is allowed (the caller surfaces its own error) ────


def test_can_filter_to_zero_days():
    """If the user picks bounds with no overlap to the corpus, the
    helper returns []. The CLI wrapper logs an error and aborts on
    empty combined; the helper itself doesn't second-guess."""
    f = _import_filter()
    days = [date(2024, 11, 1), date(2024, 11, 15)]
    out = f(days, cut_from=date(2025, 1, 1), cut_to=None)
    assert out == []
