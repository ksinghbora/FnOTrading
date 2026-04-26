"""Constants for Indian F&O markets — lot sizes, charges, trading hours, expiry schedule."""

from datetime import time
from decimal import Decimal

# ─── Trading Hours (IST) ────────────────────────────────────────────

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_OPEN_START = time(9, 0)
PRE_OPEN_END = time(9, 8)

# Auto square-off buffer (close MIS positions before broker does)
MIS_SQUARE_OFF_TIME = time(15, 15)

# ─── Expiry Schedule ────────────────────────────────────────────────
# Post SEBI Nov 2024 circular: only 1 weekly expiry per exchange
# NSE weekly: NIFTY only (Tuesday)
# BankNifty & FinNifty: Monthly only

EXPIRY_SCHEDULE = {
    "NIFTY": {
        "weekly_day": 1,        # Tuesday (0=Monday, 1=Tuesday, ...)
        "has_weekly": True,
    },
    "BANKNIFTY": {
        "weekly_day": None,
        "has_weekly": False,    # Discontinued — monthly only
    },
    "FINNIFTY": {
        "weekly_day": None,
        "has_weekly": False,    # Discontinued — monthly only
    },
}

# Monthly expiry: last Tuesday of the month for all indices

# ─── Lot Sizes ──────────────────────────────────────────────────────

LOT_SIZES = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "FINNIFTY": 40,
    # Stock F&O lot sizes are loaded dynamically from instrument master
}

# ─── Freeze Quantities (max lots per single order) ──────────────────

FREEZE_QUANTITIES = {
    "NIFTY": 1800,       # units, not lots
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
}

# ─── Charges (effective rates) ──────────────────────────────────────

CHARGES = {
    "brokerage": {
        "per_order_cap": Decimal("20"),       # Rs 20 flat cap per executed order
        "percentage": Decimal("0.03"),        # 0.03% (whichever is lower)
    },
    "stt": {
        # Apr 25 2026 audit (Phase 3-Pre): updated to current rates per
        # Union Budget 2026 (effective April 1 2026). Prior values were
        # the pre-Oct-2024 rates and silently understated trading costs
        # in every backtest run since Oct 2024.
        # Source: Union Budget 2024 (Oct 1 2024 hike from 0.0625 → 0.1%) and
        # Union Budget 2026 (Apr 1 2026 hike from 0.1 → 0.15%).
        "options_sell_pct": Decimal("0.1"),     # 0.10% on sell side premium (Oct 1 2024 - Mar 31 2026)
        "options_sell_pct_apr2026": Decimal("0.15"),  # 0.15% from Apr 1 2026
        "options_exercise_pct": Decimal("0.125"),  # 0.125% on intrinsic value (ITM exercise) — until Mar 31 2026
        "options_exercise_pct_apr2026": Decimal("0.15"),  # 0.15% from Apr 1 2026
        "futures_sell_pct": Decimal("0.02"),    # 0.02% (Oct 1 2024 - Mar 31 2026)
        "futures_sell_pct_apr2026": Decimal("0.05"),   # 0.05% from Apr 1 2026
    },
    "transaction_charges": {
        # NSE circular 100/2024 effective Oct 1 2024:
        # Options on premium turnover: 0.0353% (was 0.05%)
        # Futures on turnover: 0.00173% (was 0.002%)
        "options_pct": Decimal("0.0353"),     # NSE transaction charge on premium turnover (post Oct 2024)
        "futures_pct": Decimal("0.00173"),    # NSE transaction charge on futures turnover (post Oct 2024)
    },
    "sebi_charges_pct": Decimal("0.0001"),   # 0.0001% of turnover
    "gst_pct": Decimal("18"),                # 18% on (brokerage + transaction + SEBI)
    "stamp_duty": {
        "options_buy_pct": Decimal("0.003"),  # 0.003% on buy side (uniform across India since 2020)
        "futures_buy_pct": Decimal("0.002"),  # 0.002% on buy side
    },
}

# ─── India VIX ─────────────────────────────────────────────────────
# Kite instrument token for India VIX index
INDIA_VIX_TOKEN = 264969

# VIX regime thresholds — calibrated to Indian VIX (2024-2026), not US/SPX bands.
# Boundaries: <13 complacency / 13-16 normal / 16-20 elevated / 20-25 stressed / >25 event.
VIX_LOW = 13.0       # Below: complacency — premium too cheap, naked sellers stop working
VIX_NORMAL = 16.0    # 13-16 = strangle ideal band
VIX_HIGH = 20.0      # 16-20 = iron condor ideal band
VIX_EXTREME = 25.0   # Above: event/crash — no new short premium positions

# ─── Risk Defaults ──────────────────────────────────────────────────

DEFAULT_RISK_LIMITS = {
    "max_day_loss": Decimal("15000"),
    "max_strategy_loss": Decimal("5000"),
    "max_total_lots": 50,
    "max_open_orders": 20,
    "max_orders_per_second": 5,
}

# ─── Risk-Free Rate ────────────────────────────────────────────────
# 91-day T-bill rate (approximate, update periodically)
RISK_FREE_RATE = 0.07  # 7%

# ─── NSE Holidays 2026 (update annually) ────────────────────────────
# Format: list of (month, day) tuples
NSE_HOLIDAYS_2026 = [
    # Source: NSE official API /api/holiday-master (fetched 2026-03-26)
    (1, 15),   # Municipal Corporation Election - Maharashtra
    (1, 26),   # Republic Day
    (2, 15),   # Mahashivratri
    (3, 3),    # Holi
    (3, 21),   # Id-Ul-Fitr (Ramadan Eid)
    (3, 26),   # Shri Ram Navami
    (3, 31),   # Shri Mahavir Jayanti
    (4, 3),    # Good Friday
    (4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    (5, 1),    # Maharashtra Day
    (5, 28),   # Bakri Id (Eid-Ul-Adha)
    (6, 26),   # Muharram
    (8, 15),   # Independence Day
    (9, 14),   # Ganesh Chaturthi
    (10, 2),   # Mahatma Gandhi Jayanti
    (10, 20),  # Dussehra
    (11, 8),   # Diwali Laxmi Pujan
    (11, 10),  # Diwali (Balipratipada)
    (11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    (12, 25),  # Christmas
]
