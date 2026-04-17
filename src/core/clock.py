"""Market clock for Indian exchanges — trading hours, expiry dates, holidays."""

import calendar
from datetime import date, datetime, time, timedelta

import pytz

from src.core.constants import (
    EXPIRY_SCHEDULE,
    MARKET_CLOSE,
    MARKET_OPEN,
    MIS_SQUARE_OFF_TIME,
    NSE_HOLIDAYS_2026,
    PRE_OPEN_END,
    PRE_OPEN_START,
)

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    """IST-aware current time. Use everywhere instead of datetime.now().

    Apr 17 fix: naive datetimes from datetime.now() pick up the host TZ,
    which caused expiry-day comparisons to silently shift +/- 5h30m
    when the box was in UTC vs IST.
    """
    return datetime.now(IST)


class MarketClock:
    """Provides market time awareness for the trading system."""

    def now(self) -> datetime:
        return datetime.now(IST)

    def today(self) -> date:
        return self.now().date()

    def current_time(self) -> time:
        return self.now().time()

    def is_market_open(self) -> bool:
        now = self.now()
        if self.is_trading_holiday(now.date()):
            return False
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def is_pre_open(self) -> bool:
        now = self.now()
        if self.is_trading_holiday(now.date()):
            return False
        if now.weekday() >= 5:
            return False
        return PRE_OPEN_START <= now.time() <= PRE_OPEN_END

    def should_square_off_mis(self) -> bool:
        return self.is_market_open() and self.current_time() >= MIS_SQUARE_OFF_TIME

    def time_to_close(self) -> timedelta:
        now = self.now()
        close_dt = now.replace(
            hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
        )
        if now >= close_dt:
            return timedelta(0)
        return close_dt - now

    def is_trading_holiday(self, dt: date) -> bool:
        if dt.weekday() >= 5:
            return True
        year = dt.year
        holidays = self._get_holidays(year)
        return dt in holidays

    def is_expiry_day(self, underlying: str, dt: date | None = None) -> bool:
        dt = dt or self.today()
        schedule = EXPIRY_SCHEDULE.get(underlying)
        if not schedule:
            return False

        # Check weekly expiry
        if schedule["has_weekly"] and schedule["weekly_day"] is not None:
            if dt.weekday() == schedule["weekly_day"]:
                if not self.is_trading_holiday(dt):
                    return True

        # Check monthly expiry (last Tuesday)
        last_tuesday = self._last_weekday_of_month(dt.year, dt.month, 1)  # 1 = Tuesday
        if dt == last_tuesday:
            return True

        return False

    def next_expiry(self, underlying: str, dt: date | None = None) -> date:
        dt = dt or self.today()
        schedule = EXPIRY_SCHEDULE.get(underlying)
        if not schedule:
            raise ValueError(f"Unknown underlying: {underlying}")

        check_date = dt
        for _ in range(60):  # Look ahead max 60 days
            if self.is_expiry_day(underlying, check_date) and check_date >= dt:
                if not self.is_trading_holiday(check_date):
                    return check_date
                # If expiry day is a holiday, expiry moves to previous trading day
                return self._previous_trading_day(check_date)
            check_date += timedelta(days=1)

        # Fallback: return last Tuesday of current month
        return self._last_weekday_of_month(dt.year, dt.month, 1)

    def next_monthly_expiry(self, dt: date | None = None) -> date:
        dt = dt or self.today()
        last_tue = self._last_weekday_of_month(dt.year, dt.month, 1)
        if last_tue >= dt:
            if self.is_trading_holiday(last_tue):
                return self._previous_trading_day(last_tue)
            return last_tue
        # Move to next month
        if dt.month == 12:
            next_month_date = date(dt.year + 1, 1, 1)
        else:
            next_month_date = date(dt.year, dt.month + 1, 1)
        last_tue = self._last_weekday_of_month(next_month_date.year, next_month_date.month, 1)
        if self.is_trading_holiday(last_tue):
            return self._previous_trading_day(last_tue)
        return last_tue

    def time_to_expiry_years(self, expiry: date, dt: date | None = None) -> float:
        """Time to expiry in years (for Black-Scholes). Uses calendar days / 365."""
        dt = dt or self.today()
        days = (expiry - dt).days
        if days <= 0:
            # On expiry day, use fraction of day remaining
            now = self.now()
            close_dt = now.replace(
                hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
            )
            remaining_seconds = max(0, (close_dt - now).total_seconds())
            return remaining_seconds / (365 * 24 * 3600)
        return days / 365

    def _previous_trading_day(self, dt: date) -> date:
        prev = dt - timedelta(days=1)
        while self.is_trading_holiday(prev):
            prev -= timedelta(days=1)
        return prev

    def _last_weekday_of_month(self, year: int, month: int, weekday: int) -> date:
        """Get the last occurrence of a weekday in a month.
        weekday: 0=Monday, 1=Tuesday, ..., 6=Sunday
        """
        last_day = calendar.monthrange(year, month)[1]
        dt = date(year, month, last_day)
        while dt.weekday() != weekday:
            dt -= timedelta(days=1)
        return dt

    def _get_holidays(self, year: int) -> set[date]:
        if year == 2026:
            return {date(2026, m, d) for m, d in NSE_HOLIDAYS_2026}
        # For other years, return empty (should be updated annually)
        return set()
