"""Offline right-side confirmation experiments over a causal shared seed stream."""
from __future__ import annotations

from collections import Counter

import pandas as pd

from pullback_entry_research import _bars, feature_history, build_entry_timeline
from rebound_short_research import build_short_timeline
from pullback_validation import shift_session
from technical_signal_metrics import calculate_atr
from corporate_action_research import action_aware_replay, prepare_history, shares_between

VERSION = 'right_side_fixed_v2'
POLICIES = ('current', 'strict', 'retest')
MATURITY_SESSIONS = 15


def confirmation_opportunities(symbol, frame, side, timeline=None):
    timeline = timeline or (build_entry_timeline if side=='LONG' else build_short_timeline)(symbol, frame)
    watch, seeds = None, []
    by_day = {str(day.date()): row for day, row in frame.iterrows()}
    stamps = {str(day.date()): day for day in frame.index}
    if len(stamps) != len(frame):
        raise ValueError('duplicate_calendar_dates_in_daily_input')
    normalized, factors, _ = prepare_history(symbol, frame) if not frame.attrs.get('corporate_action_basis') else (
        frame, {str(d.date()):1. for d in frame.index}, None)
    atr = calculate_atr(normalized, 14)
    adv = (frame.Close*frame.Volume).rolling(20).mean()
    for marker in timeline['markers']:
        day, stage = marker['time'], marker['stage']
        if stage == 'WATCH':
            watch = day
        elif stage == 'INVALIDATED':
            watch = None
        elif stage == 'CONFIRMED' and watch and day in by_day:
            factor = shares_between(symbol, watch, day) if not frame.attrs.get('corporate_action_basis') else 1.
            seeds.append(dict(id=symbol+':'+side+':'+day, date=day, setup_date=watch,
                high=float(by_day[watch].High)/factor, low=float(by_day[watch].Low)/factor,
                atr=float(atr.at[stamps[day]])/factors.get(day, 1.), adv=float(adv.at[stamps[day]])))
            watch = None
    return seeds


def make_context(frame):
    context = feature_history(frame)
    context['ma20_slope'] = context.ma20-context.ma20.shift(5)
    return context


def direction_ok(bar, context, side, strict=False):
    if side == 'LONG':
        original = bool(context.trend_up and context.cycle_state != 'down_cycle')
        return bool(original and (not strict or bar.Close > context.ma20 > context.ma50 and context.ma20_slope > 0))
    original = bool(context.trend_down and context.cycle_state == 'down_cycle')
    return bool(original and (not strict or bar.Close < context.ma20 < context.ma50 and context.ma20_slope < 0))


def event_matches(event, symbol):
    return event['kind'] != 'EARNINGS' or symbol in event['subjects']


@action_aware_replay
def replay_right_side(symbol, frame, side, policy='current', schedule=None, opportunities=None, context=None,
                      *, experiment='baseline', market_context=None):
    from entry_path_research import EXPERIMENTS, entry_gate
    from planned_risk_research import EXPERIMENTS as PLANNED_EXPERIMENTS
    if side not in {'LONG','SHORT'} or policy not in POLICIES:
        raise ValueError('invalid_right_side_policy')
    if experiment not in EXPERIMENTS + PLANNED_EXPERIMENTS or (experiment == 'market' and market_context is None):
        raise ValueError('invalid_entry_path_experiment')
    holding_sessions = {'hold3': 3, 'hold5': 5}.get(experiment, 8)
    f = _bars(frame)
    out = dict(version=VERSION, policy=policy, side=side, status='unavailable', markers=[], transactions=[],
               skips={}, latest={}, calibrated=False, entry='next_observed_open_after_completed_signal',
               reference_close='quote_only_not_same_close_signal_fill')
    if len(f) < 55:
        return out
    context = make_context(f) if context is None else context.loc[f.index]
    seeds = confirmation_opportunities(symbol, f, side) if opportunities is None else opportunities
    by_day = {seed['date']:seed for seed in seeds}
    events = schedule.exits if schedule else []
    pending = position = None
    skips = Counter()
    sign = 1 if side == 'LONG' else -1
    unknown = None

    def mark(i, stage, seed, price=None):
        marker = dict(time=str(f.index[i].date()), stage=stage, opportunity_id=seed['id'], side=side, policy=policy)
        if price is not None:
            marker['price'] = float(price)
        out['markers'].append(marker)

    def finish(i, price, reason, matched=None):
        nonlocal position
        p = position
        out['transactions'].append({**p, 'exit_date':str(f.index[i].date()), 'exit_price':float(price),
            'held_sessions':i-p['entry_index'], 'exit_reason':reason,
            'gross_return':sign*(float(price)/p['entry_price']-1),
            'event_types':sorted({event['kind'] for event in matched or []})})
        mark(i, 'EXIT', p, price)
        position = None

    def intraday_exit(i, bar):
        if position is None:
            return
        p = position
        if sign*(bar.Open-p['stop']) <= 0:
            finish(i, bar.Open, 'gap_stop')
        elif p.get('failed_level_exit'):
            finish(i, bar.Open, 'failed_level_next_open')
        elif i-p['entry_index'] >= holding_sessions:
            finish(i, bar.Open, 'time_exit_at_open')
        elif sign*(bar.Open-p['target']) >= 0:
            finish(i, bar.Open, 'gap_target')
        elif (bar.Low <= p['stop'] if side=='LONG' else bar.High >= p['stop']):
            finish(i, p['stop'], 'stop_first_conservative')
        elif (bar.High >= p['target'] if side=='LONG' else bar.Low <= p['target']):
            finish(i, p['target'], 'target')

    for i, (stamp, bar) in enumerate(f.iterrows()):
        day, x = str(stamp.date()), context.iloc[i]
        if i and (pending or position) and shift_session(f.index[i-1].date(), 1) < stamp.date():
            unknown = 'missing_session_before_'+day
            break
        deferred_today = False
        intraday_exit(i, bar)
        if pending and pending['ready'] is not None and pending['ready'] < i and position is None:
            on_event = [e for e in events if e['event_date']==day and event_matches(e,symbol)]
            if on_event:
                pending['event_deferrals'] += 1
                if pending['event_deferrals'] > 3:
                    skips['event_deferral_expired'] += 1
                    mark(i, 'CANCELLED', pending)
                    pending = None
                else:
                    deferred_today = True
                    pending['ready'] = i
                    mark(i, 'EVENT_WAIT', pending)
            else:
                stop = pending['low'] if side=='LONG' else pending['high']
                risk = sign*(bar.Open-stop)
                target = bar.Open+sign*2*risk
                if risk <= 0 or target <= 0:
                    skips['invalid_open_risk'] += 1
                    mark(i, 'CANCELLED', pending)
                    pending = None
                else:
                    position = dict(**pending, symbol=symbol, side=side, policy=policy, entry_date=day,
                        entry_price=float(bar.Open), entry_index=i, signal_date=pending['date'], stop=stop,
                        target=float(target))
                    mark(i, 'ENTRY', pending, bar.Open)
                    pending = None
                    intraday_exit(i, bar)
        if position:
            matched = [e for e in events if e['exit_date']==day and event_matches(e,symbol)]
            if matched:
                finish(i, bar.Close, 'event_previous_close', matched)
            elif experiment == 'failed_level' and sign*(bar.Close-position['level']) < 0:
                position['failed_level_exit'] = day
        if pending:
            stop = pending['low'] if side=='LONG' else pending['high']
            level, tolerance = pending['level'], .5*pending['atr']
            structure_broken = bar.Low < stop if side=='LONG' else bar.High > stop
            if structure_broken:
                skips['structure_invalidated'] += 1
                mark(i, 'CANCELLED', pending)
                pending = None
            elif deferred_today:
                gate_reason = entry_gate(experiment, side, day, float(bar.Close), pending, market_context, x)
                if sign*(bar.Close-level) <= 0 or not direction_ok(bar,x,side,policy=='strict') or gate_reason:
                    skips['event_close_not_reconfirmed'] += 1
                    if gate_reason:
                        skips[gate_reason] += 1
                    mark(i, 'CANCELLED', pending)
                    pending = None
                else:
                    pending['reference_event_close'] = dict(date=day, price=float(bar.Close), publication_time='unknown')
                    pending['decision_date'] = day
                    pending['strict_at_decision'] = direction_ok(bar,x,side,True)
            elif pending['ready'] is None:
                age = i-pending['source_index']
                if age > 3 or sign*(bar.Close-level) < -tolerance:
                    skips['retest_expired_or_failed'] += 1
                    mark(i, 'CANCELLED', pending)
                    pending = None
                elif 1 <= age <= 3:
                    extreme = bar.Low if side=='LONG' else bar.High
                    touch = abs(extreme-level) <= tolerance
                    if touch and sign*(bar.Close-level) > 0 and sign*(bar.Close-bar.Open) > 0 and direction_ok(bar,x,side):
                        pending['ready'], pending['decision_date'] = i, day
                        pending['strict_at_decision'] = direction_ok(bar,x,side,True)
                        mark(i, 'CONFIRMED', pending, bar.Close)
        seed = by_day.get(day)
        if seed:
            gate_reason = entry_gate(experiment, side, day, float(bar.Close), seed, market_context, x)
            if position or pending:
                skips['active_or_pending'] += 1
            elif gate_reason:
                skips[gate_reason] += 1
                mark(i, 'FILTERED', seed)
            elif policy=='strict' and not direction_ok(bar,x,side,True):
                skips['strict_trend_rejected'] += 1
                mark(i, 'FILTERED', seed)
            else:
                pending = dict(**seed, source_index=i, level=seed['high'] if side=='LONG' else seed['low'],
                    ready=None if policy=='retest' else i, event_deferrals=0, decision_date=day,
                    strict_at_decision=direction_ok(bar,x,side,True))
                mark(i, 'WAIT_RETEST' if policy=='retest' else 'CONFIRMED', pending, bar.Close)
        out['latest'] = dict(date=day, stage='HOLD' if position else 'PENDING' if pending else 'WAIT')
    out.update(status='unavailable' if unknown or f.attrs.get('invalid_bar_at') else 'research_only',
        reason=unknown or f.attrs.get('invalid_bar_at'), skips=dict(skips), unresolved=int(position is not None),
        pending=int(pending is not None))
    return out
