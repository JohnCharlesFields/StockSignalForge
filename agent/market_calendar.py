"""Zero-dependency US (NYSE) trading calendar.

We deliberately avoid pandas_market_calendars / exchange_calendars (not in the
image). NYSE holiday rules are deterministic, so we encode them directly:

  - Weekends are closed.
  - Fixed holidays observed on the nearest weekday (Sat -> Fri, Sun -> Mon).
  - Good Friday (Easter - 2 days; Easter via the anonymous Gregorian computus).
  - Floating Monday/Thursday holidays (MLK, Presidents, Memorial, Labor,
    Thanksgiving).
  - Juneteenth is a market holiday from 2022 onward.

The primary consumer is the daily batch: when the most-recent US session is the
same one we already synced (weekend/holiday gap), there is no new price data, so
we skip the market-data sync and only refresh news. The batch runs at 09:30
Asia/Shanghai = ~21:30 ET of the *previous* US calendar day, i.e. after that
day's 16:00 ET close -- so ``most_recent_session`` resolves to that session.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Official NYSE calendar, published 2025-12-23 (cash-equity close, not options).
# https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/
_EARLY_CLOSE_DAYS = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26),
                     date(2028, 7, 3), date(2028, 11, 24)}


def session_close_et(d: date) -> datetime:
    """Cash-equity regular close; published early-close coverage: 2026..2028."""
    hour = 13 if d in _EARLY_CLOSE_DAYS else 16
    return datetime.combine(d, time(hour), tzinfo=_ET)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month``."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d: date) -> date:
    """Saturday holiday -> observed Friday; Sunday -> observed Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    """Full-day NYSE market closures for ``year`` (not early-close days)."""
    hols = {
        _observed(date(year, 1, 1)),            # New Year's Day
        _nth_weekday(year, 1, 0, 3),            # MLK Jr Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),            # Washington's Birthday (3rd Mon Feb)
        _easter(year) - timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),              # Memorial Day (last Mon May)
        _observed(date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),          # Christmas
    }
    if year >= 2022:
        hols.add(_observed(date(year, 6, 19)))  # Juneteenth
    return hols


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in nyse_holidays(d.year)


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def most_recent_session(now: Optional[datetime] = None) -> date:
    """The most recently *completed* regular US session as of ``now``.

    A session counts as complete at its scheduled close (13:00 on early closes).
    """
    now_et = (now or datetime.now(tz=_ET)).astimezone(_ET)
    today = now_et.date()
    after_close = now_et >= session_close_et(today)
    if is_trading_day(today) and after_close:
        return today
    return previous_trading_day(today)


def trading_days_back(n: int, end: Optional[date] = None) -> list[date]:
    """The ``n`` trading days ending at (and including) the session at ``end``.

    Returned oldest-first. ``end`` defaults to ``most_recent_session()``.
    """
    cur = end or most_recent_session()
    if not is_trading_day(cur):
        cur = previous_trading_day(cur)
    out = [cur]
    while len(out) < max(1, int(n)):
        cur = previous_trading_day(cur)
        out.append(cur)
    return list(reversed(out))


def market_status(now: Optional[datetime] = None) -> dict:
    """Diagnostic snapshot used by the daily batch + status endpoints."""
    now_et = (now or datetime.now(tz=_ET)).astimezone(_ET)
    session = most_recent_session(now_et)
    return {
        "now_et": now_et.isoformat(),
        "today_et": now_et.date().isoformat(),
        "today_is_trading_day": is_trading_day(now_et.date()),
        "most_recent_session": session.isoformat(),
        "previous_session": previous_trading_day(session).isoformat(),
        "scheduled_close_et": session_close_et(now_et.date()).isoformat() if is_trading_day(now_et.date()) else None,
        "early_close": now_et.date() in _EARLY_CLOSE_DAYS,
        "early_close_calendar_verified": 2026 <= now_et.year <= 2028,
    }
