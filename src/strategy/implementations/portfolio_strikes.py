"""Strike-selection helpers for the portfolio strategy.

Extracted from portfolio_strategy.py (Apr 17 trader-analysis #13). These
were instance methods (`_find_delta_strikes`, `_find_oi_validated_strikes`,
`_find_spot_token`) that read a handful of attributes off `self` — moving
them to module-level functions with explicit arguments makes them:

  - testable without a full strategy instance,
  - reusable from counterfactual replay (`evaluate_entry` style),
  - easier to reason about (no hidden state).

The strategy class keeps thin wrapper methods for backward compatibility.
"""

from __future__ import annotations

import logging
import math
from datetime import date

logger = logging.getLogger(__name__)


def find_delta_strikes(chain, target_ce_delta: float, target_pe_delta: float):
    """Find CE and PE strikes closest to target deltas.

    Pure function — only reads from `chain`. Returns `(ce_entry, pe_entry)`
    where each is the chain entry whose option leg's delta is closest to
    the target. Returns (None, None) if the chain has no qualifying strikes.
    """
    best_ce = best_pe = None
    best_ce_diff = best_pe_diff = float("inf")

    for entry in chain.strikes:
        if entry.ce and entry.ce.greeks.delta > 0:
            diff = abs(entry.ce.greeks.delta - target_ce_delta)
            if diff < best_ce_diff:
                best_ce_diff = diff
                best_ce = entry
        if entry.pe and entry.pe.greeks.delta < 0:
            diff = abs(entry.pe.greeks.delta - target_pe_delta)
            if diff < best_pe_diff:
                best_pe_diff = diff
                best_pe = entry

    return best_ce, best_pe


def find_oi_validated_strikes(
    chain,
    target_ce_delta: float,
    target_pe_delta: float,
    *,
    vix: float,
    expiry: date | None,
    today: date,
    log_prefix: str = "",
):
    """Find strikes using OI walls + delta validation + VIX range.

    Strategy (from Anant Ladha's methodology):
    1. Find highest Call OI strike (resistance) → sell CE at or above
    2. Find highest Put OI strike (support) → sell PE at or below
    3. Validate with VIX range formula (sell outside 1 SD range)
    4. Cross-check delta is in 0.10-0.30 range (safety)
    5. Fall back to pure delta if OI data is insufficient

    Returns (ce_entry, pe_entry). The selection method is logged so the
    caller doesn't need to handle it separately.
    """
    spot = float(chain.spot_price)

    # Step 1: Find OI walls
    max_ce_oi = 0
    max_ce_oi_entry = None
    max_pe_oi = 0
    max_pe_oi_entry = None

    for entry in chain.strikes:
        if entry.ce and entry.ce.oi > max_ce_oi and float(entry.strike) > spot:
            max_ce_oi = entry.ce.oi
            max_ce_oi_entry = entry
        if entry.pe and entry.pe.oi > max_pe_oi and float(entry.strike) < spot:
            max_pe_oi = entry.pe.oi
            max_pe_oi_entry = entry

    # Step 2: VIX-based expected range (1 SD)
    dte = (expiry - today).days if expiry else 7
    dte = max(1, dte)
    if vix > 0 and spot > 0:
        expected_range = spot * vix / 100 * math.sqrt(dte / 365)
        vix_ce_boundary = spot + expected_range
        vix_pe_boundary = spot - expected_range
    else:
        vix_ce_boundary = 0
        vix_pe_boundary = 0

    # Step 3: Select CE strike — prefer OI wall if valid
    ce_entry = None
    pe_entry = None
    method = "delta"

    if max_ce_oi_entry and max_ce_oi > 0:
        ce_delta = max_ce_oi_entry.ce.greeks.delta if max_ce_oi_entry.ce else 0
        oi_strike = float(max_ce_oi_entry.strike)

        # OI wall must be:
        # - Above spot (OTM for CE)
        # - Delta in acceptable range (0.05-0.30)
        # - At or beyond VIX range boundary
        if 0.05 <= ce_delta <= 0.30:
            if vix_ce_boundary <= 0 or oi_strike >= vix_ce_boundary * 0.95:
                ce_entry = max_ce_oi_entry
                method = "oi_wall"

    if max_pe_oi_entry and max_pe_oi > 0:
        pe_delta = max_pe_oi_entry.pe.greeks.delta if max_pe_oi_entry.pe else 0
        oi_strike = float(max_pe_oi_entry.strike)

        if -0.30 <= pe_delta <= -0.05:
            if vix_pe_boundary <= 0 or oi_strike <= vix_pe_boundary * 1.05:
                pe_entry = max_pe_oi_entry
                if method == "oi_wall":
                    method = "oi_wall"
                else:
                    method = "oi_wall+delta"

    # Step 4: Fall back to delta for any missing side
    if not ce_entry or not pe_entry:
        delta_ce, delta_pe = find_delta_strikes(chain, target_ce_delta, target_pe_delta)
        if not ce_entry:
            ce_entry = delta_ce
        if not pe_entry:
            pe_entry = delta_pe
        if method == "delta":
            method = "delta"
        else:
            method = method + "+delta_fallback"

    # Step 5: Minimum premium check (0.5% of spot)
    if ce_entry and pe_entry and ce_entry.ce and pe_entry.pe:
        total_prem = float(ce_entry.ce.ltp + pe_entry.pe.ltp)
        min_prem = spot * 0.005  # 0.5% of spot
        if total_prem < min_prem:
            logger.info(
                f"{log_prefix}OI strikes premium {total_prem:.1f} < min {min_prem:.0f} "
                f"(0.5% of spot) — falling back to delta"
            )
            ce_entry, pe_entry = find_delta_strikes(chain, target_ce_delta, target_pe_delta)
            method = "delta(min_prem)"

    # Log selection details
    ce_strike = float(ce_entry.strike) if ce_entry else 0
    pe_strike = float(pe_entry.strike) if pe_entry else 0
    ce_oi = max_ce_oi_entry.ce.oi if max_ce_oi_entry and max_ce_oi_entry.ce else 0
    pe_oi = max_pe_oi_entry.pe.oi if max_pe_oi_entry and max_pe_oi_entry.pe else 0
    logger.info(
        f"{log_prefix}STRIKE SELECTION: method={method} "
        f"CE@{ce_strike:.0f} PE@{pe_strike:.0f} "
        f"OI_wall_CE@{float(max_ce_oi_entry.strike) if max_ce_oi_entry else 0:.0f}(oi={ce_oi:,}) "
        f"OI_wall_PE@{float(max_pe_oi_entry.strike) if max_pe_oi_entry else 0:.0f}(oi={pe_oi:,}) "
        f"VIX_range=[{vix_pe_boundary:.0f}-{vix_ce_boundary:.0f}] "
        f"spot={spot:.0f}"
    )

    return ce_entry, pe_entry


def find_spot_token(chain_builder, underlying: str) -> int | None:
    """Look up the spot instrument token for an underlying symbol."""
    for token, name in chain_builder._spot_tokens.items():
        if name == underlying:
            return token
    return None
