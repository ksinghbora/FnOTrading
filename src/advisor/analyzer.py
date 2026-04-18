"""Claude API integration — generate DayBias and Advisory from trading data."""

from __future__ import annotations

import json
import logging
import re

from anthropic import AsyncAnthropic

from src.advisor.models import Advisory, DayBias, ExternalContext, KeyLevels, TodayData
from src.config import Settings
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


# ── System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert Indian F&O (Futures & Options) trading advisor. You advise an automated NIFTY options system on NSE.

Your job: analyze external context (news, global markets, flows) that the rule-based system CANNOT see, and provide signal adjustments. Think step by step before concluding.

## The System You're Advising

**Portfolio Strategy** — two independent legs running simultaneously:

1. **Premium leg**: Sells options to collect theta (time decay).
   - Uses Iron Condor when VIX ≥ 12 (hedged — defined max loss via wings).
   - Uses Short Strangle when VIX < 12 (unhedged — unlimited risk).
   - Entry: signal score 0-100, threshold 60.

2. **Trend leg**: Buys debit spreads on confirmed breakouts.
   - Profits from strong directional moves.
   - Entry: independent score 0-100, threshold 60.

## How These Strategies Actually Work (reason from mechanics, not rules of thumb)

**Premium selling P&L mechanics:**
- Revenue = premium collected at entry (higher VIX → richer premiums → more revenue)
- Cost = adverse spot movement beyond break-even
- Iron condor: max loss is capped by wings regardless of how far spot moves
- Key risk: gap moves that blow past short strikes before stop-loss triggers
- Theta decay accelerates as expiry approaches (especially last 2 days)
- IV crush on expiry day reduces option prices → benefits sellers

**Trend following P&L mechanics:**
- Debit spread: pays upfront, profits if spot moves in predicted direction
- Needs genuine directional conviction — fails in choppy/range-bound markets
- Spread structure limits both max profit and max loss
- Works best when there's a clear catalyst driving sustained moves

**What affects each leg differently:**
- A factor can be positive for one leg and negative for the other
- Example: event risk increases gap probability (bad for premium) but also increases breakout probability (good for trend)
- Analyze each leg independently based on HOW the factor affects its specific P&L mechanics

## Market Context (facts, not opinions)

- NIFTY weekly expiry: Tuesday. Lot size: 75.
- Market hours: 9:15-15:30 IST.
- India is a net crude oil importer — rising oil increases costs across the economy.
- FII flows: Foreign institutional investors move large capital. Net selling = risk-off. Net buying = risk-on.
- DII flows: Domestic institutions often provide counter-support during FII selling.
- NIFTY correlates with S&P 500 / NASDAQ overnight moves.
- GIFT Nifty (pre-market futures) is the best predictor of NIFTY opening gap direction and magnitude.
- DXY (dollar index) strengthening → emerging market FII outflows.

## Your Reasoning Process

For each signal adjustment, think through:
1. **What is the specific external factor?** (e.g., "crude oil up 5% on Iran tensions")
2. **How does it mechanically affect this leg's P&L?** (e.g., "increases probability of gap-down → premium leg at risk of stop-loss" vs "creates directional conviction → trend leg benefits")
3. **Is this already priced in?** (e.g., if VIX is already 26, the market already reflects fear)
4. **What's the magnitude?** (e.g., routine FII selling of -2000cr vs extreme -10000cr)
5. **What's my confidence?** (only high if the causal chain is clear and specific)

## Tunable Parameters
- `signal_threshold` (60): Min score to enter. Higher = more selective.
- `strangle_vix_max` (12.0): Strangle only below this VIX (above = Iron Condor with wings).
- `premium_stop_loss_pct` (30.0): Exit premium leg at X% loss.
- `ic_stop_loss_pct` (40.0): Exit IC at X% loss.
- `gamma_exit_threshold` (60.0): Tighten stops when gamma exposure exceeds this.
- `sizing_multiplier` (0.5x to 1.5x): Scale position size.

## Output Format

Return ONLY valid JSON:
{
  "risk_level": "LOW|MEDIUM|HIGH|EXTREME",
  "mode_bias": "iron_condor|strangle|skip_premium|no_opinion",
  "sizing_multiplier": 1.0,
  "premium_score_adj": 0,
  "premium_confidence": 0.0,
  "trend_score_adj": 0,
  "trend_confidence": 0.0,
  "key_levels": {"support": 0, "resistance": 0, "max_pain": 0},
  "reasoning": "Step-by-step analysis of how each external factor affects each leg",
  "confidence": 0.0,
  "today_summary": "...",
  "parameter_suggestions": []
}

## Rules
1. Output confidence 0.0 if you have no significant edge. MOST DAYS you should have NO opinion — the rule-based system handles routine well. Only speak when external context adds genuine insight.
2. Analyze premium and trend legs INDEPENDENTLY. A factor that hurts one may help the other.
3. Think about what's already priced in. If VIX is 25, the market already knows there's fear. Your edge is in specific catalysts the VIX level alone doesn't capture.
4. Adjustments bounded to ±15. Justify every non-zero adjustment with a specific causal chain (factor → mechanism → P&L impact).
5. sizing_multiplier: 0.5-1.5 range. Stop losses: 20-80% range.
6. parameter_suggestions: only with HIGH confidence. Format: [{"param": "name", "current": X, "suggested": Y, "reason": "...", "confidence": 0.0-1.0}]
"""


# ── Prompt Builder ───────────────────────────────────────────────

def _build_user_message(today: TodayData, context: ExternalContext) -> str:
    """Build structured JSON user message from collected data."""
    payload = {
        "date": today.date.isoformat(),
        "today_performance": {
            "total_pnl": today.total_pnl,
            "premium_leg": {
                "pnl": today.premium_leg.pnl,
                "mode": today.premium_leg.mode,
                "trades": today.premium_leg.trades,
                "attributions": [a.model_dump() for a in today.premium_leg.attributions],
            },
            "trend_leg": {
                "pnl": today.trend_leg.pnl,
                "direction": today.trend_leg.direction,
                "trades": today.trend_leg.trades,
                "attributions": [a.model_dump() for a in today.trend_leg.attributions],
            },
            "total_trades": today.trades_count,
            "risk_events": today.risk_events,
        },
        "recent_history": [
            {"date": d.date.isoformat(), "pnl": d.pnl, "premium": d.premium_pnl, "trend": d.trend_pnl}
            for d in today.recent_history
        ],
        "current_params": today.current_params,
        "market_state": {
            "closing_spot": today.closing_spot,
            "closing_vix": today.closing_vix,
        },
        "tomorrow_context": {
            "is_trading_day": context.is_trading_day,
            "is_expiry_day": context.is_expiry_day,
            "dte": context.dte,
            "news_headlines": context.news_headlines[:10],
            "global_headlines": context.global_headlines[:8],
            "fii_dii": context.fii_dii.model_dump() if context.fii_dii else None,
            "global_markets": context.global_markets.model_dump() if context.global_markets else None,
            "events": [e.model_dump(mode="json") for e in context.events],
            "data_sources_available": context.available_sources,
        },
    }
    return json.dumps(payload, indent=2)


# ── Response Parser ──────────────────────────────────────────────

def _sanitize_json(text: str) -> str:
    """Fix common JSON issues from LLM output (e.g. +8 → 8)."""
    # Remove leading + on numbers (invalid JSON but common LLM output)
    return re.sub(r':\s*\+(\d)', r': \1', text)


def _parse_response(text: str) -> dict:
    """Parse JSON from Claude response. Handles raw JSON or markdown code blocks."""
    # Try raw JSON first
    try:
        return json.loads(_sanitize_json(text))
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(_sanitize_json(match.group(1)))
        except json.JSONDecodeError:
            pass

    logger.error(
        "could not parse Claude response as JSON",
        extra={"tag": Tag.ADVISOR, "phase": "parse", "preview": text[:200]},
    )
    return {}


def _enforce_safety_bounds(data: dict) -> dict:
    """Clamp all values to safety bounds after parsing."""
    data["sizing_multiplier"] = max(0.5, min(1.5, data.get("sizing_multiplier", 1.0)))
    data["premium_score_adj"] = max(-15, min(15, int(data.get("premium_score_adj", 0))))
    data["trend_score_adj"] = max(-15, min(15, int(data.get("trend_score_adj", 0))))
    data["premium_confidence"] = max(0.0, min(1.0, float(data.get("premium_confidence", 0.0))))
    data["trend_confidence"] = max(0.0, min(1.0, float(data.get("trend_confidence", 0.0))))
    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))

    # Validate risk_level
    if data.get("risk_level") not in ("LOW", "MEDIUM", "HIGH", "EXTREME"):
        data["risk_level"] = "LOW"

    # Validate mode_bias
    valid_modes = ("iron_condor", "strangle", "skip_premium", "no_opinion")
    if data.get("mode_bias") not in valid_modes:
        data["mode_bias"] = "no_opinion"

    return data


# ── Main Analyzer ────────────────────────────────────────────────

async def analyze_with_claude(
    today: TodayData,
    context: ExternalContext,
    settings: Settings | None = None,
) -> Advisory:
    """Call Claude API to generate advisory from today's data + external context.

    Returns an Advisory with DayBias that the confluence engine can consume.
    """
    settings = settings or Settings()

    if not settings.anthropic_api_key:
        logger.warning(
            "no ANTHROPIC_API_KEY configured, returning empty advisory",
            extra={"tag": Tag.ADVISOR, "phase": "init", "reason": "missing_api_key"},
        )
        return Advisory(
            date=today.date,
            day_bias=DayBias(date=today.date),
            today_summary="No API key configured — advisor disabled.",
        )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_message = _build_user_message(today, context)

    logger.info(
        "calling Claude advisor",
        extra={
            "tag": Tag.ADVISOR,
            "phase": "api_call",
            "model": settings.advisor_model,
            "prompt_chars": len(user_message),
        },
    )

    try:
        response = await client.messages.create(
            model=settings.advisor_model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        logger.error(
            "Claude API call failed",
            extra={"tag": Tag.ADVISOR, "phase": "api_call", "model": settings.advisor_model, "error": str(e)},
        )
        return Advisory(
            date=today.date,
            day_bias=DayBias(date=today.date),
            today_summary=f"API call failed: {e}",
        )

    # Parse response
    raw_text = response.content[0].text if response.content else ""
    parsed = _parse_response(raw_text)

    if not parsed:
        return Advisory(
            date=today.date,
            day_bias=DayBias(date=today.date),
            today_summary="Failed to parse Claude response.",
            full_reasoning=raw_text,
        )

    # Enforce safety bounds
    parsed = _enforce_safety_bounds(parsed)

    # Build DayBias
    key_levels_data = parsed.get("key_levels")
    key_levels = KeyLevels(**key_levels_data) if isinstance(key_levels_data, dict) else None

    day_bias = DayBias(
        date=today.date,
        risk_level=parsed["risk_level"],
        mode_bias=parsed["mode_bias"],
        sizing_multiplier=parsed["sizing_multiplier"],
        premium_score_adj=parsed["premium_score_adj"],
        premium_confidence=parsed["premium_confidence"],
        trend_score_adj=parsed["trend_score_adj"],
        trend_confidence=parsed["trend_confidence"],
        key_levels=key_levels,
        reasoning=parsed.get("reasoning", ""),
        confidence=parsed["confidence"],
    )

    # Track token usage
    usage = {}
    if response.usage:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    advisory = Advisory(
        date=today.date,
        day_bias=day_bias,
        today_summary=parsed.get("today_summary", ""),
        parameter_suggestions=[],  # Extracted from parsed data
        full_reasoning=parsed.get("reasoning", ""),
        token_usage=usage,
    )

    logger.info(
        "advisory generated",
        extra={
            "tag": Tag.ADVISOR,
            "phase": "complete",
            "risk_level": day_bias.risk_level,
            "confidence": round(day_bias.confidence, 2),
            "mode_bias": day_bias.mode_bias,
            "premium_score_adj": day_bias.premium_score_adj,
            "premium_confidence": round(day_bias.premium_confidence, 2),
            "trend_score_adj": day_bias.trend_score_adj,
            "trend_confidence": round(day_bias.trend_confidence, 2),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    )

    return advisory
