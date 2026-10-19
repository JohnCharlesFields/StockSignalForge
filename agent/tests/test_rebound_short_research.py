"""Causal short-direction research, not borrow availability or Put profits."""
import numpy as np
import pandas as pd
import pytest

import rebound_short_research as short
from test_pullback_entry_research import bars


def controlled(monkeypatch, *, hold=3):
    frame = bars(90, down=True)
    frame.loc[:, ['Open', 'Close']] = 100.
    frame['High'], frame['Low'] = 100.5, 99.
    frame.iloc[55, frame.columns.get_loc('High')] = 101.
    frame.iloc[56] = [100., 101., 97., 98., 1_000_000.]
    frame.iloc[57] = [98., 99., 97., 98., 1_000_000.]

    def features(f, config=None):
        out = pd.DataFrame({'setup': False, 'trend_down': True, 'cycle_state': 'down_cycle',
                            'ma50': 110., 'ma50_slope': -1., 'adv': 100_000_000.,
                            'atr14': 2.}, index=f.index)
        if len(f) > 55:
            out.iloc[55, out.columns.get_loc('setup')] = True
        return out
    monkeypatch.setattr(short, 'short_feature_history', features)
    return frame, short.ShortEntryConfig(hold_sessions=hold)


def test_confirm_then_next_open_not_same_close(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    prefix = short.build_short_timeline('T', frame.iloc[:57], cfg)
    assert prefix['latest']['stage'] == 'PENDING_ENTRY'
    assert prefix['latest']['entry_price'] is None
    assert not any(m['stage'] == 'ENTRY' for m in prefix['markers'])
    full = short.build_short_timeline('T', frame, cfg)
    entry = next(m for m in full['markers'] if m['stage'] == 'ENTRY')
    assert entry['time'] == str(frame.index[57].date())
    assert entry['price'] == 98.
    assert entry['action'] == 'OPEN_SHORT'
    assert all(m['direction'] == 'SHORT' and m['research_only'] for m in full['markers'])
    assert {m['text'] for m in full['markers']} >= {'空观', '空确', '空入', '回补'}


def test_short_pnl_uses_entry_not_exit_denominator(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[58] = [95., 96., 91., 93., 1_000_000.]
    trade = short.build_short_timeline('T', frame, cfg)['transactions'][0]
    assert trade['exit_price'] == 92.  # 98 - 2 * (101 - 98)
    assert trade['gross_directional_return'] == pytest.approx(6/98)
    assert trade['return_kind'] == 'underlying_short_proxy'
    assert trade['borrow_verified'] is False


def test_stop_first_if_both_barriers_touched(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[58] = [98., 102., 90., 96., 1_000_000.]
    trade = short.build_short_timeline('T', frame, cfg)['transactions'][0]
    assert trade['exit_price'] == 101.
    assert trade['exit_reason'] == 'stop_first_conservative'


def test_open_target_happens_before_later_intraday_stop(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[58] = [90., 110., 89., 105., 1_000_000.]
    trade = short.build_short_timeline('T', frame, cfg)['transactions'][0]
    assert trade['exit_price'] == 90.
    assert trade['exit_reason'] == 'gap_below_target'


def test_gap_stop_not_assumed_filled_at_stop(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[58] = [110., 111., 108., 109., 1_000_000.]
    trade = short.build_short_timeline('T', frame, cfg)['transactions'][0]
    assert trade['exit_price'] == 110.
    assert trade['gross_directional_return'] < 0


def test_next_open_above_invalidation_cancels_entry(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[57] = [102., 103., 101., 102., 1_000_000.]
    result = short.build_short_timeline('T', frame, cfg)
    assert not result['transactions']
    assert not any(m['stage'] == 'ENTRY' for m in result['markers'])
    assert result['markers'][-1]['text'] == '空失'


def test_confirmation_cannot_ignore_higher_high_same_bar(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    frame.iloc[56] = [100., 102., 97., 98., 1_000_000.]
    result = short.build_short_timeline('T', frame, cfg)
    assert not any(m['stage'] == 'CONFIRMED' for m in result['markers'])


def test_one_short_until_cover_and_time_exit(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    result = short.build_short_timeline('T', frame, cfg)
    assert len(result['transactions']) == 1
    assert result['transactions'][0]['held_sessions'] == 3
    assert result['transactions'][0]['exit_reason'] == 'time_exit_at_open'
    assert len([m for m in result['markers'] if m['stage'] == 'ENTRY']) == 1
    assert next(m for m in result['markers'] if m['stage'] == 'EXIT')['action'] == 'COVER_SHORT'


def test_real_rejection_features_require_downtrend_and_pressure():
    frame = bars(100, down=True)
    frame['Close'] = np.linspace(200, 100, len(frame))
    frame['Open'], frame['High'], frame['Low'] = frame.Close, frame.Close+1, frame.Close-1
    # An actual rejection around MA10 in a downtrend; no future outcome used.
    frame.iloc[70] = [130., 134., 128., 129., 1_000_000.]
    features = short.short_feature_history(frame)
    assert features.iloc[70]['setup']
    assert not features.iloc[69]['setup']  # falling tape alone is not a rebound rejection
    result = short.build_short_timeline('T', bars(100))
    assert not any(m['stage'] in {'CONFIRMED', 'ENTRY'} for m in result['markers'])


@pytest.mark.parametrize('cut', [60, 80, 120])
def test_real_prefix_and_future_shock_do_not_rewrite_markers(cut):
    frame = bars(180, down=True)
    prefix = short.build_short_timeline('T', frame.iloc[:cut])
    full = short.build_short_timeline('T', frame)
    day = str(frame.index[cut-1].date())
    assert prefix['markers'] == [m for m in full['markers'] if m['time'] <= day]
    assert prefix['states'] == full['states'][:cut]
    frame.iloc[cut:, :4] *= 4
    changed = short.build_short_timeline('T', frame)
    assert prefix['markers'] == [m for m in changed['markers'] if m['time'] <= day]


def test_causal_sequence_has_nonempty_markers(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    full = short.build_short_timeline('T', frame, cfg)
    for cut in range(55, 63):
        prefix = short.build_short_timeline('T', frame.iloc[:cut], cfg)
        day = str(frame.index[cut-1].date())
        assert prefix['markers'] == [m for m in full['markers'] if m['time'] <= day]


def test_invalid_future_does_not_erase_known_marks(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    before = short.build_short_timeline('T', frame.iloc[:65], cfg)
    frame.iloc[65, frame.columns.get_loc('Low')] = np.nan
    after = short.build_short_timeline('T', frame, cfg)
    assert before['markers'] == after['markers']
    assert after['latest']['stage'] == 'DATA_UNKNOWN'


@pytest.mark.parametrize('hold', [0, -1, 1.2, True])
def test_bad_hold_rejected(hold):
    with pytest.raises(ValueError):
        short.build_short_timeline('T', bars(), short.ShortEntryConfig(hold_sessions=hold))


def test_no_future_outcomes_or_claimed_probability_in_marks(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    result = short.build_short_timeline('T', frame, cfg)
    assert result['validated_profit_probability'] is None
    assert not result['contract']['calibrated']
    for marker in result['markers']:
        assert not {'exit_date', 'future_win', 'forward_return', 'probability'} & marker.keys()


def test_chart_contains_separate_short_metadata_no_provider(monkeypatch):
    import api_server
    import market_data_service
    frame = bars(down=True)
    monkeypatch.setattr(api_server, 'cache_get', lambda *_: None)
    monkeypatch.setattr(api_server, 'cache_set', lambda *a, **k: None)
    monkeypatch.setattr(api_server, 'get_daily_history', lambda *a, **k: (frame, 'fixture'))
    monkeypatch.setattr(market_data_service, '_massive_get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no provider')))
    response = api_server._single_candles('T', 'daily')
    assert response['short_entry_research']['contract']['version'] == short.VERSION
    assert response['entry_research']['contract']['version'] == 'pullback_entry_research_v2'
    assert response['short_entry_research']['contract']['hold_sessions'] == 8


def test_offline_cost_and_borrow_sensitivity_are_not_option_profit():
    from scripts.validate_rebound_short import price_proxy
    result = price_proxy(dict(entry_price=100., exit_price=90., adv=100_000_000.,
                              entry_date='2026-01-02', exit_date='2026-01-12'))
    assert result['gross_directional_return'] == pytest.approx(.1)
    assert result['borrow_5pct_return'] == pytest.approx(result['net_return']-.05*10/365)
    assert result['borrow_20pct_return'] < result['borrow_5pct_return'] < result['net_return']


def test_missing_session_during_position_stops_replay(monkeypatch):
    frame, cfg = controlled(monkeypatch)
    prefix = short.build_short_timeline('T', frame.iloc[:58], cfg)
    full = short.build_short_timeline('T', frame.drop(frame.index[58]), cfg)
    assert full['status'] == 'unavailable'
    assert full['latest']['stage'] == 'DATA_UNKNOWN'
    assert full['markers'] == prefix['markers']


def test_zero_range_or_volume_does_not_create_setup():
    frame = bars(100, down=True)
    frame.loc[:, ['Open', 'High', 'Low', 'Close']] = 100.
    assert not short.short_feature_history(frame)['setup'].any()
    frame['Volume'] = 0
    assert not short.build_short_timeline('T', frame)['markers']


def test_official_mourning_closure_is_not_a_missing_session():
    from datetime import date
    from market_calendar import is_trading_day
    from pullback_validation import shift_session
    assert not is_trading_day(date(2025, 1, 9))
    assert shift_session(date(2025, 1, 8), 1) == date(2025, 1, 10)
    assert is_trading_day(date(2025, 1, 10))
