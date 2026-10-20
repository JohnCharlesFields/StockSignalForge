import pandas as pd
import pytest

from event_exit_research import EventExitSchedule
from portfolio_risk_research import PortfolioConfig, replay_portfolio
from .test_portfolio_risk_research import setup


def split(symbol, day, ratio=4):
    return dict(symbol=symbol, effective_date=day, ratio=ratio, kind='split',
                known_at='2024-01-01T00:00:00Z', source_url='https://issuer.example/split',
                evidence_kind='dated_official_reconstruction')


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_split_changes_shares_and_levels_not_economic_value(side):
    f, p = setup(side)
    day = str(f.index[57].date())
    f.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    f.loc[f.index[57]:, 'Volume'] *= 4
    out = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]],
        EventExitSchedule([], {}), mode=side, corporate_actions=[split('AAA', day)])
    assert out['status'] == 'completed'
    trade = out['trades'][0]
    assert trade['exit_reason'] == 'time_exit_at_open'
    assert trade['qty'] == out['entries'][0]['qty']*4
    assert trade['entry_price'] == 25.
    assert trade['net_pnl'] < 0  # Flat economic value minus fees / borrow.
    assert abs(out['metrics']['total_return']) < .01


def test_unknown_jump_is_not_a_winning_or_losing_completed_account():
    f, p = setup()
    f.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    out = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]], EventExitSchedule([], {}))
    assert out['status'] == 'data_unknown'
    assert out['metrics'] is None
    assert out['entries'] and not out['trades']


def test_same_day_late_split_knowledge_cannot_adjust_earlier_open():
    f, p = setup()
    day = str(f.index[57].date())
    action = {**split('AAA', day), 'known_at':day+'T23:00:00Z'}
    f.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    out = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]],
        EventExitSchedule([], {}), corporate_actions=[action])
    assert out['status'] == 'data_unknown' and out['metrics'] is None


def test_future_split_does_not_change_previous_orders_or_snapshots():
    f, p = setup()
    days = [str(d.date()) for d in f.index[55:70]]
    a = replay_portfolio({'AAA':f}, [p], days[:2], EventExitSchedule([], {}), require_closed=False)
    changed = f.copy()
    changed.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    b = replay_portfolio({'AAA':changed}, [p], days, EventExitSchedule([], {}),
        corporate_actions=[split('AAA', days[2])])
    assert a['entries'] == b['entries']
    assert a['snapshots'] == b['snapshots'][:2]


def test_fractional_reverse_split_cash_in_lieu_is_not_invented():
    f, p = setup()
    day = str(f.index[57].date())
    f.loc[f.index[57]:, ['Open','High','Low','Close']] *= 7
    out = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]],
        EventExitSchedule([], {}), config=PortfolioConfig(initial_cash=10001),
        corporate_actions=[split('AAA', day, 1/7)])
    assert out['status'] == 'data_unknown'
    assert 'fractional' in out['unknown_reason']


def test_completed_account_costs_remain_in_original_notional_after_split():
    f, p = setup()
    day = str(f.index[57].date())
    baseline = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]], EventExitSchedule([], {}))
    f.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    after = replay_portfolio({'AAA':f}, [p], [str(d.date()) for d in f.index[55:70]],
        EventExitSchedule([], {}), corporate_actions=[split('AAA', day)])
    assert after['fees'] == baseline['fees']
    assert after['metrics']['final_value'] == baseline['metrics']['final_value']


def test_split_aware_single_trade_cost_is_not_raw_price_crash():
    from scripts.validate_event_exit import costed
    trade = dict(entry_date='2024-11-22', exit_date='2024-12-04', entry_price=405., exit_price=106.01,
                 share_multiplier=4., adv=1e9)
    assert costed(trade,'LONG')['net_return'] == pytest.approx(106.01*4/405-1-.0006)


def test_known_split_replay_preserves_past_signals_and_raw_chart_units():
    from right_side_entry_research import replay_right_side
    from .test_right_side_entry_research import fixture
    f, context, seed = fixture()
    original = replay_right_side('AAA',f,'LONG',opportunities=[seed],context=context)
    changed = f.copy()
    changed.loc[f.index[58]:, ['Open','High','Low','Close']] /= 4
    changed.loc[f.index[58]:, 'Volume'] *= 4
    out = replay_right_side('AAA',changed,'LONG',opportunities=[seed],context=context,
                            corporate_actions=[split('AAA',str(f.index[58].date()))])
    assert out['transactions'][0]['gross_return'] == original['transactions'][0]['gross_return']
    assert out['transactions'][0]['entry_price'] == original['transactions'][0]['entry_price']
    assert out['transactions'][0]['exit_price'] == original['transactions'][0]['exit_price']/4
    assert out['transactions'][0]['share_multiplier'] == 4


def test_adjusted_input_cannot_be_silently_split_again():
    from corporate_action_research import prepare_history
    f, _ = setup()
    day = str(f.index[57].date())
    _, _, unknown = prepare_history('AAA', f, [split('AAA', day)])
    assert unknown == 'unverified_price_units:AAA:'+day


@pytest.mark.parametrize('changes', [dict(ratio=float('nan')), dict(kind='spinoff'),
                                   dict(known_at='2024-01-01'), dict(source_url='')])
def test_unverified_action_is_not_an_automatic_price_adjustment(changes):
    from corporate_action_research import ActionDataUnknown, split_ratio
    action = {**split('AAA', '2026-07-01'), **changes}
    with pytest.raises(ActionDataUnknown):
        split_ratio('AAA', '2026-07-01', [action])


@pytest.mark.parametrize('exit_day,multiplier,close', [('2024-12-03',1.,410.), ('2024-12-04',4.,102.5)])
def test_changed_event_exit_recomputes_shares_at_actual_exit(exit_day, multiplier, close):
    from event_exit_research import apply_event_exit
    trade = dict(symbol='ANET', entry_date='2024-11-22', entry_price=405.,
                 exit_date='2024-12-05', share_multiplier=4., exit_price=103.41)
    frame = pd.DataFrame(dict(Close=[close]), index=pd.to_datetime([exit_day]))
    schedule = EventExitSchedule([dict(exit_date=exit_day, kind='FOMC', subjects=[],
        event_id='e', event_date='2024-12-06', known_at='2024-01-01T00:00:00Z')], {})
    out = apply_event_exit(trade, frame, schedule, 'LONG')
    assert out['share_multiplier'] == multiplier
    assert out['gross_return'] == pytest.approx(close*multiplier/405-1)


def test_unsafe_archived_path_is_blocked_not_reproduced_as_valid_profit():
    from scripts.validate_portfolio_risk import archive_unit_issues, verify_single_paths, daily_returns
    f, plan = setup()
    day = str(f.index[57].date())
    f.loc[f.index[57]:, ['Open','High','Low','Close']] /= 4
    trade = {**plan, 'exit_date':str(f.index[63].date())}
    rows = [dict(symbol='AAA', variants=dict(baseline=trade))]
    issues = archive_unit_issues({'AAA':f}, rows, [split('AAA', day)])
    assert issues[0]['reasons'] == [dict(date=day, reason='split_accounting_requires_regeneration')]
    with pytest.raises(ValueError, match='regeneration'):
        verify_single_paths({'AAA':f}, rows, EventExitSchedule([], {}))
    with pytest.raises(ValueError, match='not_a_return_series'):
        daily_returns(dict(status='data_unknown',metrics=None,snapshots=[]),100000.)
