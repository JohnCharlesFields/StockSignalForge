import pytest

from portfolio_risk_research import entry_plans
from research_opportunity_ledger import opportunity_record


def seed():
    return dict(id='AAA:LONG:2026-05-05', date='2026-05-05', setup_date='2026-05-04',
                low=95., high=105., adv=1e9)


def test_outcome_unknown_or_future_loss_cannot_remove_a_causal_plan():
    a = opportunity_record('AAA','LONG',seed(),'admitted','pending')
    b = {**a, 'outcome_status':'missing_path', 'future_return':-.99}
    assert entry_plans([a]) == entry_plans([b])
    assert a['future_prices_used_for_admission'] is False
    assert entry_plans([a])[0]['entry_date'] == '2026-05-06'


def test_completed_only_archive_is_not_default_prospective_universe():
    with pytest.raises(ValueError, match='explicit_diagnostic'):
        entry_plans([dict(variants=dict(baseline=None))])
    assert entry_plans([dict(variants=dict(baseline=None))], allow_completed_archive=True) == []


def test_known_input_rejection_stays_in_ledger_not_a_false_zero_return():
    record = opportunity_record('AAA','LONG',seed(),'input_unknown','pending')
    assert record['prospective_plan'] is None
    assert 'net_return' not in record
    assert entry_plans([record]) == []


def test_research_run_keeps_a_seed_when_future_prices_are_missing(tmp_path, monkeypatch):
    from scripts import validate_right_side as research
    from event_exit_research import EventExitSchedule
    from .test_right_side_entry_research import fixture
    f, _, s = fixture()
    # Enough historical input; omit the final settlement sessions only.
    s = {**s, 'date':str(f.index[-2].date()), 'setup_date':str(f.index[-3].date())}
    path = tmp_path/'AAA.csv'
    monkeypatch.setattr(research, 'confirmation_opportunities',
                        lambda symbol, frame, side: [s] if side == 'LONG' else [])
    monkeypatch.setattr(research, 'replay_right_side', lambda *args: dict(
        status='ok', skips={}, markers=[], transactions=[], latest=dict(date=str(f.index[-1].date()))))
    # run_symbol requires 100 past observations. This duplicate-free prefix is input-only.
    import pandas as pd
    from market_calendar import previous_trading_day
    extra = f.iloc[:30].copy()
    day, days = f.index[0].date(), []
    for _ in range(30):
        day = previous_trading_day(day)
        days.append(day)
    extra.index = pd.to_datetime(days[::-1])
    pd.concat([extra,f]).to_csv(path,index_label='Date')
    out = research.run_symbol((str(path), EventExitSchedule([],{})))
    assert 'error' not in out
    assert out['opportunities'] == []
    record = out['opportunity_ledger'][0]
    assert record['outcome_status'] == 'immature_or_missing_sessions'
    assert record['admission_status'] == 'admitted'
    assert entry_plans([record])
