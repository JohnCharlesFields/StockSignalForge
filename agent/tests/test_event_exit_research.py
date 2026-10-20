from datetime import date
import pandas as pd
import pytest

from event_exit_research import compile_calendar, apply_event_exit
from trading_calendar_history import full_snapshot


def fixtures():
    f = pd.DataFrame(dict(Open=[100.]*5, High=[101.]*5, Low=[99.]*5,
                          Close=[100., 105., 90., 80., 70.], Volume=[1_000_000.]*5),
                     index=pd.bdate_range('2026-10-05', periods=5))
    trade = dict(symbol='AAA', signal_date='2026-10-02', entry_date='2026-10-05', entry_price=100.,
                 exit_date='2026-10-09', exit_price=70., held_sessions=4, exit_reason='time_exit_at_open')
    return f, trade


def event(symbol='AAA', *, category='earnings', known='2026-10-01T00:00:00Z', day='2026-10-07'):
    return dict(event_id='e1', category=category, date=day, first_seen_at=known, updated_at=known, status='scheduled',
                confidence='official', title='CPI' if category=='macro' else 'earnings',
                symbols=[dict(symbol=symbol, relation_type='subject')])


def revised_export(item, before, after, when):
    original = {**item, **before, 'detail': {}}
    current = {**original, **after, 'updated_at': when}
    return dict(events=[current], changes=[
        dict(event_id='e1', changed_at=original['first_seen_at'], before={}, after=full_snapshot(original)),
        dict(event_id='e1', changed_at=when, before=full_snapshot(original), after=full_snapshot(current)),
    ])


def archived(items):
    return dict(events=items, changes=[dict(event_id=item['event_id'], changed_at=item['updated_at'],
        before={}, after=full_snapshot({**item, 'detail': {}})) for item in items])


def test_earnings_only_closes_subject_stock_not_others():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event()]), {})
    changed = apply_event_exit(trade, f, schedule, 'LONG')
    assert changed['exit_date'] == '2026-10-06'
    assert changed['exit_price'] == 105.
    assert changed['exit_reason'] == 'event_previous_close'
    assert apply_event_exit({**trade, 'symbol':'BBB'}, f, schedule, 'LONG') == trade | {'symbol':'BBB'}


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_macro_affects_all_symbols_and_both_directions(side):
    f, trade = fixtures()
    schedule = compile_calendar(archived([event(category='macro')]), {})
    changed = apply_event_exit({**trade, 'symbol':'BBB'}, f, schedule, side)
    assert changed['exit_date'] == '2026-10-06'
    expected = .05 if side=='LONG' else -.05
    assert changed['event_gross_return'] == pytest.approx(expected)


def test_late_observation_cannot_change_a_past_exit():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event(known='2026-10-07T00:00:00Z')]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_close_auction_decision_cutoff_is_before_close():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event(known='2026-10-06T19:50:00Z')]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_original_intraday_stop_on_same_day_precedes_event_close():
    f, trade = fixtures()
    trade.update(exit_date='2026-10-06', exit_price=98., exit_reason='stop_first_conservative')
    schedule = compile_calendar(archived([event()]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_same_entry_day_close_is_allowed_not_earlier_than_entry():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event(day='2026-10-06')]), {})
    changed = apply_event_exit(trade, f, schedule, 'LONG')
    assert changed['exit_date'] == trade['entry_date']
    assert changed['held_sessions'] == 0


def test_event_before_entry_does_not_backdate_exit():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event(day='2026-10-05')]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_future_cancellation_does_not_erase_past_exit():
    f, trade = fixtures()
    item = event()
    item['status'] = 'cancelled'
    change = dict(event_id='e1', changed_at='2026-10-08T00:00:00Z',
                  before=dict(date='2026-10-07', status='scheduled'), after=dict(date='2026-10-07', status='cancelled'))
    schedule = compile_calendar(revised_export(item, change['before'], change['after'], change['changed_at']), {})
    assert apply_event_exit(trade, f, schedule, 'LONG')['exit_date'] == '2026-10-06'


def test_cancellation_known_before_cutoff_does_cancel():
    f, trade = fixtures()
    item = event()
    item['status'] = 'cancelled'
    change = dict(event_id='e1', changed_at='2026-10-05T00:00:00Z',
                  before=dict(date='2026-10-07', status='scheduled'), after=dict(date='2026-10-07', status='cancelled'))
    schedule = compile_calendar(revised_export(item, change['before'], change['after'], change['changed_at']), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_date_revision_cannot_use_latest_date_retroactively():
    f, trade = fixtures()
    item = event(day='2026-10-09')
    change = dict(event_id='e1', changed_at='2026-10-08T00:00:00Z',
                  before=dict(date='2026-10-07'), after=dict(date='2026-10-09'))
    schedule = compile_calendar(revised_export(item, change['before'], change['after'], change['changed_at']), {})
    assert apply_event_exit(trade, f, schedule, 'LONG')['exit_date'] == '2026-10-06'


def test_unknown_timezone_or_reported_only_date_is_not_preknown():
    f, trade = fixtures()
    item = event(known='2026-10-01')
    schedule = compile_calendar(archived([item]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade
    item = event()
    item['status'] = 'reported'
    schedule = compile_calendar(archived([item]), {})
    assert apply_event_exit(trade, f, schedule, 'LONG') == trade


def test_fomc_uses_second_meeting_day():
    item = event(category='macro', day='2026-10-06')
    item.update(title='FOMC meeting', end_date='2026-10-07')
    schedule = compile_calendar(archived([item]), {})
    assert schedule.exits[0]['exit_date'] == '2026-10-06'


def test_official_advance_schedule_and_weekend_previous_session():
    schedule = compile_calendar({}, dict(announcements=[dict(source_url='official',
        known_at='2026-09-01T00:00:00Z', decision_dates=['2026-10-12'])]))
    assert schedule.exits[0]['exit_date'] == '2026-10-09'
    assert schedule.exits[0]['kind'] == 'FOMC'


def test_append_future_event_preserves_old_exit():
    f, trade = fixtures()
    a = compile_calendar(archived([event()]), {})
    b = compile_calendar(archived([event(), {**event(known='2026-11-01T00:00:00Z'), 'event_id':'future'}]), {})
    assert apply_event_exit(trade, f, a, 'LONG') == apply_event_exit(trade, f, b, 'LONG')


def test_missing_exit_close_is_not_filled_from_future():
    f, trade = fixtures()
    schedule = compile_calendar(archived([event()]), {})
    with pytest.raises(ValueError, match='missing_event_exit_close'):
        apply_event_exit(trade, f.drop(pd.Timestamp('2026-10-06')), schedule, 'LONG')


def test_future_confidence_downgrade_does_not_erase_prior_macro_exit():
    f, trade = fixtures()
    item = event(category='macro')
    item['confidence'] = 'unverified'
    change = dict(event_id='e1', changed_at='2026-10-08T00:00:00Z',
                  before=dict(confidence='official'), after=dict(confidence='unverified'))
    schedule = compile_calendar(revised_export(item, change['before'], change['after'], change['changed_at']), {})
    assert apply_event_exit(trade, f, schedule, 'LONG')['exit_date'] == '2026-10-06'


def test_early_close_uses_earlier_decision_cutoff():
    schedule = compile_calendar(archived([event(category='macro', day='2026-11-30',
        known='2026-11-27T18:00:00Z')]), {})
    assert not schedule.exits  # Friday after Thanksgiving closed at 13:00 ET.


def test_comparison_costs_preserve_long_and_short_return_meaning():
    from scripts.validate_event_exit import costed
    _, trade = fixtures()
    trade.update(exit_date='2026-10-06', exit_price=105., adv=1_000_000_000.)
    long, short = costed(trade, 'LONG'), costed(trade, 'SHORT')
    assert long['net_return'] == pytest.approx(.05-.0006)
    assert short['net_return'] == pytest.approx(-.05-.0006)
    assert short['borrow_5pct_return'] == pytest.approx(short['net_return']-.05/365)


@pytest.mark.parametrize('side,key', [('LONG','gross_return'), ('SHORT','gross_directional_return')])
def test_changed_exit_does_not_leave_old_gross_return_metadata(side, key):
    f, trade = fixtures()
    trade[key] = -.3
    schedule = compile_calendar(archived([event()]), {})
    changed = apply_event_exit(trade, f, schedule, side)
    assert changed[key] == changed['event_gross_return']
    assert changed[key] == pytest.approx(.05 if side == 'LONG' else -.05)
