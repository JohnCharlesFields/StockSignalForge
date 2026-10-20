"""Local, dated split evidence and conservative raw-price replay safeguards."""
from __future__ import annotations

from functools import wraps
import inspect
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from market_calendar import session_close_et

VERSION = 'corporate_action_units_v1'
MAX_UNVERIFIED_RATIO = 1.8  # Quality alarm, not an entry/exit strategy threshold.


class ActionDataUnknown(ValueError):
    pass


def action_records(records=None):
    if records is not None:
        return records
    path = Path(__file__).resolve().parent/'config/corporate_actions_verified.json'
    return json.loads(path.read_text(encoding='utf-8'))['actions']


def evidence_digest():
    path = Path(__file__).resolve().parent/'config/corporate_actions_verified.json'
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def split_ratio(symbol, day, records):
    ratio = 1.
    for item in records:
        if str(item.get('symbol', '')).upper() != symbol.upper() or item.get('effective_date') != day:
            continue
        try:
            known = pd.Timestamp(item.get('known_at'))
            opened = pd.Timestamp(session_close_et(pd.Timestamp(day).date())).normalize()+pd.Timedelta(hours=9, minutes=30)
            value = float(item['ratio'])
            valid = (known.tzinfo is not None and known <= opened and item.get('kind') == 'split'
                     and str(item.get('source_url', '')).startswith('https://') and math.isfinite(value) and value > 0)
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ActionDataUnknown('unverified_action:'+symbol+':'+day)
        ratio *= value
    return ratio


def suspicious_ratio(current, previous, factor=1.):
    if not all(math.isfinite(float(v)) and v > 0 for v in (current, previous, factor)):
        return True
    value = current*factor/previous
    return value > MAX_UNVERIFIED_RATIO or value < 1/MAX_UNVERIFIED_RATIO


def shares_between(symbol, entry_day, exit_day, records=None):
    ratio = 1.
    for item in action_records(records):
        day = item.get('effective_date', '')
        if str(item.get('symbol', '')).upper() == symbol.upper() and entry_day < day <= exit_day:
            ratio *= split_ratio(symbol, day, [item])
    return ratio


def prepare_history(symbol, frame, records=None, *, check_prices=True):
    """Forward unit conversion leaves every pre-action prefix byte-equivalent."""
    records = action_records(records)
    out = frame.copy()
    factors, cumulative, previous, unknown = {}, 1., None, None
    for i, (stamp, bar) in enumerate(frame.iterrows()):
        day = str(stamp.date())
        try:
            factor = split_ratio(symbol, day, records)
        except ActionDataUnknown as exc:
            unknown = str(exc)
            out = out.iloc[:i].copy()
            break
        if check_prices and previous is not None and (suspicious_ratio(float(bar.Open), previous, factor)
                                     or suspicious_ratio(float(bar.Close), previous, factor)):
            unknown = 'unverified_price_units:'+symbol+':'+day
            out = out.iloc[:i].copy()
            break
        cumulative *= factor
        factors[day] = cumulative
        previous = float(bar.Close)
    scale = pd.Series([factors[str(stamp.date())] for stamp in out.index], index=out.index)
    out[['Open','High','Low','Close']] = out[['Open','High','Low','Close']].mul(scale, axis=0)
    out['Volume'] = out.Volume/scale
    out.attrs.update(frame.attrs)
    out.attrs['corporate_action_basis'] = VERSION
    if unknown:
        out.attrs['invalid_bar_at'] = unknown
    return out, factors, unknown


def action_aware_replay(function):
    """Adapt existing single-symbol engines, preserving raw chart/quote units."""
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(symbol, frame, *args, corporate_actions=None, **kwargs):
        if frame is None or frame.attrs.get('corporate_action_basis') == VERSION:
            return function(symbol, frame, *args, **kwargs)
        if not {'Open','High','Low','Close','Volume'} <= set(frame):
            return function(symbol, frame, *args, **kwargs)
        if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates
                or not frame.index.is_monotonic_increasing):
            return function(symbol, frame, *args, **kwargs)
        normalized, factors, unknown = prepare_history(symbol, frame, corporate_actions)
        bound = signature.bind(symbol, normalized, *args, **kwargs)
        if any(value != 1 for value in factors.values()):
            if 'context' in bound.arguments:
                bound.arguments['context'] = None
            if bound.arguments.get('opportunities') is not None:
                seeds = []
                for seed in bound.arguments['opportunities']:
                    s = dict(seed)
                    confirm = factors.get(s['date'], 1.)
                    s.update(high=s['high']*confirm, low=s['low']*confirm, atr=s['atr']*confirm)
                    seeds.append(s)
                bound.arguments['opportunities'] = seeds
        result = function(*bound.args, **bound.kwargs)
        for marker in result.get('markers', []):
            if marker.get('price') is not None:
                marker['price'] = round(marker['price']/factors.get(marker['time'], 1.), 4)
        for trade in result.get('transactions', []):
            entry = factors.get(trade['entry_date'], 1.)
            exit_factor = factors.get(trade['exit_date'], 1.)
            trade['entry_price'] /= entry
            trade['exit_price'] /= exit_factor
            for key in ('stop', 'target'):
                if key in trade:
                    trade[key] /= entry
            if exit_factor != entry:
                trade['share_multiplier'] = exit_factor/entry
                trade['return_basis'] = 'split_accounted_entry_notional'
        result['corporate_action_policy'] = VERSION
        if unknown:
            result.update(status='unavailable', reason=unknown, outcome_status='data_unknown',
                          validated_profit_probability=None)
        return result
    return wrapped
