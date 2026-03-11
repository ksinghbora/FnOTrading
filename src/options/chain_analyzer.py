"""Option chain analysis — PCR, max pain, OI analysis, IV skew."""

from decimal import Decimal

from src.core.models import OptionChain, OptionChainEntry


def compute_pcr_oi(chain: OptionChain) -> float:
    """Put-Call Ratio based on Open Interest."""
    total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
    total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)
    if total_ce_oi == 0:
        return 0.0
    return total_pe_oi / total_ce_oi


def compute_pcr_volume(chain: OptionChain) -> float:
    """Put-Call Ratio based on Volume."""
    total_ce_vol = sum(e.ce.volume for e in chain.strikes if e.ce)
    total_pe_vol = sum(e.pe.volume for e in chain.strikes if e.pe)
    if total_ce_vol == 0:
        return 0.0
    return total_pe_vol / total_ce_vol


def compute_max_pain(chain: OptionChain) -> Decimal:
    """Calculate max pain strike — the strike at which option writers lose the least.

    Max pain is where total intrinsic value of all ITM options is minimized.
    """
    if not chain.strikes:
        return Decimal("0")

    min_pain: Decimal | None = None
    max_pain_strike = chain.strikes[0].strike

    for target in chain.strikes:
        total_pain = Decimal("0")
        target_strike = target.strike

        for entry in chain.strikes:
            # CE pain: if target > strike, CE is ITM
            if entry.ce and target_strike > entry.strike:
                total_pain += (target_strike - entry.strike) * entry.ce.oi

            # PE pain: if target < strike, PE is ITM
            if entry.pe and target_strike < entry.strike:
                total_pain += (entry.strike - target_strike) * entry.pe.oi

        if min_pain is None or total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = target_strike

    return max_pain_strike


def find_atm_strike(spot_price: float, strikes: list[OptionChainEntry]) -> Decimal:
    """Find the ATM strike closest to spot price."""
    if not strikes:
        return Decimal("0")
    return min(strikes, key=lambda e: abs(float(e.strike) - spot_price)).strike


def get_iv_skew(chain: OptionChain, num_strikes: int = 5) -> dict:
    """Compute IV skew around ATM.

    Returns IV for OTM puts and OTM calls around ATM to analyze skew.
    """
    atm = float(chain.atm_strike)
    otm_puts = []
    otm_calls = []

    for entry in sorted(chain.strikes, key=lambda e: float(e.strike)):
        strike = float(entry.strike)
        if strike < atm and entry.pe and entry.pe.greeks.iv > 0:
            otm_puts.append({
                "strike": strike,
                "iv": entry.pe.greeks.iv,
                "distance": atm - strike,
            })
        elif strike > atm and entry.ce and entry.ce.greeks.iv > 0:
            otm_calls.append({
                "strike": strike,
                "iv": entry.ce.greeks.iv,
                "distance": strike - atm,
            })

    return {
        "otm_puts": otm_puts[-num_strikes:],  # Nearest to ATM
        "otm_calls": otm_calls[:num_strikes],
        "atm_iv_ce": next(
            (e.ce.greeks.iv for e in chain.strikes if e.strike == chain.atm_strike and e.ce),
            0,
        ),
        "atm_iv_pe": next(
            (e.pe.greeks.iv for e in chain.strikes if e.strike == chain.atm_strike and e.pe),
            0,
        ),
    }


def get_high_oi_strikes(
    chain: OptionChain, top_n: int = 5
) -> dict[str, list[dict]]:
    """Find strikes with highest OI — potential support/resistance levels."""
    ce_oi = sorted(
        [{"strike": e.strike, "oi": e.ce.oi} for e in chain.strikes if e.ce and e.ce.oi > 0],
        key=lambda x: x["oi"],
        reverse=True,
    )[:top_n]

    pe_oi = sorted(
        [{"strike": e.strike, "oi": e.pe.oi} for e in chain.strikes if e.pe and e.pe.oi > 0],
        key=lambda x: x["oi"],
        reverse=True,
    )[:top_n]

    return {"ce_high_oi": ce_oi, "pe_high_oi": pe_oi}
