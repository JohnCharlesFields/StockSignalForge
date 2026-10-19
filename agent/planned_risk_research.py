"""Fixed, close-observable filters for offline research; never a live strategy."""
from __future__ import annotations

import math

from entry_path_research import complete_window
from technical_signal_metrics import calculate_atr

VERSION = 'planned_risk_fixed_v1'
EXPERIMENTS = ('long_risk_floor', 'short_risk_cap', 'short_rebound', 'short_extension')
COMPARISONS = (('LONG', 'long_risk_floor'), ('SHORT', 'short_risk_cap'),
               ('SHORT', 'short_rebound'), ('SHORT', 'short_extension'))


def finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def enrich_setup_features(frame, seeds):
    """Freeze rebound evidence at WATCH, using only its past three sessions."""
    atr = calculate_atr(frame, 14)
    prior_low = frame.Low.shift(1).rolling(3, min_periods=3).min()
    positions = {str(stamp.date()): i for i, stamp in enumerate(frame.index)}
    if len(positions) != len(frame):
        raise ValueError('duplicate_setup_calendar_dates')
    result = []
    for seed in seeds:
        value = None
        i = positions.get(seed['setup_date'])
        if i is not None and seed['setup_date'] <= seed['date']:
            a, low = atr.iloc[i], prior_low.iloc[i]
            if (complete_window(frame.index[:i+1], seed['setup_date'], 15)
                    and finite(a) and a > 0 and finite(low) and low > 0):
                value = float((frame.High.iloc[i]-low)/a)
        result.append({**seed, 'setup_rebound_atr': value})
    return result


def planned_gate(experiment, side, close, seed, decision_context=None):
    if experiment not in EXPERIMENTS:
        raise ValueError('invalid_planned_risk_experiment')
    if (experiment == 'long_risk_floor') != (side == 'LONG'):
        return None
    a = seed.get('atr')
    if not finite(close) or close <= 0 or not finite(a) or a <= 0:
        return 'planned_risk_data_unknown'
    if experiment in ('long_risk_floor', 'short_risk_cap'):
        stop = seed.get('low' if side == 'LONG' else 'high')
        if not finite(stop) or stop <= 0:
            return 'planned_risk_data_unknown'
        risk = (close-stop)/a if side == 'LONG' else (stop-close)/a
        passed = risk >= 1 if side == 'LONG' else 0 < risk <= 2
        return None if passed else 'planned_risk_rejected'
    if experiment == 'short_rebound':
        rebound = seed.get('setup_rebound_atr')
        if not finite(rebound):
            return 'setup_rebound_unknown'
        return None if rebound >= 1 else 'setup_rebound_rejected'
    ma20 = decision_context.get('ma20') if decision_context is not None else None
    if not finite(ma20) or ma20 <= 0:
        return 'decision_ma20_unknown'
    return None if (ma20-close)/a <= 2 else 'short_extension_rejected'
