"""Trailing raw setups for archival; deliberately no simulated holdings/fills."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timezone
import hashlib

import numpy as np
import pandas as pd

from entry_path_research import MAG7
from market_calendar import is_trading_day, session_close_et
from market_data_service import _flag_price_jumps
from pullback_entry_research import EntryConfig, _bars, feature_history
from pullback_validation import shift_session
from rebound_short_research import ShortEntryConfig, short_feature_history

VERSION = 'raw_confirmation_capture_v1'
MAX_HISTORY = 256


def signal_contract():
    return dict(version=VERSION, long=asdict(EntryConfig()), short=asdict(ShortEntryConfig()),
                short_exclusions=sorted(MAG7), history_limit=MAX_HISTORY,
                scope='independent_recent_setups_no_replay_position_suppression',
                entry='future_next_session_open_only', calibrated=False)


def decision_evidence(symbol, frame, signal_date):
    """Evaluate only completed prefix bars; preserve overlapping raw setups."""
    symbol = str(symbol).strip().upper()
    day = date.fromisoformat(signal_date)
    out = dict(version=VERSION, status='DATA_UNKNOWN', setups=[], new_confirmations=[],
               short_excluded=symbol in MAG7, simulation_enabled=False)
    if (not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex)
            or frame.index.tz is not None or frame.index.hasnans):
        return {**out, 'reason': 'invalid_daily_index'}
    # Cut future rows before validating values or computing any indicator.
    f = frame.loc[frame.index.date <= day].sort_index().tail(MAX_HISTORY).copy()
    if (f.empty or f.index[-1].date() != day or len(f) < 55 or f.index.has_duplicates
            or any(ts != ts.normalize() or not is_trading_day(ts.date()) for ts in f.index)):
        return {**out, 'reason': 'stale_duplicate_or_insufficient_daily_history'}
    if any(shift_session(a.date(), 1) != b.date() for a, b in zip(f.index[:-1], f.index[1:])):
        return {**out, 'reason': 'missing_trading_session'}
    clean = _bars(f)
    if len(clean) != len(f) or clean.attrs.get('invalid_bar_at'):
        return {**out, 'reason': 'invalid_ohlcv'}
    f = clean
    if _flag_price_jumps(f).attrs.get('price_jump_suspects'):
        return {**out, 'reason': 'unverified_price_jump_or_corporate_action'}
    out['prefix_sha256'] = hashlib.sha256(f.to_csv(date_format='%Y-%m-%d', float_format='%.17g').encode()).hexdigest()
    out['price_as_of'] = signal_date
    out['reference_close'] = float(f.Close.iloc[-1])
    out['history_bars'] = len(f)
    long_cfg, short_cfg = EntryConfig(), ShortEntryConfig()
    long_features = feature_history(f)
    short_features = None if symbol in MAG7 else short_feature_history(f, short_cfg)
    adv = float((f.Close*f.Volume).tail(20).mean())
    if not np.isfinite(adv) or adv <= 0:
        return {**out, 'reason': 'unverified_positive_liquidity'}
    for side, features, cfg in [('LONG', long_features, long_cfg), ('SHORT', short_features, short_cfg)]:
        if features is None:
            continue
        for i in range(max(54, len(f)-1-cfg.confirmation_sessions), len(f)):
            setup, x = f.iloc[i], features.iloc[i]
            qualifies = (x.pullback_hv_score >= cfg.setup_threshold) if side == 'LONG' else bool(x.setup)
            if not qualifies:
                continue
            record = dict(side=side, setup_date=str(f.index[i].date()), stage='WATCH',
                          high=float(setup.High), low=float(setup.Low), confirmation_date=None,
                          stop_reference=float(setup.Low if side == 'LONG' else setup.High))
            for j in range(i+1, min(i+cfg.confirmation_sessions+1, len(f))):
                bar, context = f.iloc[j], features.iloc[j]
                broken = bar.Low < setup.Low if side == 'LONG' else bar.High > setup.High
                if broken:
                    record.update(stage='INVALIDATED', ended_date=str(f.index[j].date()))
                    break
                crossed = (bar.Close > setup.High and bar.Close > bar.Open) if side == 'LONG' else (
                    bar.Close < setup.Low and bar.Close < bar.Open)
                direction = (context.trend_up and context.cycle_state != 'down_cycle') if side == 'LONG' else (
                    context.trend_down and context.cycle_state == 'down_cycle')
                if crossed and direction and bar.Volume > 0:
                    record.update(stage='CONFIRMED', confirmation_date=str(f.index[j].date()))
                    break
            out['setups'].append(record)
            if record['confirmation_date'] == signal_date:
                out['new_confirmations'].append({**record,
                    'opportunity_id': ':'.join((symbol, side, record['setup_date'], signal_date)),
                    'reference_close': out['reference_close'], 'avg_dollar_volume': adv})
    out['status'] = 'CONFIRMED' if out['new_confirmations'] else 'WATCH' if any(
        s['stage'] == 'WATCH' for s in out['setups']) else 'WAIT'
    return out


def entry_timing(signal_date, available_at):
    """Never move a missed open forward or invent a past fill."""
    if available_at.tzinfo is None:
        raise ValueError('availability_requires_timezone')
    if available_at < session_close_et(date.fromisoformat(signal_date)):
        raise ValueError('daily_signal_not_yet_complete')
    from zoneinfo import ZoneInfo
    next_day = shift_session(date.fromisoformat(signal_date), 1)
    opened = datetime.combine(next_day, time(9, 30), tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
    return dict(next_session_open_at=opened.isoformat(), available_at=available_at.isoformat(),
                entry_timing='awaiting_next_open' if available_at < opened else 'missed_next_open',
                fill_price=None, simulation_enabled=False)
