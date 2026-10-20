"""Membership observation times; backfilled file names are not PIT evidence."""
from __future__ import annotations

from datetime import date
import pandas as pd

from market_calendar import session_close_et


def observation_time(value):
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert('UTC') if stamp.tzinfo is not None and not pd.isna(stamp) else None
    except (TypeError, ValueError):
        return None


def available_snapshot(payload, as_of):
    try:
        effective = date.fromisoformat(str(payload.get('effective_date')))
        requested = date.fromisoformat(str(as_of))
    except ValueError:
        return False
    captured = observation_time(payload.get('captured_at'))
    cutoff = pd.Timestamp(session_close_et(requested)).tz_convert('UTC')
    return captured is not None and effective <= requested and captured <= cutoff
