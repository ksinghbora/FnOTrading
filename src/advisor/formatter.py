"""Format advisory and audit data for Telegram delivery."""

from __future__ import annotations

from src.advisor.models import Advisory, ConfluenceAudit


def format_advisory_telegram(advisory: Advisory) -> str:
    """Format advisory as a Telegram message."""
    bias = advisory.day_bias
    lines = [
        f"Advisory for {advisory.date}",
        "",
        f"Risk: {bias.risk_level}",
        f"Confidence: {bias.confidence:.0%}",
        f"Mode: {bias.mode_bias}",
        f"Sizing: {bias.sizing_multiplier:.1f}x",
    ]

    # Score adjustments (only if significant)
    adjs = []
    if abs(bias.premium_score_adj) >= 5 and bias.premium_confidence >= 0.7:
        adjs.append(f"Premium {bias.premium_score_adj:+d} (conf={bias.premium_confidence:.0%})")
    if abs(bias.trend_score_adj) >= 5 and bias.trend_confidence >= 0.7:
        adjs.append(f"Trend {bias.trend_score_adj:+d} (conf={bias.trend_confidence:.0%})")

    if adjs:
        lines.append("")
        lines.append("Score Adjustments:")
        for a in adjs:
            lines.append(f"  {a}")

    # Key levels
    if bias.key_levels:
        kl = bias.key_levels
        if kl.support > 0 or kl.resistance > 0:
            lines.append("")
            lines.append(f"Levels: S={kl.support:.0f} R={kl.resistance:.0f} MP={kl.max_pain:.0f}")

    # Parameter suggestions
    if advisory.parameter_suggestions:
        lines.append("")
        lines.append("Parameter Suggestions:")
        for s in advisory.parameter_suggestions:
            lines.append(f"  {s.param}: {s.current} -> {s.suggested} ({s.reason})")

    # Summary
    if advisory.today_summary:
        lines.append("")
        lines.append(advisory.today_summary)

    # Reasoning
    if bias.reasoning:
        lines.append("")
        lines.append(f"Reasoning: {bias.reasoning[:500]}")

    # Token usage
    if advisory.token_usage:
        inp = advisory.token_usage.get("input_tokens", 0)
        out = advisory.token_usage.get("output_tokens", 0)
        lines.append("")
        lines.append(f"Tokens: {inp}in/{out}out")

    return "\n".join(lines)


def format_audit_telegram(audit: ConfluenceAudit) -> str:
    """Format confluence audit as a Telegram message."""
    total = audit.agree_count + audit.disagree_count
    if total == 0:
        return f"Audit {audit.date}: No confluence decisions recorded."

    lines = [
        f"Confluence Audit {audit.date}",
        "",
        f"Decisions: {len(audit.decisions)}",
        f"Agree: {audit.agree_count}  Disagree: {audit.disagree_count}",
    ]

    if audit.disagree_count > 0:
        lines.append(f"AI right: {audit.ai_right_count}  Rules right: {audit.rule_right_count}")

    lines.append(f"AI Alpha: {audit.ai_alpha:+,.0f}")

    return "\n".join(lines)


def split_telegram_message(text: str, max_length: int = 4096) -> list[str]:
    """Split message into chunks that fit Telegram's character limit."""
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        # Find last newline before limit
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return parts
