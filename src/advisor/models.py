"""Pydantic schemas for the AI advisor pipeline."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


# ── Pre-Market Advisory Output ─────────────────────────────────────

class DayBias(BaseModel):
    """AI advisor's pre-market signal — read by confluence engine during trading."""

    date: date
    risk_level: str = "LOW"  # LOW | MEDIUM | HIGH | EXTREME
    mode_bias: str = "no_opinion"  # iron_condor | strangle | skip_premium | no_opinion
    sizing_multiplier: float = Field(1.0, ge=0.5, le=1.5)

    # Per-signal adjustments (±15 max, applied only if confidence >= 0.7)
    premium_score_adj: int = Field(0, ge=-15, le=15)
    premium_confidence: float = Field(0.0, ge=0.0, le=1.0)
    trend_score_adj: int = Field(0, ge=-15, le=15)
    trend_confidence: float = Field(0.0, ge=0.0, le=1.0)

    key_levels: KeyLevels | None = None
    reasoning: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)  # overall


class KeyLevels(BaseModel):
    support: float = 0.0
    resistance: float = 0.0
    max_pain: float = 0.0


# ── Data Collector Output ──────────────────────────────────────────

class LegAttribution(BaseModel):
    """Greeks P&L decomposition for a single exit."""

    delta_pnl: float = 0.0
    gamma_pnl: float = 0.0
    theta_pnl: float = 0.0
    vega_pnl: float = 0.0
    residual: float = 0.0
    dominant: str = ""


class PremiumLegData(BaseModel):
    pnl: float = 0.0
    mode: str = "idle"
    entry_score: int = 0
    greeks_at_entry: dict = Field(default_factory=dict)
    attributions: list[LegAttribution] = Field(default_factory=list)
    trades: int = 0


class TrendLegData(BaseModel):
    pnl: float = 0.0
    direction: str = ""
    entry_score: int = 0
    attributions: list[LegAttribution] = Field(default_factory=list)
    trades: int = 0


class DayResult(BaseModel):
    date: date
    pnl: float
    premium_pnl: float = 0.0
    trend_pnl: float = 0.0


class TodayData(BaseModel):
    """All internal trading data for a single day."""

    date: date
    total_pnl: float = 0.0
    premium_leg: PremiumLegData = Field(default_factory=PremiumLegData)
    trend_leg: TrendLegData = Field(default_factory=TrendLegData)
    trades_count: int = 0
    risk_events: list[str] = Field(default_factory=list)
    recent_history: list[DayResult] = Field(default_factory=list)
    current_params: dict = Field(default_factory=dict)
    closing_spot: float = 0.0
    closing_vix: float = 0.0


# ── External Context ───────────────────────────────────────────────

class FiiDiiData(BaseModel):
    fii_net: float = 0.0  # Net FII buy/sell in crores
    dii_net: float = 0.0


class CalendarEvent(BaseModel):
    date: date
    event: str
    impact: str = "MEDIUM"  # LOW | MEDIUM | HIGH | EXTREME


class GlobalMarketData(BaseModel):
    """Overnight global market snapshot — strongest predictor of NIFTY gap."""

    sp500_close: float = 0.0
    sp500_change_pct: float = 0.0
    nasdaq_close: float = 0.0
    nasdaq_change_pct: float = 0.0
    us_vix: float = 0.0
    us_vix_change_pct: float = 0.0
    crude_oil_close: float = 0.0  # Brent
    crude_oil_change_pct: float = 0.0
    gift_nifty: float = 0.0  # Pre-market NIFTY futures
    gift_nifty_change_pct: float = 0.0  # vs NIFTY prev close
    dxy: float = 0.0  # Dollar index
    dxy_change_pct: float = 0.0
    global_sentiment: str = ""  # "risk_on" | "risk_off" | "mixed" | ""


class ExternalContext(BaseModel):
    """External market context for tomorrow."""

    is_trading_day: bool = True
    is_expiry_day: bool = False
    dte: int = 0
    news_headlines: list[str] = Field(default_factory=list)
    global_headlines: list[str] = Field(default_factory=list)
    fii_dii: FiiDiiData | None = None
    global_markets: GlobalMarketData | None = None
    events: list[CalendarEvent] = Field(default_factory=list)
    available_sources: list[str] = Field(default_factory=list)


# ── Full Advisory (Nightly Report) ─────────────────────────────────

class ParamSuggestion(BaseModel):
    param: str
    current: float
    suggested: float
    reason: str
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class Advisory(BaseModel):
    """Full nightly advisory — includes DayBias + performance analysis."""

    date: date
    day_bias: DayBias
    today_summary: str = ""
    parameter_suggestions: list[ParamSuggestion] = Field(default_factory=list)
    full_reasoning: str = ""
    token_usage: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


# ── Confluence Audit ───────────────────────────────────────────────

class DecisionRecord(BaseModel):
    """Single decision point where rule + AI were compared."""

    timestamp: datetime
    leg: str  # "premium" | "trend"
    rule_score: int
    ai_adj: int
    ai_confidence: float
    final_score: int
    action: str  # "ENTER" | "SKIP"
    applied: bool  # Was AI adjustment actually used?
    outcome_pnl: float = 0.0  # Filled in by nightly audit


class ConfluenceAudit(BaseModel):
    """End-of-day audit comparing rule-only vs confluence outcomes."""

    date: date
    decisions: list[DecisionRecord] = Field(default_factory=list)
    agree_count: int = 0
    disagree_count: int = 0
    ai_right_count: int = 0
    rule_right_count: int = 0
    pnl_with_ai: float = 0.0
    pnl_rules_only: float = 0.0
    ai_alpha: float = 0.0
