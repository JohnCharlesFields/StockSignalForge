"""Causal chart/research contract; these tests do not validate profitability."""
import numpy as np
import pandas as pd
import pytest

from launch_signal_service import _score_frame
from pullback_entry_research import EntryConfig, feature_history, build_entry_timeline, pullback_score


def bars(n=180, down=False):
    rng = np.random.default_rng(215)
    close = 100 * np.exp(np.cumsum(rng.normal(-.002 if down else .002, .006, n)))
    return pd.DataFrame(dict(Open=close*.999, High=close*1.004, Low=close*.996,
                             Close=close, Volume=np.full(n, 1_000_000.)),
                        index=pd.bdate_range('2025-01-02', periods=n))


def test_bulk_features_match_actual_live_scorer():
    frame = bars()
    history = feature_history(frame)
    for i in range(26, len(frame), 7):
        actual = _score_frame('TEST', frame.iloc[:i+1], .5)
        assert history.iloc[i].launch_score == pytest.approx(actual['launch_score'], abs=1e-4)
        assert history.iloc[i].tunnel_score == actual['daily_tunnel']['score']
        assert history.iloc[i].cycle_state == actual['daily_tunnel']['cycle_state']


def test_score_uses_existing_weights():
    assert pullback_score(.4, .2, (.5, .5)) == pytest.approx(.6)
    assert pullback_score(.4, None, (.5, .5)) == pytest.approx(.55)


@pytest.mark.parametrize('cutoff', [60, 90, 130])
def test_appending_future_cannot_change_past_markers_or_states(cutoff):
    frame = bars()
    cfg = EntryConfig(setup_threshold=0)
    before = build_entry_timeline('TEST', frame.iloc[:cutoff], cfg)
    full = build_entry_timeline('TEST', frame, cfg)
    day = str(frame.index[cutoff-1].date())
    assert before['markers'] == [m for m in full['markers'] if m['time'] <= day]
    assert before['states'] == full['states'][:cutoff]


def test_future_shock_does_not_change_old_features_or_decisions():
    frame = bars()
    changed = frame.copy()
    changed.iloc[110:, :4] *= np.linspace(1, 4, len(frame)-110)[:, None]
    before, after = feature_history(frame), feature_history(changed)
    pd.testing.assert_frame_equal(before.iloc[:110], after.iloc[:110])
    cfg = EntryConfig(setup_threshold=0)
    a, b = build_entry_timeline('TEST', frame, cfg), build_entry_timeline('TEST', changed, cfg)
    day = str(frame.index[109].date())
    assert [m for m in a['markers'] if m['time'] <= day] == [m for m in b['markers'] if m['time'] <= day]


def test_falling_midterm_never_opens_research_position():
    frame = bars(down=True)
    # An unambiguous declining tape, not selected because its future lost money.
    frame['Close'] = np.linspace(200, 80, len(frame))
    for col, factor in [('Open', 1.001), ('High', 1.006), ('Low', .994)]: frame[col] = frame.Close * factor
    result = build_entry_timeline('TEST', frame, EntryConfig(setup_threshold=0))
    assert any(m['stage'] == 'WATCH' for m in result['markers'])
    assert not any(m['stage'] == 'ENTRY' for m in result['markers'])
    assert result['latest']['strategy'] == 'countertrend'


def test_missing_or_malformed_bars_are_not_bridged():
    frame = bars()
    frame.loc[frame.index[60], 'Close'] = np.nan
    result = build_entry_timeline('TEST', frame)
    assert result['status'] == 'unavailable'
    assert result['latest']['stage'] == 'DATA_UNKNOWN'
    assert all(m['time'] < str(frame.index[60].date()) for m in result['markers'])


def test_invalid_future_bar_does_not_delete_known_history():
    frame = bars()
    cfg = EntryConfig(setup_threshold=0)
    prefix = build_entry_timeline('TEST', frame.iloc[:120], cfg)
    frame.loc[frame.index[120], 'Low'] = np.nan
    full = build_entry_timeline('TEST', frame, cfg)
    assert full['markers'] == prefix['markers']


def test_open_target_exit_cannot_be_overridden_by_later_intraday_low():
    frame = bars()
    cfg = EntryConfig(setup_threshold=0)
    a = build_entry_timeline('TEST', frame, cfg)
    entry = next(t for t in a['transactions'] if t['held_sessions'] > 0)
    pos = frame.index.get_loc(pd.Timestamp(entry['entry_date']))
    # Use a very large positive open, with later low below any previous stop.
    frame.iloc[pos+1, frame.columns.get_loc('Open')] = 1000.
    frame.iloc[pos+1, frame.columns.get_loc('High')] = 1010.
    frame.iloc[pos+1, frame.columns.get_loc('Low')] = 1.
    frame.iloc[pos+1, frame.columns.get_loc('Close')] = 500.
    result = build_entry_timeline('TEST', frame, cfg)
    trade = next(t for t in result['transactions'] if t['entry_date'] == entry['entry_date'])
    assert trade['exit_price'] == 1000.
    assert trade['exit_reason'] == 'gap_above_target'


def test_chart_sanitizer_cannot_erase_an_earlier_spike():
    from api_server import _sanitize_candle_rows
    values = [100., 100., 1000., 100.]
    candles = [dict(t=i, o=v, c=v, h=v*1.01, l=v*.99, v=1000) for i, v in enumerate(values)]
    assert _sanitize_candle_rows(candles[:3]) == _sanitize_candle_rows(candles)[:3]
    assert _sanitize_candle_rows(candles)[2]['c'] == 1000.


def test_chart_api_does_not_read_legacy_percentile_collector(monkeypatch):
    import api_server
    import market_data_service
    from scripts import research_signal_framework_backtest as legacy
    frame = bars()
    monkeypatch.setattr(api_server, 'cache_get', lambda *_: None)
    monkeypatch.setattr(api_server, 'cache_set', lambda *a, **k: None)
    monkeypatch.setattr(api_server, 'get_daily_history', lambda *a, **k: (frame, 'fixture'))
    monkeypatch.setattr(market_data_service, '_massive_get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden')))
    monkeypatch.setattr(legacy, '_collect_symbol_events', lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy chart path forbidden')))
    result = api_server._single_candles('TEST', 'daily')
    assert result['entry_research']['contract']['version'] == 'pullback_entry_research_v2'
    assert result['entry_research']['contract']['hold_sessions'] == 8
    assert all(m['research_only'] for m in result['markers'])


def test_missing_volume_does_not_become_healthy_liquidity():
    result = build_entry_timeline('TEST', bars().drop(columns='Volume'))
    assert result['status'] == 'unavailable'


def test_pending_signal_is_not_backfilled_with_next_open():
    frame = bars()
    cfg = EntryConfig(setup_threshold=0)
    result = build_entry_timeline('TEST', frame, cfg)
    confirmations = [m for m in result['markers'] if m['stage'] == 'CONFIRMED']
    assert confirmations
    day = confirmations[0]['time']
    prefix = build_entry_timeline('TEST', frame.loc[:day], cfg)
    assert prefix['latest']['stage'] == 'PENDING_ENTRY'
    assert prefix['latest'].get('entry_price') is None
    assert not any(m['stage'] == 'ENTRY' for m in prefix['markers'])


def test_one_position_until_exit_and_explicit_next_open_entry():
    result = build_entry_timeline('TEST', bars(), EntryConfig(setup_threshold=0))
    active = False
    confirmation = None
    for marker in result['markers']:
        if marker['stage'] == 'CONFIRMED': confirmation = marker['time']
        if marker['stage'] == 'ENTRY':
            assert not active
            assert marker['time'] > confirmation
            assert marker['entry_convention'] == 'next_session_open'
            active = True
        if marker['stage'] == 'EXIT':
            assert active
            active = False
    assert all(m['research_only'] for m in result['markers'])
    assert result['validated_profit_probability'] is None


def test_hold_window_is_explicit_and_configurable():
    result = build_entry_timeline('TEST', bars(), EntryConfig(hold_sessions=5, setup_threshold=0))
    assert result['contract']['hold_sessions'] == 5
    assert all(t['held_sessions'] <= 5 for t in result['transactions'])


def test_no_full_sample_quantile_or_outcome_in_signal_contract():
    result = build_entry_timeline('TEST', bars())
    assert result['contract']['threshold_source'] == 'fixed_rule_not_full_sample_quantile'
    for marker in result['markers']:
        assert not {'forward_return', 'resolved', 'future_win', 'exit_date'} & marker.keys()


@pytest.mark.parametrize('horizon', [0, -1, 1.5, True])
def test_invalid_holding_window_rejected(horizon):
    with pytest.raises(ValueError): build_entry_timeline('TEST', bars(), EntryConfig(hold_sessions=horizon))


@pytest.mark.parametrize('missing_index', [56, 57, 58])
def test_missing_session_stops_pending_confirmation_entry_and_position(monkeypatch, missing_index):
    import pullback_entry_research as research
    from pullback_validation import shift_session
    days = [pd.Timestamp(shift_session(pd.Timestamp('2026-01-02').date(), i)) for i in range(75)]
    frame = pd.DataFrame(dict(Open=100., High=102., Low=98., Close=101., Volume=1e6), index=days)
    frame.loc[days[56], ['Open', 'High', 'Low', 'Close']] = [101., 104., 99., 103.]
    frame.loc[days[57], ['Open', 'High', 'Low', 'Close']] = [103., 104., 102., 103.]

    def features(f, weights):
        x = pd.DataFrame(dict(trend_up=True, trend_down=False, cycle_state='up_cycle',
                              pullback_hv_score=0., ma50=99., ma50_slope=1.), index=f.index)
        if days[55] in x.index:
            x.loc[days[55], 'pullback_hv_score'] = 1.
        return x

    monkeypatch.setattr(research, 'feature_history', features)
    full = build_entry_timeline('TEST', frame)
    assert full['transactions']
    broken = frame.drop(days[missing_index])
    out = build_entry_timeline('TEST', broken)
    assert out['status'] == 'unavailable'
    assert out['reason'] == 'missing_session_before_'+str(days[missing_index+1].date())
    assert out['latest']['stage'] == 'DATA_UNKNOWN'
    assert not out['transactions']
    assert out['markers'] == [m for m in full['markers'] if m['time'] < str(days[missing_index].date())]
    before = build_entry_timeline('TEST', frame.iloc[:missing_index])
    assert before['markers'] == out['markers']


def test_weekends_and_official_holidays_do_not_break_contiguous_path(monkeypatch):
    import pullback_entry_research as research
    from pullback_validation import shift_session
    days = [pd.Timestamp(shift_session(pd.Timestamp('2026-01-02').date(), i)) for i in range(80)]
    frame = pd.DataFrame(dict(Open=100., High=102., Low=98., Close=101., Volume=1e6), index=days)
    x = pd.DataFrame(dict(trend_up=True, trend_down=False, cycle_state='up_cycle',
                         pullback_hv_score=1., ma50=99., ma50_slope=1.), index=days)
    monkeypatch.setattr(research, 'feature_history', lambda f, w: x.loc[f.index])
    out = build_entry_timeline('TEST', frame)
    assert out['status'] == 'research_only'
    assert out['latest']['stage'] != 'DATA_UNKNOWN'


def test_chart_cache_is_scoped_to_entry_rule_version(monkeypatch):
    import api_server
    from pullback_entry_research import VERSION
    seen = []
    frame = bars()
    monkeypatch.setattr(api_server, 'cache_get', lambda key: seen.append(key) or None)
    monkeypatch.setattr(api_server, 'cache_set', lambda *a, **k: None)
    monkeypatch.setattr(api_server, 'get_daily_history', lambda *a, **k: (frame, 'fixture'))
    result = api_server._single_candles('TEST', 'daily')
    assert seen[0].startswith('single_candles:v6:'+VERSION+':TEST:daily:')
    assert not any(key.startswith('single_candles:v5:') for key in seen)
    assert result['entry_research']['contract']['version'] == VERSION
