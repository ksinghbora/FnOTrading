"""Tests for the portfolio scoring functions extracted in #13.

These were inline in portfolio_strategy.py and were never directly tested —
they only got coverage incidentally via integration runs. Moving them to a
module made it natural to lock in the score boundaries here.

Note on PCR: pcr_oi=0 short-circuits the PCR scoring branch (the function
treats 0 as "missing"). Tests that aren't probing PCR set it to 0 to keep
the score arithmetic readable.
"""

from __future__ import annotations

from pathlib import Path

from src.strategy.implementations.portfolio_scoring import (
    ScoreBreakdown,
    compute_iv_rank,
    iv_rank_shadow_adj,
    load_iv_rank_baseline,
    score_premium_selling,
    score_trend_following,
    score_trend_following_breakdown,
)
from src.strategy.indicators import BreakoutSignal


def _bs(direction: str | None = "UP", strength: float = 0.6) -> BreakoutSignal:
    return BreakoutSignal(
        direction=direction, strength=strength,
        breakout_level=24500.0, morning_high=24500.0, morning_low=24400.0,
    )


class TestScorePremiumSellingNakedMode:
    """Naked-selling VIX scoring (default ic_mode=False) — conservative."""

    def test_ideal_vix_window_full_25(self):
        # PCR=0 to skip that branch; only VIX +25 and DTE +10 should score
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5,
        )
        assert score == 35
        assert any("VIX=14.0 ideal" in r for r in reasons)

    def test_high_vix_too_high_scores_only_dte(self):
        # VIX 30 → "too high" branch (no points). PCR 0 (skip). Only DTE +10.
        score, _ = score_premium_selling(
            vix=30.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5,
        )
        assert score == 10

    def test_all_gates_pass_max_in(self):
        # VIX 14 +25, range 0.2 +25, move 0.1 +25, PCR 1.0 +15, DTE 5 +10 = 100
        score, _ = score_premium_selling(
            vix=14.0, morning_range_pct=0.2, move_from_open_pct=0.1,
            pcr_oi=1.0, is_expiry_day=False, dte=5,
        )
        assert score == 100

    def test_extreme_pcr_subtracts_ten(self):
        # VIX +25, DTE +10, PCR 2.0 -10 = 25
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=2.0, is_expiry_day=False, dte=5,
        )
        assert score == 25
        assert any("extreme" in r for r in reasons)

    def test_neutral_pcr_adds_fifteen(self):
        # PCR 1.0 in 0.8-1.2 → +15
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=1.0, is_expiry_day=False, dte=5,
        )
        # +25 + 10 + 15 = 50
        assert score == 50
        assert any("neutral" in r for r in reasons)

    def test_expiry_day_penalty_applied(self):
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=True, dte=0,
        )
        # VIX +25, expiry -10, no DTE bonus on expiry = 15
        assert score == 15
        assert any("expiry_day_penalty" in r for r in reasons)


class TestScorePremiumSellingICMode:
    """ic_mode=True is more generous on VIX — wings cap downside."""

    def test_ic_mode_allows_higher_vix(self):
        # VIX 22 in IC mode: "rich premiums(IC)" +20
        score, reasons = score_premium_selling(
            vix=22.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=True,
        )
        assert score == 30  # +20 (VIX rich) + 10 (DTE)
        assert any("rich premiums(IC)" in r for r in reasons)

    def test_naked_same_vix_scores_lower(self):
        # Same VIX 22, but naked mode → "high(risky naked)" +8
        naked, _ = score_premium_selling(
            vix=22.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=False,
        )
        assert naked == 18  # +8 + 10

    def test_ic_mode_thin_premiums_below_ideal(self):
        score, reasons = score_premium_selling(
            vix=12.5, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=True,
        )
        assert score == 18  # +8 (thin) + 10 (DTE)
        assert any("thin premiums(IC)" in r for r in reasons)


class TestScoreTrendFollowing:
    def test_no_breakout_returns_zero(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction=None, strength=0.0),
            oi_confirmed=False, trend_duration_minutes=0, vix=15.0,
        )
        assert score == 0
        assert reasons == ["no breakout"]

    def test_strong_confirmed_sustained_max(self):
        score, _ = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
        )
        # +30 strong + 25 OI + 25 sustained + 10 VIX = 90
        # (Apr 18 rebalance: VIX-level reduced from +20 to make room for
        # VIX-direction without double-counting; max base now 90.)
        assert score == 90

    def test_moderate_breakout_no_oi_early(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.35),
            oi_confirmed=False, trend_duration_minutes=20, vix=12.0,
        )
        # +15 moderate + 12 sustained-early + 5 VIX-low = 32
        assert score == 32
        assert any("moderate" in r for r in reasons)
        assert any("early" in r for r in reasons)

    def test_low_vix_no_bonus(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=10.0,
        )
        # +30 + 25 + 25 + 0 = 80 (VIX < 11 contributes nothing)
        assert score == 80
        assert not any(r.startswith("VIX=") for r in reasons)


class TestScoreTrendFollowingVixDirection:
    """VIX direction confirmation (factor 5) — ported from trend-improvements,
    threshold raised 1% → 2% Apr 18 to filter Indian VIX intraday jitter."""

    def test_vix_rising_on_up_breakout_subtracts_fifteen(self):
        # Base 90, then VIX_prev triggers: 15.0 > 14.0 * 1.02 = 14.28 → rising → -15.
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.0,
        )
        assert score == 75  # 90 - 15
        assert any("contradicts UP" in r for r in reasons)

    def test_vix_stable_on_up_breakout_adds_ten(self):
        # vix=15.0, vix_prev=14.95 → 14.95 * 1.02 = 15.249 > 15.0 → not rising.
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.95,
        )
        assert score == 100  # 90 + 10
        assert any("VIX_stable/falling supports UP" in r for r in reasons)

    def test_vix_rising_on_down_breakout_adds_ten(self):
        # DOWN breakout + rising VIX = real selling, +10. Clamps to 100 max.
        score, reasons = score_trend_following(
            breakout=_bs(direction="DOWN", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.0,
        )
        assert score == 100  # 90 + 10
        assert any("confirms DOWN" in r for r in reasons)

    def test_vix_falling_on_down_breakout_subtracts_fifteen(self):
        # DOWN breakout + falling VIX = bounce risk, -15.
        # vix=15, vix_prev=15.5 → not rising → falling branch → -15.
        score, reasons = score_trend_following(
            breakout=_bs(direction="DOWN", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=15.5,
        )
        assert score == 75  # 90 - 15
        assert any("contradicts DOWN" in r for r in reasons)

    def test_vix_prev_sub_two_percent_jitter_treated_as_stable(self):
        # vix=15.0, vix_prev=14.9 → 14.9 * 1.02 = 15.198 > 15.0 → not rising.
        # The 2% threshold filters India-VIX intraday jitter (~0.8% on quiet days).
        # Old 1% threshold would have ALSO treated this as stable, so this test
        # only proves the lower bound. See test_vix_prev_one_to_two_percent_now_stable
        # for the new behavior at the old "rising" range.
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.9,
        )
        assert score == 100  # 90 + 10 (treated as stable)
        assert any("supports UP" in r for r in reasons)

    def test_vix_prev_one_to_two_percent_now_stable(self):
        # NEW Apr 18 behavior: 1.5% jump now treated as stable (was rising).
        # vix=15.0, vix_prev=14.78 → diff = 0.22/14.78 = 1.49% < 2% threshold.
        # Old 1% threshold would have flagged this as rising (-15); new
        # 2% threshold treats it as noise and gives +10.
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.78,
        )
        assert score == 100  # 90 + 10 — would have been 75 under old threshold
        assert any("supports UP" in r for r in reasons)

    def test_vix_prev_zero_skips_branch(self):
        # Default vix_prev=0.0 → branch skipped (back-compat with old callers).
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=0.0,
        )
        assert score == 90  # base only
        assert not any("VIX_rising" in r or "VIX_stable" in r for r in reasons)


class TestScoreTrendFollowingBankNifty:
    """BankNifty sector confirmation (factor 6).

    Apr 18 rebalance: asymmetry inverted from +10/-15 to +5/-20. BN follows
    NIFTY ~80% of the time unconditionally so confirmation is the common
    weak-info case; divergence is the rare high-info case.
    """

    def test_banknifty_confirming_adds_five(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            banknifty_confirming=True,
        )
        assert score == 95  # 90 + 5
        assert any("BankNifty confirming" in r for r in reasons)

    def test_banknifty_diverging_subtracts_twenty(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            banknifty_confirming=False,
        )
        assert score == 70  # 90 - 20
        assert any("diverging" in r for r in reasons)

    def test_banknifty_unknown_no_adjustment(self):
        # None means "not yet built / unavailable" — must not penalise.
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            banknifty_confirming=None,
        )
        assert score == 90  # base unchanged
        assert not any("BankNifty" in r for r in reasons)


class TestScoreTrendFollowingClamp:
    """Score is clamped to [0, 100] — promise made, promise kept."""

    def test_clamped_at_100_when_factors_aligned(self):
        # Strong + OI + sustained + VIX-supports + VIX-direction-supports +
        # BN-confirming = 30+25+25+10+10+5 = 105 → clamps to 100.
        score, _ = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.95, banknifty_confirming=True,
        )
        assert score == 100

    def test_clamped_at_zero_when_factors_oppose(self):
        # Moderate breakout (15) only, then VIX-rising contradicts (-15)
        # and BN diverging (-20) → 15-15-20 = -20 → clamps to 0.
        score, _ = score_trend_following(
            breakout=_bs(direction="UP", strength=0.35),
            oi_confirmed=False, trend_duration_minutes=0, vix=10.0,
            vix_prev=9.0, banknifty_confirming=False,
        )
        assert score == 0


class TestScoreTrendFollowingBreakdown:
    """Per-factor breakdown variant — used by Phase A decision logging.

    Locks the invariant: when clamp_hit=False, sum(f1..f6) == score. When
    clamp_hit=True, the per-factor fields hold the unclamped contributions
    and score holds the [0, 100]-clamped projection. The decision log uses
    this to attribute blocked entries to specific factors during the 30-day
    shadow window — see memory/score_validation_plan.md.
    """

    def test_all_zero_when_no_breakout(self):
        bd = score_trend_following_breakdown(
            breakout=_bs(direction=None, strength=0.0),
            oi_confirmed=True, trend_duration_minutes=60, vix=15.0,
        )
        assert bd.score == 0
        assert bd.f1_breakout == 0
        assert bd.f2_oi == 0
        assert bd.f3_duration == 0
        assert bd.f4_vix_level == 0
        assert bd.f5_vix_dir == 0
        assert bd.f6_banknifty == 0
        assert bd.clamp_hit is False
        assert bd.reasons == ["no breakout"]

    def test_individual_factor_contributions_match_branches(self):
        # Strong breakout (30) + OI (25) + sustained 45min (25) + VIX 15 (10)
        # + VIX-stable supports UP (10) + BN confirming (5) = 105 → clamps 100.
        bd = score_trend_following_breakdown(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.95, banknifty_confirming=True,
        )
        assert bd.f1_breakout == 30
        assert bd.f2_oi == 25
        assert bd.f3_duration == 25
        assert bd.f4_vix_level == 10
        assert bd.f5_vix_dir == 10
        assert bd.f6_banknifty == 5
        # Unclamped raw is 105, clamped to 100 — clamp_hit must reflect this.
        assert bd.score == 100
        assert bd.clamp_hit is True

    def test_unclamped_sum_equals_score_in_normal_range(self):
        # Strong (30) + OI (25) + early sustained 15-29min (12) + VIX low (5)
        # + no vix_prev branch (0) + BN unknown (0) = 72. No clamp.
        bd = score_trend_following_breakdown(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=20, vix=12.0,
            vix_prev=0.0, banknifty_confirming=None,
        )
        raw_sum = (
            bd.f1_breakout + bd.f2_oi + bd.f3_duration
            + bd.f4_vix_level + bd.f5_vix_dir + bd.f6_banknifty
        )
        assert bd.clamp_hit is False
        assert bd.score == raw_sum == 72

    def test_negative_clamp_sets_clamp_hit(self):
        # Moderate breakout (15) + nothing else + VIX-rising contradicts UP (-15)
        # + BN diverging (-20) = -20 → clamps 0.
        bd = score_trend_following_breakdown(
            breakout=_bs(direction="UP", strength=0.35),
            oi_confirmed=False, trend_duration_minutes=0, vix=10.0,
            vix_prev=9.0, banknifty_confirming=False,
        )
        assert bd.f1_breakout == 15
        assert bd.f5_vix_dir == -15
        assert bd.f6_banknifty == -20
        assert bd.score == 0
        assert bd.clamp_hit is True

    def test_back_compat_wrapper_returns_same_score(self):
        # The thin wrapper must project (score, reasons) identically.
        kwargs = dict(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
            vix_prev=14.95, banknifty_confirming=True,
        )
        wrapper_score, wrapper_reasons = score_trend_following(**kwargs)
        bd = score_trend_following_breakdown(**kwargs)
        assert wrapper_score == bd.score
        assert wrapper_reasons == bd.reasons


class TestDecisionLoggerPhaseA:
    """The Phase A logging columns appear in the CSV when a TREND row is logged.

    Doesn't exercise the live strategy — that's covered by
    test_portfolio_entry_gates.py and integration runs. This locks the schema
    contract: every Phase A field has a column header AND round-trips through
    csv writer/reader without loss.
    """

    def test_phase_a_columns_present_in_header(self):
        from src.strategy.decision_logger import COLUMNS
        required = [
            "breakout_strength", "oi_confirmed", "trend_duration_minutes",
            "vix_prev", "banknifty_confirming", "bn_data_available",
            "score_f1_breakout", "score_f2_oi", "score_f3_duration",
            "score_f4_vix_level", "score_f5_vix_dir", "score_f6_banknifty",
            "score_clamp_hit", "trend_signal_threshold",
        ]
        for col in required:
            assert col in COLUMNS, f"Phase A column {col!r} missing from CSV header"

    def test_trend_row_round_trips_through_csv(self, tmp_path):
        import csv as _csv
        from src.strategy.decision_logger import (
            COLUMNS, DecisionLogger, DecisionSnapshot,
        )

        dl = DecisionLogger(output_dir=tmp_path)
        snap = DecisionSnapshot(
            timestamp="2026-04-21T10:15:00+05:30",
            strategy_id="portfolio_hist", leg="TREND", decision="ENTER",
            mode="debit_spread", spot=24500.0, vix=15.0,
            rule_score=72, threshold=50,
            breakout_strength=0.62, oi_confirmed=True,
            trend_duration_minutes=45, vix_prev=14.95,
            banknifty_confirming=True, bn_data_available=True,
            score_f1_breakout=30, score_f2_oi=25, score_f3_duration=25,
            score_f4_vix_level=10, score_f5_vix_dir=10, score_f6_banknifty=5,
            score_clamp_hit=True, trend_signal_threshold=50,
        )
        dl.log(snap)
        dl.close()

        csv_path = tmp_path / "decisions_2026-04-21.csv"
        assert csv_path.exists(), "Logger should have created the daily CSV"
        with open(csv_path, newline="") as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == 1
        row = rows[0]
        # Headers match COLUMNS exactly (no drift).
        assert list(rows[0].keys()) == COLUMNS
        # Phase A round-trip checks — string-typed since CSV is untyped.
        assert row["breakout_strength"] == "0.62"
        assert row["oi_confirmed"] == "True"
        assert row["trend_duration_minutes"] == "45"
        assert row["banknifty_confirming"] == "True"
        assert row["bn_data_available"] == "True"
        assert row["score_f1_breakout"] == "30"
        assert row["score_f6_banknifty"] == "5"
        assert row["score_clamp_hit"] == "True"
        assert row["trend_signal_threshold"] == "50"

    def test_premium_row_leaves_phase_a_at_defaults(self, tmp_path):
        import csv as _csv
        from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot

        dl = DecisionLogger(output_dir=tmp_path)
        # PREMIUM row: caller doesn't fill any Phase A fields.
        dl.log(DecisionSnapshot(
            timestamp="2026-04-21T09:35:00+05:30",
            strategy_id="portfolio_hist", leg="PREMIUM", decision="ENTER",
            mode="iron_condor", spot=24500.0, vix=18.0,
            rule_score=78, threshold=60,
        ))
        dl.close()

        with open(tmp_path / "decisions_2026-04-21.csv", newline="") as f:
            row = next(_csv.DictReader(f))
        # banknifty_confirming defaults to None → CSV writes empty string.
        assert row["banknifty_confirming"] == ""
        # All numeric Phase A defaults are 0 / False.
        assert row["score_f1_breakout"] == "0"
        assert row["score_clamp_hit"] == "False"


class TestDecisionLoggerTruncateMode:
    """The truncate_per_session flag (Apr 18 2026) prevents replay re-runs
    from piling duplicate rows on the same per-day CSV.

    Concrete trigger: scripts/replay_23days.py was invoked 6× during the
    Apr 18 chain-quality investigation. Each run reopened
    decisions_2026-04-17.csv in append mode, producing 6× duplication of
    every ENTER/EXIT row. This made any per-trade analysis (win rate,
    P&L attribution, factor activation) wrong by a factor of 6.

    Live trading must keep the original append behavior — within a single
    session, we open the file at session start and append decisions all
    day. Truncate mode is only safe when the caller knows the file
    content can be discarded (replay over historical chain data).
    """

    def test_truncate_mode_overwrites_existing_file(self, tmp_path):
        """Second logger instance with truncate=True wipes the first run's rows."""
        import csv as _csv
        from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot

        # First "replay run" — writes 3 rows.
        dl1 = DecisionLogger(output_dir=tmp_path, truncate_per_session=True)
        for i in range(3):
            dl1.log(DecisionSnapshot(
                timestamp="2026-04-17T09:30:00+05:30",
                strategy_id="portfolio_replay", leg="PREMIUM",
                decision="ENTER", mode="iron_condor",
                spot=24000.0 + i, rule_score=70,
            ))
        dl1.close()

        # Second "replay run" — should TRUNCATE, then write 2 new rows.
        dl2 = DecisionLogger(output_dir=tmp_path, truncate_per_session=True)
        for i in range(2):
            dl2.log(DecisionSnapshot(
                timestamp="2026-04-17T09:30:00+05:30",
                strategy_id="portfolio_replay", leg="PREMIUM",
                decision="ENTER", mode="iron_condor",
                spot=25000.0 + i, rule_score=80,
            ))
        dl2.close()

        with open(tmp_path / "decisions_2026-04-17.csv", newline="") as f:
            rows = list(_csv.DictReader(f))
        # Only the second run's rows survive — the first 3 were truncated.
        assert len(rows) == 2
        assert all(int(float(r["spot"])) >= 25000 for r in rows)

    def test_append_mode_preserves_existing_rows(self, tmp_path):
        """Default (truncate=False) keeps prior rows — required for live."""
        import csv as _csv
        from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot

        # First write — 2 rows
        dl1 = DecisionLogger(output_dir=tmp_path)  # default: append
        for i in range(2):
            dl1.log(DecisionSnapshot(
                timestamp="2026-04-17T09:30:00+05:30",
                strategy_id="portfolio_live", leg="PREMIUM",
                decision="ENTER", mode="iron_condor", rule_score=70,
            ))
        dl1.close()

        # Second logger instance (e.g. process restart mid-day) — should append.
        dl2 = DecisionLogger(output_dir=tmp_path)
        dl2.log(DecisionSnapshot(
            timestamp="2026-04-17T13:30:00+05:30",
            strategy_id="portfolio_live", leg="PREMIUM",
            decision="EXIT", mode="iron_condor", rule_score=0,
        ))
        dl2.close()

        with open(tmp_path / "decisions_2026-04-17.csv", newline="") as f:
            rows = list(_csv.DictReader(f))
        # All 3 rows preserved (2 ENTER + 1 EXIT).
        assert len(rows) == 3
        assert sum(1 for r in rows if r["decision"] == "ENTER") == 2
        assert sum(1 for r in rows if r["decision"] == "EXIT") == 1

    def test_truncate_only_on_first_open_per_date(self, tmp_path):
        """Within a single replay run, multi-day rotation must not re-truncate.

        A replay loops through dates; the logger opens 04-17 first
        (truncate), writes rows, then 04-18 (truncate), then maybe back
        to 04-17 if the strategy emits an EXIT for a position that
        spanned days. The second open of 04-17 in the SAME logger
        instance must APPEND, not truncate — otherwise we'd lose the
        morning's rows the moment the afternoon's logic ran.
        """
        import csv as _csv
        from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot

        dl = DecisionLogger(output_dir=tmp_path, truncate_per_session=True)
        # Day 1 morning
        dl.log(DecisionSnapshot(
            timestamp="2026-04-17T09:30:00+05:30",
            strategy_id="portfolio_replay", leg="PREMIUM",
            decision="ENTER", mode="iron_condor", rule_score=70,
        ))
        # Day 2 morning
        dl.log(DecisionSnapshot(
            timestamp="2026-04-18T09:30:00+05:30",
            strategy_id="portfolio_replay", leg="PREMIUM",
            decision="ENTER", mode="iron_condor", rule_score=72,
        ))
        # Day 1 afternoon — same date as first row; must NOT wipe it.
        dl.log(DecisionSnapshot(
            timestamp="2026-04-17T15:15:00+05:30",
            strategy_id="portfolio_replay", leg="PREMIUM",
            decision="EXIT", mode="iron_condor", rule_score=0,
        ))
        dl.close()

        with open(tmp_path / "decisions_2026-04-17.csv", newline="") as f:
            day1_rows = list(_csv.DictReader(f))
        # Both the morning ENTER and afternoon EXIT are present.
        assert len(day1_rows) == 2
        assert {r["decision"] for r in day1_rows} == {"ENTER", "EXIT"}


class TestComputeIvRank:
    """IV Rank arithmetic — null-safe under degenerate baselines."""

    def test_midpoint_returns_fifty(self):
        assert compute_iv_rank(vix=15.0, vix_52w_high=20.0, vix_52w_low=10.0) == 50.0

    def test_at_low_returns_zero(self):
        assert compute_iv_rank(vix=10.0, vix_52w_high=20.0, vix_52w_low=10.0) == 0.0

    def test_at_high_returns_hundred(self):
        assert compute_iv_rank(vix=20.0, vix_52w_high=20.0, vix_52w_low=10.0) == 100.0

    def test_above_high_returns_above_hundred(self):
        # We don't clamp — caller's job to interpret. Above-52w-high is a
        # legitimate signal worth seeing.
        result = compute_iv_rank(vix=22.0, vix_52w_high=20.0, vix_52w_low=10.0)
        assert result == 120.0

    def test_zero_high_returns_none(self):
        # Baseline unavailable (e.g. CSV missing) → None, not 0.
        assert compute_iv_rank(vix=15.0, vix_52w_high=0.0, vix_52w_low=0.0) is None

    def test_inverted_range_returns_none(self):
        # Defensive — bad data shouldn't crash the strategy.
        assert compute_iv_rank(vix=15.0, vix_52w_high=10.0, vix_52w_low=20.0) is None


class TestIvRankShadowAdj:
    """Shadow-mode adjustment brackets."""

    def test_unavailable_returns_zero(self):
        adj, reason = iv_rank_shadow_adj(None)
        assert adj == 0
        assert "unavailable" in reason

    def test_below_thirty_subtracts_fifteen(self):
        adj, reason = iv_rank_shadow_adj(20.0)
        assert adj == -15
        assert "thin_premium" in reason

    def test_thirty_to_fifty_subtracts_eight(self):
        adj, reason = iv_rank_shadow_adj(40.0)
        assert adj == -8
        assert "below_avg" in reason

    def test_at_fifty_no_adjustment(self):
        adj, reason = iv_rank_shadow_adj(50.0)
        assert adj == 0
        assert "acceptable" in reason

    def test_above_fifty_no_adjustment(self):
        adj, _ = iv_rank_shadow_adj(85.0)
        assert adj == 0


class TestLoadIvRankBaseline:
    """CSV ingestion — tolerant of column variants, returns (0,0) on failure."""

    def test_missing_file_returns_zeros(self, tmp_path: Path):
        result = load_iv_rank_baseline(tmp_path / "nope.csv")
        assert result == (0.0, 0.0)

    def test_canonical_columns_date_and_close(self, tmp_path: Path):
        csv = tmp_path / "vix.csv"
        csv.write_text(
            "date,close\n"
            "2025-01-01T09:15:00,12.0\n"
            "2025-01-01T15:30:00,13.0\n"  # last tick of day → 13.0 wins
            "2025-01-02T15:30:00,18.0\n"
            "2025-01-03T15:30:00,15.0\n"
        )
        high, low = load_iv_rank_baseline(csv)
        assert high == 18.0
        assert low == 13.0

    def test_alt_columns_ts_and_vix(self, tmp_path: Path):
        # The recorder went through a "ts/vix" naming era — must still load.
        csv = tmp_path / "vix.csv"
        csv.write_text(
            "ts,vix\n"
            "2025-01-01T15:30:00,12.0\n"
            "2025-01-02T15:30:00,20.0\n"
        )
        high, low = load_iv_rank_baseline(csv)
        assert high == 20.0
        assert low == 12.0

    def test_only_last_252_days_used(self, tmp_path: Path):
        # Generate 300 days; the first 48 should be ignored.
        csv = tmp_path / "vix.csv"
        lines = ["date,close\n"]
        # Days 0-47: VIX=99 (would dominate if window honoured wrong)
        for i in range(48):
            lines.append(f"2024-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}T15:30:00,99.0\n")
        # Days 48-299: VIX in 10-20 range (these should be the only ones counted)
        for i in range(48, 300):
            lines.append(f"2025-{((i - 48) // 30) + 1:02d}-{((i - 48) % 30) + 1:02d}T15:30:00,15.0\n")
        csv.write_text("".join(lines))
        high, low = load_iv_rank_baseline(csv)
        assert high == 15.0
        assert low == 15.0  # 99.0 must not appear

    def test_unparseable_close_skipped(self, tmp_path: Path):
        csv = tmp_path / "vix.csv"
        csv.write_text(
            "date,close\n"
            "2025-01-01T15:30:00,not-a-number\n"
            "2025-01-02T15:30:00,15.0\n"
        )
        high, low = load_iv_rank_baseline(csv)
        # Bad row dropped silently; only the parseable day survives
        assert high == 15.0
        assert low == 15.0

    def test_empty_csv_returns_zeros(self, tmp_path: Path):
        csv = tmp_path / "vix.csv"
        csv.write_text("date,close\n")  # header only
        assert load_iv_rank_baseline(csv) == (0.0, 0.0)
