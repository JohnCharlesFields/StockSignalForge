"""Causal decisions and path diagnostics; no profitability assumptions."""
import copy

import numpy as np
import pandas as pd
import pytest

from entry_path_research import complete_window, complete_forward_window, entry_gate, market_states, path_diagnostic, EXPERIMENTS
from pullback_validation import shift_session
from right_side_entry_research import replay_right_side
from scripts.validate_event_exit import costed
from .test_right_side_entry_research import fixture, schedule_for


def replay(f, c, s, side='LONG', experiment='baseline', market=None):
    return replay_right_side('AAA', f, side, opportunities=[s], context=c,
                             experiment=experiment, market_context=market)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_confirmation_distance_boundary_and_symmetry(side):
    seed = dict(high=100., low=100., atr=4.)
    sign = 1 if side == 'LONG' else -1
    assert entry_gate('distance', side, '2026-01-02', 100+sign*2, seed, None) is None
    assert entry_gate('distance', side, '2026-01-02', 100+sign*2.01, seed, None)
    assert entry_gate('distance', side, '2026-01-02', 100-sign, seed, None)
    assert entry_gate('distance', side, '2026-01-02', 100, {**seed, 'atr': float('nan')}, None)


def test_market_window_is_trailing_complete_and_finite():
    days = [pd.Timestamp(shift_session(pd.Timestamp('2025-01-02').date(), i)) for i in range(90)]
    f = pd.DataFrame(dict(Close=np.arange(100., 190.)), index=days)
    before = market_states(f.iloc[:70])
    future = f.copy()
    future.iloc[70:] *= .01
    after = market_states(future)
    assert before == {d: x for d, x in after.items() if d <= str(days[69].date())}
    assert before[str(days[53].date())]['state'] == 'unknown'
    assert before[str(days[54].date())]['state'] == 'up'
    assert market_states(f.drop(days[40]))[str(days[60].date())]['state'] == 'unknown'
    f.loc[days[40], 'Close'] = float('inf')
    assert market_states(f)[str(days[60].date())]['state'] == 'unknown'


def test_duplicate_daily_dates_rejected_in_market_window():
    f, _, _ = fixture()
    extra = f.iloc[[-1]].copy()
    extra.index += pd.Timedelta(hours=5)
    with pytest.raises(ValueError, match='duplicate_market_calendar'):
        market_states(pd.concat([f, extra]))
    assert not complete_window(pd.concat([f, extra]).index, str(f.index[-1].date()), 55)


def test_holiday_quote_cannot_change_market_ma_or_block_later_valid_sessions():
    days = [pd.Timestamp(shift_session(pd.Timestamp('2025-10-01').date(), i)) for i in range(100)]
    f = pd.DataFrame(dict(Close=np.arange(100., 200.)), index=days)
    christmas = pd.DataFrame(dict(Close=[1e6]), index=[pd.Timestamp('2025-12-25')])
    assert market_states(f) == market_states(pd.concat([f, christmas]).sort_index())


def test_forward_calendar_audit_rejects_phantom_holiday_and_missing_session_equally():
    days = [pd.Timestamp(shift_session(pd.Timestamp('2024-08-27').date(), i)) for i in range(16)]
    assert complete_forward_window(pd.DatetimeIndex(days), '2024-08-27')
    assert not complete_forward_window(pd.DatetimeIndex(days[:3]+days[4:]), '2024-08-27')
    phantom = pd.DatetimeIndex(days+[pd.Timestamp('2024-09-02')]).sort_values()
    assert not complete_forward_window(phantom, '2024-08-27')


def test_forward_calendar_audit_never_reads_trade_outcomes_or_future_price_values():
    f, _, s = fixture()
    before = complete_forward_window(f.index, s['date'])
    changed = f.copy()
    changed.loc[changed.index[56]:, ['Open', 'High', 'Low', 'Close']] *= 100
    assert before and before == complete_forward_window(changed.index, s['date'])


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_unknown_and_opposite_market_do_not_open(side):
    f, c, s = fixture(side)
    assert not replay(f, c, s, side, 'market', {})['transactions']
    state = 'up' if side == 'LONG' else 'down'
    assert replay(f, c, s, side, 'market', {s['date']: dict(state=state)})['transactions']
    assert not replay(f, c, s, side, 'market', {s['date']: dict(state='neutral')})['transactions']


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_failed_level_waits_until_next_open_and_stop_still_has_priority(side):
    f, c, s = fixture(side)
    f.loc[f.index[56], 'Close'] = 99. if side == 'LONG' else 101.
    f.loc[f.index[57], ['Open', 'High', 'Low', 'Close']] = [103., 104., 102., 103.] if side == 'LONG' else [97., 98., 96., 97.]
    prefix = replay(f.iloc[:57], c.iloc[:57], s, side, 'failed_level')
    assert not prefix['transactions']
    out = replay(f, c, s, side, 'failed_level')
    t = out['transactions'][0]
    assert t['exit_reason'] == 'failed_level_next_open'
    assert t['exit_date'] == str(f.index[57].date())
    assert t['exit_price'] == f.iloc[57].Open
    f.loc[f.index[57], ['Open', 'High', 'Low', 'Close']] = [94., 95., 93., 94.] if side == 'LONG' else [106., 107., 105., 106.]
    t = replay(f, c, s, side, 'failed_level')['transactions'][0]
    assert t['exit_reason'] == 'gap_stop'
    assert t['exit_price'] == f.iloc[57].Open


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_intraday_stop_precedes_failed_close(side):
    f, c, s = fixture(side)
    f.loc[f.index[56], ['Low', 'High', 'Close']] = [95., 102., 99.] if side == 'LONG' else [98., 105., 101.]
    t = replay(f, c, s, side, 'failed_level')['transactions'][0]
    assert t['exit_reason'] == 'stop_first_conservative'


@pytest.mark.parametrize('h', [3, 5])
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_holding_window_keeps_next_open_and_cost_convention(h, side):
    f, c, s = fixture(side)
    t = replay(f, c, s, side, 'hold'+str(h))['transactions'][0]
    assert t['held_sessions'] == h
    assert t['exit_date'] == str(f.index[56+h].date())


@pytest.mark.parametrize('experiment', EXPERIMENTS)
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_future_shock_does_not_change_known_decisions(experiment, side):
    f, c, s = fixture(side)
    market = {str(d.date()): dict(state='up' if side == 'LONG' else 'down') for d in f.index}
    cut = 59
    prefix = replay(f.iloc[:cut], c.iloc[:cut], s, side, experiment, market)
    future = f.copy()
    future.loc[future.index[cut]:, ['Open', 'High', 'Low', 'Close']] *= 3
    out = replay(future, c, s, side, experiment, market)
    assert prefix['markers'] == [m for m in out['markers'] if m['time'] <= str(f.index[cut-1].date())]


def test_diagnostic_does_not_use_exit_bar_high_as_profit_before_stop():
    f, c, s = fixture()
    f.loc[f.index[56], ['Open', 'High', 'Low', 'Close']] = [100., 120., 90., 100.]
    t = costed(replay(f, c, s)['transactions'][0], 'LONG')
    t['period'] = 'test'
    before = copy.deepcopy(t)
    d = path_diagnostic(f, t, s)
    assert not d['prior_close_profit_then_loss']
    assert d['horizons']['1']['favorable_range'] > 0  # Full-bar descriptive range only.
    assert t == before


def test_missing_session_cannot_be_a_one_day_diagnostic_return():
    f, c, s = fixture()
    t = costed(replay(f, c, s)['transactions'][0], 'LONG')
    t['period'] = 'test'
    d = path_diagnostic(f.drop(f.index[57]), t, s)
    assert d['horizons']['1'] is None


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('experiment', ['market', 'distance'])
def test_event_deferral_rechecks_the_gate_on_completed_event_day(side, experiment):
    f, c, s = fixture(side)
    same = 'up' if side == 'LONG' else 'down'
    market = {s['date']: dict(state=same), str(f.index[56].date()): dict(state='neutral')}
    f.loc[f.index[56], ['High', 'Low', 'Close']] = [104., 98., 103.] if side == 'LONG' else [102., 96., 97.]
    result = replay_right_side('AAA', f, side, opportunities=[s], context=c, experiment=experiment,
                               market_context=market, schedule=schedule_for(f, 56))
    assert not result['transactions']
    assert result['skips']['event_close_not_reconfirmed'] == 1
    assert any(m['stage'] == 'EVENT_WAIT' for m in result['markers'])


def test_event_close_exit_precedes_failed_level_next_open():
    f, c, s = fixture()
    f.loc[f.index[56], 'Close'] = 99.
    out = replay_right_side('AAA', f, 'LONG', opportunities=[s], context=c,
                            experiment='failed_level', schedule=schedule_for(f, 57))
    assert out['transactions'][0]['exit_reason'] == 'event_previous_close'
    assert out['transactions'][0]['exit_date'] == str(f.index[56].date())


def test_common_opportunities_keep_cash_in_denominator():
    from scripts.validate_entry_paths import summarize
    f, c, s = fixture()
    trade = costed(replay(f, c, s)['transactions'][0], 'LONG')
    trade['net_return'] = .1
    rows = [dict(side='LONG', period='test', date=s['date'], variants={name: trade for name in EXPERIMENTS}),
            dict(side='LONG', period='test', date=s['date'], variants={name: trade if name == 'baseline' else None for name in EXPERIMENTS})]
    result = summarize(rows, 'test', 'LONG')
    assert result['market']['trades']['net_win_rate'] == 1.
    assert result['market']['mean_per_opportunity'] == pytest.approx(.05)
    assert result['market']['equal_day_difference'] == pytest.approx(-.05)
    assert result['market']['opportunities'] == 2
