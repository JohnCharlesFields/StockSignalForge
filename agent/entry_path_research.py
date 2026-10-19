"""Trailing market gates and descriptive paths for fixed offline experiments."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from market_calendar import is_trading_day
from pullback_validation import shift_session

VERSION = 'entry_path_fixed_v1'
EXPERIMENTS = ('baseline', 'market', 'distance', 'failed_level', 'hold3', 'hold5')
MAG7 = frozenset(('AAPL', 'MSFT', 'AMZN', 'GOOG', 'GOOGL', 'META', 'NVDA', 'TSLA'))


def complete_window(index, day, sessions):
    dates = [stamp.date() for stamp in index]
    if len(set(dates)) != len(dates):
        return False
    end = pd.Timestamp(day).date()
    if not is_trading_day(end) or end not in dates:
        return False
    observed = [d for d in dates if d <= end][-sessions:]
    if len(observed) != sessions or any(not is_trading_day(d) for d in observed):
        return False
    return all(shift_session(a, 1) == b for a, b in zip(observed, observed[1:]))


def complete_forward_window(index, day, sessions=15):
    start = pd.Timestamp(day).date()
    if not is_trading_day(start):
        return False
    expected = [shift_session(start, n) for n in range(sessions+1)]
    observed = [stamp.date() for stamp in index if start <= stamp.date() <= expected[-1]]
    return observed == expected


def market_states(frame):
    if frame.index.has_duplicates:
        raise ValueError('duplicate_market_dates')
    if len({d.date() for d in frame.index}) != len(frame):
        raise ValueError('duplicate_market_calendar_dates')
    frame = frame.loc[[is_trading_day(stamp.date()) for stamp in frame.index]]
    values = pd.to_numeric(frame.Close, errors='coerce').where(lambda s: np.isfinite(s) & (s > 0))
    ma50 = values.rolling(50).mean()
    slope = ma50-ma50.shift(5)
    result = {}
    for i, stamp in enumerate(frame.index):
        day = str(stamp.date())
        if day in result:
            raise ValueError('duplicate_market_calendar_dates')
        valid = complete_window(frame.index[:i+1], day, 55) and values.iloc[max(0, i-54):i+1].notna().all()
        result[day] = dict(state='up' if valid and values.iloc[i] > ma50.iloc[i] and slope.iloc[i] > 0
                           else 'down' if valid and values.iloc[i] < ma50.iloc[i] and slope.iloc[i] < 0
                           else 'neutral' if valid else 'unknown',
                           date=day)
    return result


def entry_gate(experiment, side, day, close, seed, market, decision_context=None):
    from planned_risk_research import EXPERIMENTS as PLANNED_EXPERIMENTS, planned_gate
    if experiment in PLANNED_EXPERIMENTS:
        return planned_gate(experiment, side, close, seed, decision_context)
    if experiment == 'market':
        state = (market or {}).get(day, {}).get('state', 'unknown')
        if state == 'unknown':
            return 'market_data_unknown'
        return None if state == ('up' if side == 'LONG' else 'down') else 'market_direction_rejected'
    if experiment == 'distance':
        sign = 1 if side == 'LONG' else -1
        atr = seed['atr']
        if not math.isfinite(atr) or atr <= 0:
            return 'confirmation_atr_unknown'
        level = seed['high'] if side == 'LONG' else seed['low']
        distance = sign*(close-level)/atr
        return None if 0 <= distance <= .5 else 'confirmation_distance_rejected'
    return None


def path_diagnostic(frame, trade, seed):
    stamps = {str(stamp.date()): i for i, stamp in enumerate(frame.index)}
    start, end = stamps[trade['entry_date']], stamps[trade['exit_date']]
    decision = stamps[trade['decision_date']]
    sign = 1 if trade['side'] == 'LONG' else -1
    price, atr = trade['entry_price'], seed['atr']
    level = seed['high'] if sign == 1 else seed['low']
    closes = frame.Close.iloc[start:end]
    cost = trade['cost']
    out = dict(id=trade['id'], symbol=trade['symbol'], side=trade['side'], date=seed['date'],
               period=trade['period'], net_return=trade['net_return'],
               confirmation_distance_atr=sign*(float(frame.Close.iloc[decision])-level)/atr,
               entry_gap_atr=sign*(price-float(frame.Close.iloc[start-1]))/atr,
               risk_distance_atr=abs(price-trade['stop'])/atr,
               prior_close_profit_then_loss=bool(trade['net_return'] < 0 and ((sign*(closes/price-1)-cost) > 0).any()),
               exit_reason=trade['exit_reason'], horizons={})
    for h in (1, 3, 5, 8):
        idx = start+h
        if idx >= len(frame) or not complete_window(frame.index[start:idx+1], str(frame.index[idx].date()), h+1):
            out['horizons'][str(h)] = None
            continue
        window = frame.iloc[start:idx]
        favorable = window.High.max()/price-1 if sign == 1 else 1-window.Low.min()/price
        adverse = window.Low.min()/price-1 if sign == 1 else 1-window.High.max()/price
        out['horizons'][str(h)] = dict(
            directional_open_return=sign*(float(frame.Open.iloc[idx])/price-1)-cost,
            favorable_range=float(favorable), adverse_range=float(adverse),
            scope='fixed_future_window_diagnostic_including_after_actual_exit')
    return out
