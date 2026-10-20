import numpy as np
import json
import pandas as pd
import pytest

from right_side_entry_research import (
    confirmation_opportunities, direction_ok, make_context, replay_right_side,
)
from pullback_entry_research import build_entry_timeline
from rebound_short_research import build_short_timeline
from pullback_validation import shift_session
from event_exit_research import compile_calendar


def fixture(side='LONG'):
    days = [pd.Timestamp(shift_session(pd.Timestamp('2026-05-01').date(), n)) for n in range(80)]
    f = pd.DataFrame(dict(Open=100., High=102., Low=98., Close=101., Volume=1e6), index=days)
    if side == 'SHORT':
        f['Close'] = 99.
    c = pd.DataFrame(dict(trend_up=True, trend_down=True, cycle_state='up_cycle' if side=='LONG' else 'down_cycle',
                          ma20=99. if side=='LONG' else 101., ma50=98. if side=='LONG' else 102.,
                          ma20_slope=1. if side=='LONG' else -1.), index=f.index)
    seed = dict(id='AAA:'+side+':'+str(days[55].date()), date=str(days[55].date()),
                setup_date=str(days[54].date()), high=100., low=96. if side=='LONG' else 100., atr=4., adv=1e8)
    if side == 'SHORT':
        seed.update(high=104., low=100.)
    return f, c, seed


def run(f, c, seed, side='LONG', policy='current', schedule=None):
    return replay_right_side('AAA', f, side, policy, schedule, [seed], c)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_next_open_not_confirmation_close(side):
    f,c,s=fixture(side)
    f.loc[f.index[56], ['Open','High','Low','Close']] = [101.,103.,99.,102.] if side=='LONG' else [99.,101.,97.,98.]
    t=run(f,c,s,side)['transactions'][0]
    assert t['entry_date']==str(f.index[56].date())
    assert t['entry_price']==f.iloc[56].Open
    assert t['signal_date']==s['date']


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_strict_is_symmetric_and_does_not_change_original_gate(side):
    f,c,s=fixture(side)
    c.loc[f.index[55],'ma20_slope']=0.
    assert run(f,c,s,side)['transactions']
    assert not run(f,c,s,side,'strict')['transactions']
    assert run(f,c,s,side,'strict')['skips']['strict_trend_rejected']==1
    c.loc[f.index[55],'ma20_slope']=1. if side=='LONG' else -1.
    assert run(f,c,s,side,'strict')['transactions']
    c.loc[f.index[55],'cycle_state']='down_cycle' if side=='LONG' else 'up_cycle'
    assert not direction_ok(f.iloc[55],c.iloc[55],side,True)


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_retest_waits_one_day_then_next_open(side):
    f,c,s=fixture(side)
    out=run(f,c,s,side,'retest')
    confirmed=[m for m in out['markers'] if m['stage']=='CONFIRMED']
    assert confirmed[0]['time']==str(f.index[56].date())
    assert out['transactions'][0]['entry_date']==str(f.index[57].date())


def test_retest_timeout_and_structure_failure():
    f,c,s=fixture()
    f.loc[f.index[56]:, ['Open','High','Low','Close']]=[106.,108.,105.,107.]
    out=run(f,c,s,policy='retest')
    assert not out['transactions']
    assert out['skips']['retest_expired_or_failed']==1
    f.loc[f.index[56], 'Low']=95.
    assert run(f,c,s,policy='retest')['skips']['structure_invalidated']==1


def schedule_for(f, day_index, symbol='AAA', category='earnings'):
    event=dict(event_id='e', category=category, title='CPI' if category=='macro' else 'earnings',
               date=str(f.index[day_index].date()), first_seen_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T00:00:00Z',
               status='scheduled', confidence='official', symbols=[dict(symbol=symbol,relation_type='subject')])
    return compile_calendar(dict(events=[event],changes=[], exported_at='2026-01-01T00:00:00Z'),{})


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_event_entry_delayed_close_reference_not_fill(side):
    f,c,s=fixture(side)
    f.loc[f.index[57], ['Open','High','Low','Close']] = [102.,103.,100.,102.5] if side=='LONG' else [98.,100.,97.,97.5]
    out=run(f,c,s,side,schedule=schedule_for(f,56))
    t=out['transactions'][0]
    assert t['entry_date']==str(f.index[57].date())
    assert t['reference_event_close']['price']==f.iloc[56].Close
    assert t['entry_price']==f.iloc[57].Open
    assert t['entry_price']!=t['reference_event_close']['price']
    before=run(f.iloc[:56],c.iloc[:56],s,side,schedule=schedule_for(f,56))
    assert not before['transactions']
    assert all(m['stage']!='EVENT_WAIT' for m in before['markers'])


def test_earnings_subject_only_and_macro_global():
    f,c,s=fixture()
    assert run(f,c,s,schedule=schedule_for(f,56,'BBB'))['transactions'][0]['entry_date']==str(f.index[56].date())
    assert run(f,c,s,schedule=schedule_for(f,56,'BBB','macro'))['transactions'][0]['entry_date']==str(f.index[57].date())


def test_event_close_requires_reconfirmation():
    f,c,s=fixture()
    f.loc[f.index[56], 'Close']=99.
    o=run(f,c,s,schedule=schedule_for(f,56))
    assert not o['transactions']
    assert o['skips']['event_close_not_reconfirmed']==1


def test_event_previous_close_exit_and_intraday_stop_priority():
    f,c,s=fixture()
    o=run(f,c,s,schedule=schedule_for(f,59))
    assert o['transactions'][0]['exit_date']==str(f.index[58].date())
    assert o['transactions'][0]['exit_reason']=='event_previous_close'
    f.loc[f.index[58], 'Low']=95.
    assert run(f,c,s,schedule=schedule_for(f,59))['transactions'][0]['exit_reason']=='stop_first_conservative'


def test_both_touched_stop_first_and_gap_not_stop_fill():
    f,c,s=fixture()
    f.loc[f.index[56], ['High','Low']]=[111.,95.]
    t=run(f,c,s)['transactions'][0]
    assert t['exit_price']==96.
    f,c,s=fixture()
    f.loc[f.index[57], ['Open','High','Low','Close']]=[94.,95.,93.,94.]
    t=run(f,c,s)['transactions'][0]
    assert t['exit_price']==94.


def test_missing_session_does_not_invent_entry():
    f,c,s=fixture()
    f=f.drop(f.index[56])
    o=run(f,c,s)
    assert o['status']=='unavailable'
    assert not o['transactions']


@pytest.mark.parametrize('policy',['current','strict','retest'])
@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_appended_and_shocked_future_preserves_past_markers(policy,side):
    f,c,s=fixture(side)
    cut=59
    before=run(f.iloc[:cut],c.iloc[:cut],s,side,policy)
    future=f.copy()
    future.loc[future.index[cut]:,['Open','High','Low','Close']]*=2
    full=run(future,c,s,side,policy)
    assert before['markers']==[m for m in full['markers'] if m['time']<=str(f.index[cut-1].date())]


def test_seed_extraction_ignores_future_outcomes():
    f,c,s=fixture()
    timeline=dict(markers=[dict(time=s['setup_date'],stage='WATCH'),dict(time=s['date'],stage='CONFIRMED')],
                  transactions=[dict(fake_future_profit=999999)])
    a=confirmation_opportunities('AAA',f,'LONG',timeline)
    timeline['transactions']=[]
    assert a==confirmation_opportunities('AAA',f,'LONG',timeline)


@pytest.mark.parametrize('side,build',[('LONG',build_entry_timeline),('SHORT',build_short_timeline)])
def test_current_reproduces_baseline_next_open_transactions(side,build):
    rng=np.random.default_rng(17)
    days=[pd.Timestamp(shift_session(pd.Timestamp('2024-01-02').date(),n)) for n in range(550)]
    prices=100*np.exp(np.cumsum(rng.normal(0,.022,len(days))))
    op=np.r_[prices[0],prices[:-1]]
    f=pd.DataFrame(dict(Open=op,Close=prices,High=np.maximum(op,prices)*1.012,
                        Low=np.minimum(op,prices)*.988,Volume=1e6),index=days)
    base=build('AAA',f)
    replay=replay_right_side('AAA',f,side)
    keys=('signal_date','entry_date','entry_price','exit_date','exit_price')
    assert base['transactions']  # Guard against a vacuous equivalence test.
    assert [{k:t[k] for k in keys} for t in replay['transactions']]==[{k:t[k] for k in keys} for t in base['transactions']]


def test_maturity_is_fixed_not_selected_by_early_exit():
    from scripts.validate_right_side import eligible_period
    f,c,s=fixture()
    assert eligible_period(f.iloc[:60],s)[0] is None
    assert eligible_period(f,s)[0]=='test'
    assert eligible_period(f.drop(f.index[60]),s)[0] is None


def test_cross_partition_not_pooled():
    from scripts.validate_right_side import eligible_period
    days=[pd.Timestamp(shift_session(pd.Timestamp('2025-12-15').date(),n)) for n in range(20)]
    frame=pd.DataFrame(index=days)
    assert eligible_period(frame,dict(date='2025-12-16',setup_date='2025-12-15'))==(None,'cross_partition')


def test_cash_skips_share_common_denominator_not_trade_win_rate():
    from scripts.validate_right_side import aggregate
    f,c,s=fixture()
    from scripts.validate_event_exit import costed
    t=costed(run(f,c,s)['transactions'][0],'LONG')
    t['net_return']=.10
    rows=[dict(period='test',side='LONG',date=s['date'],variants=dict(current=t,strict=None,retest=t)),
          dict(period='test',side='LONG',date=str(f.index[57].date()),variants=dict(current=t,strict=t,retest=None))]
    o=aggregate(rows,'test','LONG')
    assert o['current']['mean_per_opportunity']==pytest.approx(.1)
    assert o['strict']['mean_per_opportunity']==pytest.approx(.05)
    assert o['strict']['trades']['net_win_rate']==1.
    assert o['strict']['opportunities']==2
    assert o['strict']['skipped_or_cash']==1


def test_new_events_not_known_before_prior_close_do_not_defer():
    f,c,s=fixture()
    item=dict(event_id='late',category='earnings',title='earnings',date=str(f.index[56].date()),
        first_seen_at=str(f.index[56].date())+'T14:00:00Z',status='scheduled',confidence='official',
        symbols=[dict(symbol='AAA',relation_type='subject')])
    schedule=compile_calendar(dict(events=[item],changes=[]),{})
    assert not schedule.exits
    assert run(f,c,s,schedule=schedule)['transactions'][0]['entry_date']==str(f.index[56].date())


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('policy',['current','strict','retest'])
def test_full_report_exports_without_nonstandard_scalars(side,policy):
    f,c,s=fixture(side)
    result=run(f,c,s,side,policy)
    assert result['transactions']
    assert type(result['transactions'][0]['strict_at_decision']) is bool
    assert json.loads(json.dumps(result,allow_nan=False))==result


def test_daily_timestamp_need_not_be_midnight_for_scalar_atr_or_maturity():
    from scripts.validate_right_side import eligible_period
    f,c,s=fixture()
    stamps=f.index+pd.Timedelta(hours=5)
    f.index=c.index=stamps
    timeline=dict(markers=[dict(time=s['setup_date'],stage='WATCH'),dict(time=s['date'],stage='CONFIRMED')])
    seeds=confirmation_opportunities('AAA',f,'LONG',timeline)
    assert np.isfinite(seeds[0]['atr'])
    assert eligible_period(f,s)[0]=='test'
    assert run(f,c,s)['transactions'][0]['entry_date']==str(stamps[56].date())


def test_intraday_rows_cannot_masquerade_as_unique_daily_bars():
    f,c,s=fixture()
    extra=f.iloc[[-1]].copy()
    extra.index+=pd.Timedelta(hours=5)
    f=pd.concat([f,extra])
    timeline=dict(markers=[dict(time=s['setup_date'],stage='WATCH'),dict(time=s['date'],stage='CONFIRMED')])
    with pytest.raises(ValueError,match='duplicate_calendar_dates'):
        confirmation_opportunities('AAA',f,'LONG',timeline)


def test_setup_confirmation_missing_calendar_session_is_not_three_days():
    from scripts.validate_right_side import eligible_period
    f,c,s=fixture()
    s['setup_date']=str(f.index[50].date())
    assert eligible_period(f,s)==(None,'setup_confirmation_gap_or_expired')
    s['setup_date']=str(f.index[53].date())
    assert eligible_period(f.drop(f.index[54]),s)==(None,'setup_confirmation_gap_or_expired')
    assert eligible_period(f,s)[0]=='test'
