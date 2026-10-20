"""Decision-time features, independent controls, and no future-open filtering."""
import numpy as np
import pandas as pd
import pytest

from entry_path_research import entry_gate
from planned_risk_research import EXPERIMENTS, enrich_setup_features, planned_gate
from right_side_entry_research import replay_right_side
from .test_right_side_entry_research import fixture, schedule_for


@pytest.mark.parametrize('risk,allowed', [(0., False), (.999, False), (1., True), (3., True)])
def test_long_planned_risk_floor(risk, allowed):
    assert (planned_gate('long_risk_floor', 'LONG', 96+4*risk,
                         dict(low=96., atr=4.)) is None) == allowed


@pytest.mark.parametrize('risk,allowed', [(-.1, False), (0., False), (.1, True), (2., True), (2.001, False)])
def test_short_planned_risk_cap(risk, allowed):
    assert (planned_gate('short_risk_cap', 'SHORT', 104-4*risk,
                         dict(high=104., atr=4.)) is None) == allowed


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), 0., -1.])
@pytest.mark.parametrize('name,side', [('long_risk_floor', 'LONG'), ('short_risk_cap', 'SHORT')])
def test_invalid_atr_is_unknown_not_a_zero_distance(value, name, side):
    assert planned_gate(name, side, 100., dict(low=96., high=104., atr=value)) == 'planned_risk_data_unknown'


def test_rebound_and_extension_boundaries_and_unknown():
    s = dict(atr=4., setup_rebound_atr=1.)
    assert planned_gate('short_rebound', 'SHORT', 100., s) is None
    assert planned_gate('short_rebound', 'SHORT', 100., {**s, 'setup_rebound_atr': .999})
    assert planned_gate('short_rebound', 'SHORT', 100., {**s, 'setup_rebound_atr': None}) == 'setup_rebound_unknown'
    assert planned_gate('short_extension', 'SHORT', 100., s, dict(ma20=108.)) is None
    assert planned_gate('short_extension', 'SHORT', 100., s, dict(ma20=108.01))
    assert planned_gate('short_extension', 'SHORT', 100., s, dict(ma20=99.)) is None
    assert planned_gate('short_extension', 'SHORT', 100., s, None) == 'decision_ma20_unknown'


def test_rebound_uses_watch_day_atr_and_excludes_watch_low_from_prior_low():
    f, _, s = fixture('SHORT')
    s = {**s, 'setup_date': str(f.index[54].date())}
    enriched = enrich_setup_features(f, [s])[0]
    assert enriched['setup_rebound_atr'] == 1.
    changed = f.copy()
    changed.loc[changed.index[55]:, ['Open', 'High', 'Low', 'Close']] *= 100
    assert enrich_setup_features(changed, [s]) == [enriched]
    assert enrich_setup_features(f.iloc[:55], [s]) == [enriched]
    # A new low on WATCH may affect its known ATR, but never becomes the prior low.
    f.loc[f.index[54], ['Low', 'High']] = [90., 102.]
    from technical_signal_metrics import calculate_atr
    expected = (102.-98.) / calculate_atr(f, 14).iloc[54]
    assert np.isclose(enrich_setup_features(f, [s])[0]['setup_rebound_atr'], expected)
    assert enrich_setup_features(f.drop(f.index[50]), [s])[0]['setup_rebound_atr'] is None
    assert 'setup_rebound_atr' not in s


def run(f, c, s, side, name, schedule=None):
    seeds = enrich_setup_features(f, [s]) if name == 'short_rebound' and side == 'SHORT' else [s]
    return replay_right_side('AAA', f, side, opportunities=seeds, context=c,
                             experiment=name, schedule=schedule)


@pytest.mark.parametrize('name', EXPERIMENTS)
@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_future_prices_never_change_existing_decisions(name, side):
    f, c, s = fixture(side)
    cut = 56
    prefix = run(f.iloc[:cut], c.iloc[:cut], s, side, name)
    changed = f.copy()
    changed.loc[changed.index[cut]:, ['Open', 'High', 'Low', 'Close']] *= 2
    out = run(changed, c, s, side, name)
    assert prefix['markers'] == [m for m in out['markers'] if m['time'] <= s['date']]


@pytest.mark.parametrize('name,side', [('long_risk_floor', 'LONG'), ('short_risk_cap', 'SHORT')])
def test_next_open_changes_fill_not_previous_risk_gate(name, side):
    f, c, s = fixture(side)
    original = run(f, c, s, side, name)
    changed = f.copy()
    changed.loc[changed.index[56], ['Open', 'High', 'Low', 'Close']] = (
        [108., 109., 107., 108.] if side == 'LONG' else [90., 91., 89., 90.])
    other = run(changed, c, s, side, name)
    assert [m for m in original['markers'] if m['time'] <= s['date']] == [m for m in other['markers'] if m['time'] <= s['date']]
    assert original['transactions'][0]['entry_price'] != other['transactions'][0]['entry_price']


@pytest.mark.parametrize('name', EXPERIMENTS)
def test_non_applicable_direction_is_exact_baseline(name):
    side = 'SHORT' if name == 'long_risk_floor' else 'LONG'
    f, c, s = fixture(side)
    assert run(f, c, s, side, name) == run(f, c, s, side, 'baseline')


def test_event_reconfirmation_rechecks_today_ma20_not_old_confirmation_ma():
    f, c, s = fixture('SHORT')
    c.loc[f.index[56], 'ma20'] = 110.
    baseline = run(f, c, s, 'SHORT', 'baseline', schedule_for(f, 56))
    out = run(f, c, s, 'SHORT', 'short_extension', schedule_for(f, 56))
    assert baseline['transactions']
    assert not out['transactions']
    assert out['skips']['short_extension_rejected'] == 1


def test_event_reconfirmation_rechecks_today_planned_risk():
    f, c, s = fixture()
    f.loc[f.index[56], ['Open', 'High', 'Low', 'Close']] = [100., 102., 98., 99.]
    out = run(f, c, s, 'LONG', 'long_risk_floor', schedule_for(f, 56))
    assert not out['transactions']
    assert out['skips']['planned_risk_rejected'] == 1
    assert entry_gate('baseline', 'LONG', s['date'], 101., s, None) is None
