"""Causal admission is separate from future outcome/maturity availability."""
from __future__ import annotations

from datetime import date
import math

from pullback_validation import shift_session


def prospective_plan(symbol, side, seed):
    if side not in {'LONG', 'SHORT'}:
        raise ValueError('invalid_plan_direction')
    day = date.fromisoformat(seed['date'])
    stop, adv = float(seed['low' if side == 'LONG' else 'high']), float(seed['adv'])
    if not all(math.isfinite(v) and v > 0 for v in (stop, adv)):
        raise ValueError('unknown_plan_inputs')
    return dict(id=seed['id'], symbol=symbol, side=side, decision_date=str(day),
                entry_date=str(shift_session(day, 1)), stop=stop, adv=adv)


def opportunity_record(symbol, side, seed, admission_status, outcome_status):
    return dict(id=seed['id'], symbol=symbol, side=side, date=seed['date'],
                prospective_plan=prospective_plan(symbol, side, seed) if admission_status == 'admitted' else None,
                admission_status=admission_status, outcome_status=outcome_status,
                future_prices_used_for_admission=False)
