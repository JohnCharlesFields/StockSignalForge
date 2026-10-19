from datetime import date, datetime, timezone
import json

import numpy as np
import pandas as pd
import pytest

import app_database as db
import market_data_service as market
from market_calendar import trading_days_back
import portfolio_forward_service as forward
import portfolio_signal_evidence as signals
from test_portfolio_forward_service import env, activate, board, persist


def bars(n=110):
    index = pd.DatetimeIndex(trading_days_back(n, date(2026, 10, 19)), name='Date')
    return pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1e6), index=index)


def controlled(monkeypatch, side='LONG', overlap=False):
    f = bars()
    f.iloc[-1] = [100, 103, 99.5, 102, 1e6] if side == 'LONG' else [100, 100.5, 97, 98, 1e6]
    def long_features(frame):
        out = pd.DataFrame(dict(pullback_hv_score=0., trend_up=side == 'LONG', cycle_state='up_cycle'), index=frame.index)
        out.loc[f.index[-2], 'pullback_hv_score'] = .7
        if overlap:
            out.loc[f.index[-3], 'pullback_hv_score'] = .7
        return out
    def short_features(frame, config):
        out = pd.DataFrame(dict(setup=False, trend_down=True, cycle_state='down_cycle'), index=frame.index)
        out.loc[f.index[-2], 'setup'] = True
        return out
    monkeypatch.setattr(signals, 'feature_history', long_features)
    monkeypatch.setattr(signals, 'short_feature_history', short_features)
    return f


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_raw_confirmation_has_no_fill_or_future_return(monkeypatch, side):
    result = signals.decision_evidence('TEST', controlled(monkeypatch, side), '2026-10-19')
    assert result['status'] == 'CONFIRMED'
    assert [s['side'] for s in result['new_confirmations']] == [side]
    assert result['new_confirmations'][0]['stop_reference'] == (99 if side == 'LONG' else 101)
    assert not any(k in json.dumps(result) for k in ('entry_price', 'exit_price', 'gross_return', 'win_rate'))
    assert not result['simulation_enabled']


def test_overlapping_setups_are_not_suppressed_by_a_shadow_position(monkeypatch):
    result = signals.decision_evidence('TEST', controlled(monkeypatch, overlap=True), '2026-10-19')
    assert len(result['new_confirmations']) == 2
    assert len({s['opportunity_id'] for s in result['new_confirmations']}) == 2


@pytest.mark.parametrize('symbol', sorted(signals.MAG7))
def test_mag7_never_emits_short_confirmation(monkeypatch, symbol):
    result = signals.decision_evidence(symbol, controlled(monkeypatch, 'SHORT'), '2026-10-19')
    assert result['short_excluded']
    assert not any(s['side'] == 'SHORT' for s in result['setups'])


def test_mag7_exclusion_is_case_insensitive(monkeypatch):
    result = signals.decision_evidence(' aapl ', controlled(monkeypatch, 'SHORT'), '2026-10-19')
    assert result['short_excluded'] and not result['new_confirmations']


def test_downward_midterm_never_confirms_long(monkeypatch):
    f = controlled(monkeypatch)
    original = signals.feature_history
    def down(frame):
        return original(frame).assign(trend_up=False, cycle_state='down_cycle')
    monkeypatch.setattr(signals, 'feature_history', down)
    assert not signals.decision_evidence('TEST', f, '2026-10-19')['new_confirmations']


def test_structure_break_before_cross_cannot_confirm(monkeypatch):
    f = controlled(monkeypatch)
    f.iloc[-1, f.columns.get_loc('Low')] = 98
    result = signals.decision_evidence('TEST', f, '2026-10-19')
    assert not result['new_confirmations']
    assert any(s['stage'] == 'INVALIDATED' for s in result['setups'])


def test_real_features_future_append_and_shock_do_not_change_prefix():
    rng = np.random.default_rng(20261019)
    f = bars(310)
    f.Close = 100*np.exp(np.cumsum(rng.normal(.001, .02, len(f))))
    f.Open, f.High, f.Low = f.Close*.997, f.Close*1.02, f.Close*.98
    day = str(f.index[-6].date())
    prefix = signals.decision_evidence('TEST', f.iloc[:-5], day)
    assert prefix['status'] != 'DATA_UNKNOWN'
    assert signals.decision_evidence('TEST', f, day) == prefix
    f.iloc[-5:, :4] = np.nan
    assert signals.decision_evidence('TEST', f, day) == prefix


def test_unverified_split_or_price_jump_cannot_create_a_confirmation():
    f = bars()
    f.iloc[-1, :4] *= 4
    result = signals.decision_evidence('TEST', f, '2026-10-19')
    assert result['status'] == 'DATA_UNKNOWN'
    assert result['reason'] == 'unverified_price_jump_or_corporate_action'


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'zero_price', 'nan', 'weekend', 'intraday', 'stale', 'zero_volume'])
def test_bad_input_is_unknown_not_no_signal(change):
    f = bars()
    if change == 'missing':
        f = f.drop(f.index[-3])
    elif change == 'duplicate':
        f = pd.concat([f, f.iloc[-2:-1]])
    elif change == 'zero_price':
        f.iloc[-1, 0] = 0
    elif change == 'nan':
        f.iloc[-1, 1] = np.nan
    elif change == 'weekend':
        f = pd.concat([f, f.iloc[-2:-1].set_axis(pd.DatetimeIndex(['2026-10-18']))])
    elif change == 'intraday':
        f.index = f.index + pd.Timedelta(hours=1)
    elif change == 'stale':
        f = f.iloc[:-1]
    else:
        f.Volume = 0
    assert signals.decision_evidence('TEST', f, '2026-10-19')['status'] == 'DATA_UNKNOWN'


def test_open_timing_exact_boundary_and_weekend():
    for stamp, expected in [('2026-10-19T13:29:59+00:00', 'awaiting_next_open'),
                            ('2026-10-19T13:30:00+00:00', 'missed_next_open'),
                            ('2026-10-19T18:00:00+00:00', 'missed_next_open')]:
        result = signals.entry_timing('2026-10-16', datetime.fromisoformat(stamp))
        assert result['entry_timing'] == expected
        assert result['next_session_open_at'] == '2026-10-19T13:30:00+00:00'
        assert result['fill_price'] is None
    with pytest.raises(ValueError):
        signals.entry_timing('2026-10-16', datetime(2026, 10, 19))
    with pytest.raises(ValueError, match='not_yet_complete'):
        signals.entry_timing('2026-10-19', datetime(2026, 10, 19, 19, tzinfo=timezone.utc))


def write_cache(monkeypatch, tmp_path, f, symbol='S0'):
    monkeypatch.setattr(market, '_CACHE_ROOT', tmp_path)
    folder = tmp_path/'ohlcv_daily'
    folder.mkdir(exist_ok=True)
    f.to_csv(folder/(symbol+'.csv'))


def test_snapshot_reader_preserves_bad_values_and_never_fetches(monkeypatch, tmp_path):
    f = bars()
    f.iloc[-1, 1] = np.nan
    write_cache(monkeypatch, tmp_path, f)
    monkeypatch.setattr(market.requests, 'get', lambda *a, **k: pytest.fail('no requests permitted'))
    frame, provenance = market.read_daily_cache_snapshot('S0')
    assert pd.isna(frame.High.iloc[-1])
    assert provenance['provider'] is None and provenance['adjustment'] == 'unverified'
    assert len(provenance['content_sha256']) == 64
    with pytest.raises(ValueError):
        market.read_daily_cache_snapshot('../../private')


def captured(env):
    activate(env)
    b = board(1)
    persist(b)
    return forward.capture_board(b, b['data_as_of'])['batch_id']


def test_evidence_is_durable_complete_and_idempotent(env, monkeypatch, tmp_path):
    batch_id = captured(env)
    write_cache(monkeypatch, tmp_path, controlled(monkeypatch))
    result = forward.capture_signal_evidence(batch_id)
    assert result['recorded'] and result['counts'] == {'CONFIRMED': 1}
    assert forward.status()['evidence_collected_signal_days'] == 1
    detail = forward.signal_evidence_detail(batch_id)
    assert detail['reference_intact']
    evidence = detail['evidence']
    assert evidence['calendar']['coverage'] == 'unknown'
    assert evidence['timing']['available_at'] == env[0].isoformat()
    assert evidence['timing']['fill_price'] is None
    assert not evidence['simulation_enabled']
    monkeypatch.setattr(market, 'read_daily_cache_snapshot', lambda *a: pytest.fail('already archived, no reread'))
    assert forward.capture_signal_evidence(batch_id)['status'] == 'already_recorded'
    assert forward.signal_evidence_detail(batch_id) == detail


def test_local_evidence_endpoint_reads_archive_without_running_capture(env, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api_server as api
    batch_id = captured(env)
    write_cache(monkeypatch, tmp_path, controlled(monkeypatch))
    forward.capture_signal_evidence(batch_id)
    monkeypatch.setattr(api, '_API_KEY', '')
    monkeypatch.delenv('API_AUTH_KEY', raising=False)
    monkeypatch.setattr(forward, 'capture_signal_evidence', lambda *a: pytest.fail('read must not recapture'))
    client = TestClient(api.app, base_url='http://127.0.0.1:19090', client=('127.0.0.1', 40000))
    assert client.get('/portfolio-forward/batches/'+batch_id+'/signal-evidence').json()['reference_intact']
    assert client.get('/portfolio-forward/batches/missing/signal-evidence').status_code == 404


def test_missing_cache_and_tampered_reference_are_visible_not_zero_return(env):
    batch_id = captured(env)
    with db.connection() as conn:
        conn.execute("UPDATE predictions SET forecast_json='{}'")
    result = forward.capture_signal_evidence(batch_id)
    assert result['counts'] == {'DATA_UNKNOWN': 1}
    member = forward.signal_evidence_detail(batch_id)['evidence']['members'][0]
    assert member['reason'] == 'unverified_forecast_reference_or_price_date'
    with db.connection() as conn:
        conn.execute("UPDATE portfolio_forward_evidence SET payload_json='{}'")
    assert not forward.signal_evidence_detail(batch_id)['reference_intact']


def test_missing_cache_is_unknown_and_successful_roster_is_preserved(env, monkeypatch, tmp_path):
    batch_id = captured(env)
    monkeypatch.setattr(market, '_CACHE_ROOT', tmp_path)
    assert forward.capture_signal_evidence(batch_id)['counts'] == {'DATA_UNKNOWN': 1}
    evidence = forward.signal_evidence_detail(batch_id)['evidence']
    assert evidence['members'][0]['error_type'] == 'FileNotFoundError'
    assert forward.batch_detail(batch_id)['roster_complete']


def test_signal_evidence_failure_does_not_revoke_daily_roster(env, monkeypatch):
    from test_portfolio_forward_service import stub_board
    import prediction_ledger_service as ledger
    activate(env)
    stub_board(env, monkeypatch, board(1))
    def fail(*args):
        raise RuntimeError('secret-must-not-leak')
    monkeypatch.setattr(forward, 'capture_signal_evidence', fail)
    result = ledger.log_predictions()
    assert result['logged_new'] == 1 and result['portfolio_forward']['roster_complete']
    assert result['portfolio_forward']['signal_evidence']['error_type'] == 'RuntimeError'
    assert 'secret-must-not-leak' not in json.dumps(result)


def test_time_budget_preserves_unprocessed_members_as_unknown(env, monkeypatch):
    batch_id = captured(env)
    timer = iter([0, 21, 22])
    monkeypatch.setattr(forward.time, 'monotonic', lambda: next(timer))
    result = forward.capture_signal_evidence(batch_id)
    assert result['counts'] == {'DATA_UNKNOWN': 1}
    assert forward.signal_evidence_detail(batch_id)['evidence']['members'][0]['reason'] == 'capture_time_budget_exhausted'


def test_pause_during_capture_prevents_late_commit(env, monkeypatch, tmp_path):
    batch_id = captured(env)
    write_cache(monkeypatch, tmp_path, bars())
    original = market.read_daily_cache_snapshot
    def pause(symbol):
        forward.pause()
        return original(symbol)
    monkeypatch.setattr(market, 'read_daily_cache_snapshot', pause)
    assert forward.capture_signal_evidence(batch_id)['status'] == 'registration_paused_during_capture'
    assert forward.signal_evidence_detail(batch_id) is None


def test_calendar_only_archives_already_known_versions_without_assuming_coverage(env):
    with db.connection() as conn:
        conn.execute('CREATE TABLE trading_calendar_events(category TEXT,payload_json TEXT,first_seen_at TEXT,updated_at TEXT)')
        conn.executemany('INSERT INTO trading_calendar_events VALUES(?,?,?,?)', [
            ('earnings', json.dumps(dict(event_id='e1', symbols=[dict(symbol='S0', relation_type='subject')])),
             '2026-10-16T20:00:00+00:00', '2026-10-16T20:00:00+00:00'),
            ('macro', '{}', '2026-10-20T20:00:00+00:00', '2026-10-20T20:00:00+00:00')])
    result = forward._calendar_evidence(datetime(2026, 10, 19, 21, tzinfo=timezone.utc))
    assert len(result['events']) == 1
    assert result['events'][0]['symbols'][0]['symbol'] == 'S0'
    assert result['omitted_invalid_versions'] == 1 and result['coverage'] == 'unknown'
