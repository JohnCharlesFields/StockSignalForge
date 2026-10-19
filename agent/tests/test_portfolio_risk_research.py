from dataclasses import replace
import copy

import numpy as np
import pandas as pd
import pytest

import cost_model
from event_exit_research import EventExitSchedule
from portfolio_risk_research import PortfolioConfig, entry_plans, exit_at, replay_portfolio
from test_right_side_entry_research import fixture, schedule_for


EMPTY = EventExitSchedule([], {})


def setup(side='LONG', symbol='AAA', entry=55):
    f, _, _ = fixture(side)
    plan = dict(id=symbol+':'+side+':'+str(f.index[entry-1].date()), symbol=symbol, side=side,
                decision_date=str(f.index[entry-1].date()), entry_date=str(f.index[entry].date()),
                stop=95. if side == 'LONG' else 105., adv=1e9)
    return f, plan


def replay(f, p, **kw):
    return replay_portfolio({p['symbol']: f}, [p], [str(d.date()) for d in f.index[55:70]], EMPTY,
                            mode=p['side'], **kw)


@pytest.mark.parametrize('kw', [dict(initial_cash=float('nan')), dict(risk_per_trade=0.),
    dict(total_risk=.001), dict(max_position_ratio=2.), dict(max_positions=1.2), dict(borrow_rate=-1.)])
def test_invalid_risk_configuration(kw):
    with pytest.raises(ValueError):
        PortfolioConfig(**kw)


def test_future_exit_fields_and_returns_are_not_input_features():
    f, p = setup()
    rows = [dict(variants=dict(baseline={**p, 'exit_date': '2099-01-01', 'net_return': 99., 'rank': 1}))]
    before = entry_plans(rows)
    rows[0]['variants']['baseline'].update(exit_date='1900-01-01', exit_price=0., net_return=-999., rank=999)
    assert before == entry_plans(rows) == [p]
    assert set(before[0]) == {'id', 'symbol', 'side', 'decision_date', 'entry_date', 'stop', 'adv'}


def test_invalid_and_duplicate_entry_timing():
    f, p = setup()
    rows = [dict(variants=dict(baseline={**p, 'entry_date': p['decision_date']}))]
    with pytest.raises(ValueError, match='next_session'):
        entry_plans(rows)
    row = dict(variants=dict(baseline=p))
    with pytest.raises(ValueError, match='duplicate'):
        entry_plans([row, row])


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('sizing', ['equal_risk', 'fixed_notional'])
def test_net_cost_and_borrow_reconcile_with_account_cash(side, sizing):
    f, p = setup(side)
    out = replay(f, p, sizing=sizing)
    t = out['trades'][0]
    q = t['qty']
    expected_fees = q*100*cost_model.equity_round_trip_cost(100., p['adv'])
    assert np.isclose(t['fees'], expected_fees)
    days = (pd.Timestamp(t['exit_date'])-pd.Timestamp(t['entry_date'])).days
    expected_borrow = q*100*.05*days/365 if side == 'SHORT' else 0.
    assert np.isclose(t['borrow'], expected_borrow)
    assert np.isclose(t['net_pnl'], -expected_fees-expected_borrow)
    assert np.isclose(out['metrics']['final_value'], 100000+t['net_pnl'])
    assert out['metrics']['win_rate'] == 0
    assert out['metrics']['profit_factor'] == 0


def test_same_day_intraday_exit_does_not_finance_earlier_open():
    f, p = setup()
    g, b = setup(symbol='BBB', entry=56)
    f.loc[f.index[56], ['Open', 'High', 'Low', 'Close']] = [100., 102., 94., 101.]
    days = [str(d.date()) for d in f.index[55:70]]
    config = PortfolioConfig(max_positions=1)
    out = replay_portfolio({'AAA': f, 'BBB': g}, [p, b], days, EMPTY, config=config)
    assert [e['symbol'] for e in out['entries']] == ['AAA']
    assert out['rejects']['position_slots'] == 1
    f.loc[f.index[56], ['Open', 'High', 'Low', 'Close']] = [94., 96., 93., 95.]
    out = replay_portfolio({'AAA': f, 'BBB': g}, [p, b], days, EMPTY, config=config)
    assert [e['symbol'] for e in out['entries']] == ['AAA', 'BBB']


def test_todays_close_high_low_cannot_change_open_order_sizes():
    f, a = setup()
    g, b = setup(symbol='BBB', entry=56)
    days = [str(d.date()) for d in f.index[55:70]]
    before = replay_portfolio({'AAA': f, 'BBB': g}, [a, b], days, EMPTY)
    changed = f.copy()
    changed.loc[f.index[56], ['High', 'Low', 'Close']] = [300., 98., 290.]
    after = replay_portfolio({'AAA': changed, 'BBB': g}, [a, b], days, EMPTY)
    day = str(f.index[56].date())
    assert [e for e in before['entries'] if e['date'] <= day] == [e for e in after['entries'] if e['date'] <= day]


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('sizing', ['equal_risk', 'fixed_notional'])
def test_future_shock_and_prefix_keep_previous_account_decisions(side, sizing):
    f, p = setup(side)
    all_days = [str(d.date()) for d in f.index[55:70]]
    prefix = replay_portfolio({p['symbol']: f.iloc[:59]}, [p], all_days[:4], EMPTY,
                            mode=side, sizing=sizing, require_closed=False)
    changed = f.copy()
    changed.loc[f.index[59]:, ['Open', 'High', 'Low', 'Close']] *= 2
    out = replay_portfolio({p['symbol']: changed}, [p], all_days, EMPTY, mode=side, sizing=sizing)
    assert prefix['entries'] == out['entries']
    assert prefix['snapshots'] == out['snapshots'][:4]


def test_all_entry_caps_include_fees_and_do_not_reinvest_short_proceeds():
    data, plans = {}, []
    for n in range(10):
        f, p = setup('SHORT', symbol='S'+str(n))
        p['stop'] = 101.
        data[p['symbol']], plans = f, plans+[p]
    days = [str(d.date()) for d in f.index[55:70]]
    out = replay_portfolio(data, plans, days, EMPTY, mode='SHORT')
    assert len(out['entries']) <= 5
    for e in out['entries']:
        assert e['trade_risk_fraction'] <= .015
        assert e['total_risk_fraction'] <= .03
        assert e['gross_fraction'] <= 1.
        assert e['qty']*e['price']/e['equity_after'] <= .35
    assert min(s['free_collateral'] for s in out['snapshots']) >= -1e-6


def test_plan_order_is_not_input_file_order():
    f, a = setup()
    g, b = setup(symbol='BBB')
    days = [str(d.date()) for d in f.index[55:70]]
    first = replay_portfolio({'AAA': f, 'BBB': g}, [a, b], days, EMPTY)
    second = replay_portfolio({'BBB': g, 'AAA': f}, [b, a], days, EMPTY)
    assert first == second


def test_conflicting_same_symbol_does_not_pick_winning_side():
    f, a = setup()
    b = {**a, 'id': 'other', 'side': 'SHORT', 'stop': 105.}
    out = replay_portfolio({'AAA': f}, [a, b], [str(d.date()) for d in f.index[55:70]], EMPTY, mode='BOTH')
    assert not out['entries'] and out['rejects']['conflicting_or_duplicate_candidate'] == 2


def test_missing_active_quote_is_not_filled_with_entry_price():
    f, p = setup()
    days = [str(d.date()) for d in f.index[55:70]]
    with pytest.raises(ValueError, match='missing_active'):
        replay_portfolio({'AAA': f.drop(f.index[56])}, [p], days, EMPTY)


def test_missing_session_in_caller_calendar_cannot_shorten_holding_period():
    f, p = setup()
    days = [str(d.date()) for d in f.index[55:70] if d != f.index[56]]
    with pytest.raises(ValueError, match='missing_session_in_portfolio_calendar'):
        replay_portfolio({'AAA': f}, [p], days, EMPTY)


def test_daily_return_denominator_uses_the_declared_initial_capital():
    from scripts.validate_portfolio_risk import daily_returns
    run = dict(snapshots=[dict(equity=50000.), dict(equity=55000.)])
    np.testing.assert_allclose(daily_returns(run, 50000.), [0., .1])


def test_earnings_event_only_closes_its_own_stock():
    f, a = setup()
    g, b = setup(symbol='BBB')
    days = [str(d.date()) for d in f.index[55:70]]
    out = replay_portfolio({'AAA': f, 'BBB': g}, [a, b], days, schedule_for(f, 58))
    assert next(t for t in out['trades'] if t['symbol'] == 'AAA')['exit_reason'] == 'event_previous_close'
    assert next(t for t in out['trades'] if t['symbol'] == 'BBB')['exit_reason'] == 'time_exit_at_open'


def test_ambiguous_daily_high_low_uses_stop_first():
    f, p = setup()
    f.loc[f.index[55], ['High', 'Low']] = [120., 90.]
    t = replay(f, p)['trades'][0]
    assert t['exit_price'] == 95. and t['exit_reason'] == 'stop_first_conservative'


def test_positive_gross_smaller_than_cost_is_a_net_loser():
    f, p = setup()
    f.loc[f.index[63], ['Open', 'High', 'Low', 'Close']] = [100.01, 102., 98., 101.]
    out = replay(f, p)
    assert out['trades'][0]['net_pnl'] < 0
    assert out['metrics']['win_rate'] == 0
