"""Point-in-time entry, calendar-aligned outcomes and dependence-aware metrics."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import json

import numpy as np
import pandas as pd

import cost_model
from market_calendar import is_trading_day, most_recent_session

EASTERN = ZoneInfo("America/New_York")


def next_session_open(recorded: datetime, signal_day: date) -> date:
    day = max(recorded.astimezone(EASTERN).date(), signal_day)
    while not is_trading_day(day) or datetime.combine(day, time(9, 30), EASTERN) <= recorded:
        day += timedelta(days=1)
    return day


def shift_session(day: date, n: int) -> date:
    for _ in range(n):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    return day


def known_open_baseline(frame: pd.DataFrame, signal_day: date, horizon: int) -> float | None:
    hist = frame.loc[:pd.Timestamp(signal_day)].tail(253)
    if "Open" not in hist:
        return None
    prices = pd.to_numeric(hist.Open, errors="coerce")
    values = prices.shift(-horizon) / prices - 1
    exits = pd.Series(hist.index, index=hist.index).shift(-horizon)
    expected = [pd.Timestamp(shift_session(ts.date(), horizon)) for ts in hist.index]
    values = values[exits == expected].replace([np.inf, -np.inf], np.nan).dropna()
    return float(values.mean()) if len(values) >= 60 else None


def outcome(frame, spy, signal_day: date, entry_day: date, horizon: int, adv=None):
    exit_day = shift_session(entry_day, horizon)
    sessions = pd.DatetimeIndex([pd.Timestamp(shift_session(entry_day, n)) for n in range(horizon + 1)])
    if not sessions.isin(frame.index).all() or not sessions.isin(spy.index).all():
        return None
    prices = [float(df.loc[pd.Timestamp(day), "Open"]) for df, day in
              ((frame, entry_day), (frame, exit_day), (spy, entry_day), (spy, exit_day))]
    if not all(np.isfinite(v) and v > 0 for v in prices):
        return None
    baseline = known_open_baseline(frame, signal_day, horizon)
    if baseline is None:
        return None
    entry, exit_price, market_entry, market_exit = prices
    gross = exit_price / entry - 1
    market_return = market_exit / market_entry - 1
    paired = pd.concat([frame.Close.pct_change(), spy.Close.pct_change()], axis=1, sort=True).loc[:pd.Timestamp(signal_day)].dropna().tail(60)
    beta = None
    if len(paired) >= 40 and paired.iloc[:, 1].var() > 0:
        beta = float(paired.iloc[:, 0].cov(paired.iloc[:, 1]) / paired.iloc[:, 1].var())
    cost = cost_model.equity_round_trip_cost(price=entry, avg_dollar_volume=adv)
    return {"exit_price": exit_price, "forward_return": gross, "baseline_return": baseline,
            "excess_return": gross - baseline, "net_excess": gross - baseline - cost,
            "win": int(gross - baseline - cost > 0),
            "beta_adjusted_alpha": gross - beta * market_return - cost if beta is not None else None,
            "entry_date": entry_day.isoformat(), "exit_date": exit_day.isoformat(), "net_return": gross - cost}


def resolve_recorded_forecast(row, frame, spy):
    recorded = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    day = date.fromisoformat(row["as_of_date"])
    if recorded.tzinfo is None or day != most_recent_session(recorded):
        return None
    forecast = json.loads(row.get("forecast_json") or "{}")
    if forecast.get("price_as_of") != day.isoformat():
        return None
    adv = ((forecast.get("features") or {}).get("liquidity") or {}).get("adv")
    return outcome(frame, spy, day, next_session_open(recorded, day), int(row["horizon_days"]), adv)


def block_interval(values_by_day, block_days=5, samples=1000):
    days = sorted(k for k, v in values_by_day.items() if v)
    if len(days) < max(20, 3 * block_days):
        return [None, None]
    sums = np.array([sum(values_by_day[d]) for d in days])
    counts = np.array([len(values_by_day[d]) for d in days])
    rng = np.random.default_rng(42)
    starts = rng.integers(0, len(days), (samples, int(np.ceil(len(days) / block_days))))
    indices = ((starts[:, :, None] + np.arange(block_days)) % len(days)).reshape(samples, -1)[:, :len(days)]
    estimates = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
    return np.quantile(estimates, [.025, .975]).tolist()
