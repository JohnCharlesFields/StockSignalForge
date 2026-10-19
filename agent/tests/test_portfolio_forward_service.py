import copy
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

import app_database as db
import portfolio_forward_service as forward
import prediction_ledger_service as ledger

REAL_RECIPE = forward._recipe
REAL_MANIFEST = forward._manifest


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 16, 21, tzinfo=timezone.utc)]
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'forward.sqlite')
    monkeypatch.setattr(db, '_INITIALIZED', False)
    monkeypatch.setattr(forward, '_now', lambda: clock[0])
    monkeypatch.setattr(db, '_utc_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(forward, '_recipe', lambda: {'version': 'test', 'simulation_enabled': False})
    monkeypatch.setattr(forward, '_manifest', lambda: {'file.py': 'sha-A'})
    return clock


def board(n=2):
    return dict(parameter_version='rank-A', ranking_reference=dict(formula_version='formula-A', configuration={'weight': .5}),
        generated_at='2026-10-19T20:05:00+00:00', snapshot_id='home-A', data_as_of='2026-10-19',
        horizon_days=8, n_candidates=n, picks=[dict(symbol='S'+str(i), price_as_of='2026-10-19', current_price=100.,
            candidate_rank=i+1, is_board_pick=i < 30,
            calibration_references=[dict(method_version='v1', signal_type='pullback_hv', horizon_days=8,
                source='fallback_sigmoid', applied=False)]) for i in range(n)])


def persist(b):
    rows = []
    for rank, p in enumerate(b['picks'], 1):
        if p['price_as_of'] != b['data_as_of']:
            continue
        rows.append(dict(as_of_date=b['data_as_of'], symbol=p['symbol'], rank=rank, horizon_days=8,
            evaluation_version='recorded_open_v3', forecast=dict(snapshot_id=b['snapshot_id'], features=p,
                validation_contract=ledger._validation_snapshot(b, p, 8))))
    return db.predictions_log_many(rows)


def activate(env):
    registration = forward.register()
    env[0] = datetime(2026, 10, 19, 21, tzinfo=timezone.utc)
    return registration


def test_default_does_not_register_scan_or_create_predictions(env):
    assert forward.status()['status'] == 'not_registered'
    assert forward.capture_board({}, '2026-10-19')['status'] == 'not_registered'
    with db.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM predictions').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM portfolio_forward_runs').fetchone()[0] == 0


def test_registration_is_actual_time_next_full_session_and_idempotent(env):
    r = forward.register()
    assert r['first_signal_date'] == '2026-10-19'
    assert r['registered_at'] == env[0].isoformat()
    assert forward.register()['run_id'] == r['run_id']
    assert not forward.status()['simulation_enabled']


def test_even_preopen_registration_cannot_include_same_et_date(env):
    env[0] = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    assert forward.register()['first_signal_date'] == '2026-10-20'


def test_capture_links_all_predictions_and_records_rank_contract(env):
    activate(env)
    b = board()
    assert persist(b) == 2
    result = forward.capture_board(b, b['data_as_of'])
    assert result['roster_complete'] and result['linked_prediction_count'] == 2
    detail = forward.batch_detail(result['batch_id'])
    assert [m['rank'] for m in detail['members']] == [1, 2]
    assert all(m['reference_intact'] for m in detail['members'])
    assert all(m['execution_eligibility'] == 'not_evaluated_capture_only' for m in detail['members'])
    assert detail['contract']['ranking_reference']['configuration'] == {'weight': .5}
    assert forward.status()['collected_signal_days'] == 1
    assert forward.status()['missing_signal_days'] == 0


def test_first_complete_snapshot_and_predictions_survive_reruns(env):
    activate(env)
    b = board()
    persist(b)
    first = forward.capture_board(b, b['data_as_of'])
    before = forward.batch_detail(first['batch_id'])
    b['picks'].reverse()
    b['ranking_reference']['configuration']['weight'] = .9
    persist(b)
    second = forward.capture_board(b, b['data_as_of'])
    assert second['status'] == 'already_recorded'
    assert forward.batch_detail(first['batch_id']) == before


def test_partial_roster_is_not_a_complete_day_but_can_later_get_complete_batch(env):
    activate(env)
    b = board(3)
    persist(b)
    partial = {**b, 'picks': b['picks'][:1]}
    result = forward.capture_board(partial, b['data_as_of'])
    assert result['status'] == 'incomplete_batch_archived'
    assert forward.status()['collected_signal_days'] == 0
    assert forward.status()['missing_signal_days'] == 1
    assert forward.capture_board(b, b['data_as_of'])['roster_complete']
    assert len(forward.status()['recent_batches']) == 2


def test_stale_member_is_archived_as_exclusion_not_fresh_prediction(env):
    activate(env)
    b = board()
    b['picks'][1]['price_as_of'] = '2026-10-16'
    assert persist(b) == 1
    result = forward.capture_board(b, b['data_as_of'])
    assert result['roster_complete'] and result['linked_prediction_count'] == 1
    member = forward.batch_detail(result['batch_id'])['members'][1]
    assert member['prediction_id'] is None
    assert member['quality_flags'] == ['stale_or_unverified_price']
    assert member['unlinked_evidence']['price_as_of'] == '2026-10-16'


@pytest.mark.parametrize('change', ['missing', 'backfill', 'wrong_rank', 'wrong_features'])
def test_does_not_adopt_missing_backfilled_or_conflicting_old_forecast(env, change):
    activate(env)
    b = board(1)
    if change != 'missing':
        persist(b)
        with db.connection() as c:
            if change == 'backfill':
                c.execute("UPDATE predictions SET mode='backfill'")
            elif change == 'wrong_rank':
                c.execute('UPDATE predictions SET rank=99')
            else:
                row = c.execute('SELECT forecast_json FROM predictions').fetchone()
                payload = json.loads(row[0])
                payload['features']['current_price'] = 999
                c.execute('UPDATE predictions SET forecast_json=?', (json.dumps(payload),))
    result = forward.capture_board(b, b['data_as_of'])
    assert not result['roster_complete']
    assert result['quality_flags']['missing_or_conflicting_immutable_prediction'] == 1


def test_ledger_tampering_is_visible_when_hydrating_archived_reference(env):
    activate(env)
    b = board(1)
    persist(b)
    result = forward.capture_board(b, b['data_as_of'])
    with db.connection() as c:
        c.execute("UPDATE predictions SET forecast_json='{}'")
    item = forward.batch_detail(result['batch_id'])['members'][0]
    assert not item['reference_intact'] and item['forecast'] is None


@pytest.mark.parametrize('field,value', [('data_as_of', '2026-10-16'),
    ('generated_at', '2026-10-19T19:00:00+00:00'), ('generated_at', '2026-10-20T20:05:00+00:00'),
    ('generated_at', '2026-10-19T20:05:00')])
def test_bad_or_future_or_naive_board_timestamps_are_not_recorded(env, field, value):
    activate(env)
    b = board(1)
    b[field] = value
    assert forward.capture_board(b, '2026-10-19')['status'] == 'unverified_board_time_or_price_date'


def test_prior_days_cannot_be_backfilled(env):
    activate(env)
    assert forward.capture_board(board(), '2026-10-16')['status'] == 'before_registration_window'
    env[0] = datetime(2026, 10, 20, 21, tzinfo=timezone.utc)
    assert forward.capture_board(board(), '2026-10-19')['status'] == 'not_latest_completed_session'


def test_code_or_cost_change_requires_new_registration_not_mixed_cohort(env, monkeypatch):
    activate(env)
    monkeypatch.setattr(forward, '_manifest', lambda: {'file.py': 'sha-B'})
    assert forward.capture_board(board(), '2026-10-19')['status'] == 'registered_code_or_cost_changed'
    assert not forward.status()['capture_ready']
    assert forward.status()['registration_block_reason'] == 'registered_code_or_cost_changed'


def test_real_recipe_and_source_manifest_can_register_and_capture_without_external_data(env, monkeypatch):
    monkeypatch.setattr(forward, '_recipe', REAL_RECIPE)
    monkeypatch.setattr(forward, '_manifest', REAL_MANIFEST)
    recipe, manifest = forward._recipe(), forward._manifest()
    assert recipe['config']['initial_cash'] == 100000
    assert recipe['config']['hold_sessions'] == 8
    assert not recipe['simulation_enabled']
    assert 'trade_risk_rules.py' in manifest
    assert all(len(value) == 64 for value in manifest.values())
    activate(env)
    assert forward.status()['capture_ready']
    b = board()
    persist(b)
    result = forward.capture_board(b, b['data_as_of'])
    assert result['roster_complete'] and result['linked_prediction_count'] == 2
    assert all(m['reference_intact'] for m in forward.batch_detail(result['batch_id'])['members'])
    assert forward.pause()['status'] == 'paused'
    assert not forward.status()['capture_ready']


def test_pause_does_not_delete_old_registration_or_allow_more_capture(env):
    activate(env)
    assert forward.pause()['status'] == 'paused'
    assert forward.capture_board(board(), '2026-10-19')['status'] == 'not_registered'
    assert forward.status()['status'] == 'paused'


def test_zero_is_explicit_empty_roster_not_missing_data(env):
    activate(env)
    result = forward.capture_board(board(0), '2026-10-19')
    assert result['roster_complete'] and result['member_count'] == 0
    assert forward.capture_board({}, '2026-10-19')['status'] == 'unverified_board_time_or_price_date'


def test_concurrent_registration_and_capture_are_idempotent(env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        registrations = list(pool.map(lambda _: forward.register(), range(2)))
    assert registrations[0]['run_id'] == registrations[1]['run_id']
    env[0] = datetime(2026, 10, 19, 21, tzinfo=timezone.utc)
    b = board(1)
    persist(b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: forward.capture_board(b, '2026-10-19'), range(2)))
    assert sum(r['recorded'] for r in results) == 1


def test_batch_reads_predictions_in_one_query_not_per_symbol(env, monkeypatch):
    activate(env)
    b = board(1001)
    persist(b)
    statements = []
    original = db._connect
    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(db, '_connect', connect)
    result = forward.capture_board(b, '2026-10-19')
    assert result['linked_prediction_count'] == 1001
    reads = [s for s in statements if s.startswith('SELECT prediction_id,symbol')]
    assert len(reads) == 1


def stub_board(env, monkeypatch, b):
    import market_calendar
    monkeypatch.setattr(market_calendar, 'most_recent_session', lambda now=None: datetime(2026, 10, 19).date())
    monkeypatch.setitem(sys.modules, 'priority_board_service', SimpleNamespace(
        CACHE_KEY='board', compute_priority_board=lambda **kw: pytest.fail('no recompute allowed')))
    monkeypatch.setattr(ledger, 'cache_get', lambda _: b)
    monkeypatch.setattr(ledger, '_bust_scorecard_cache', lambda: None)


def test_existing_daily_log_captures_after_forecast_rows_are_durable(env, monkeypatch):
    activate(env)
    b = board()
    stub_board(env, monkeypatch, b)
    result = ledger.log_predictions(1)
    assert result['logged_new'] == 2
    assert result['portfolio_forward']['roster_complete']
    assert result['portfolio_forward']['linked_prediction_count'] == 2
    assert forward.status()['collected_signal_days'] == 1


def test_capture_failure_cannot_fail_successful_daily_ledger_or_leak_error(env, monkeypatch):
    activate(env)
    stub_board(env, monkeypatch, board())
    def fail(*args):
        raise RuntimeError('API_KEY=private-secret')
    monkeypatch.setattr(forward, 'capture_board', fail)
    result = ledger.log_predictions()
    assert result['logged_new'] == 2 and result['slice_rows_written'] == 2
    assert result['portfolio_forward'] == dict(status='capture_failed', recorded=False, error_type='RuntimeError')
    assert 'private-secret' not in json.dumps(result)


def test_top_n_logging_never_claims_a_complete_roster(env, monkeypatch):
    activate(env)
    stub_board(env, monkeypatch, board())
    result = ledger.log_predictions(1, log_all_candidates=False)
    assert result['candidates'] == 1
    assert result['portfolio_forward']['status'] == 'top_n_only_not_captured'
    assert forward.status()['collected_signal_days'] == 0


@pytest.mark.parametrize('method,path', [('post', '/portfolio-forward/register'),
    ('post', '/portfolio-forward/pause'), ('get', '/portfolio-forward/status'),
    ('get', '/portfolio-forward/batches/unknown/signal-evidence'),
    ('get', '/portfolio-forward/batches/unknown')])
def test_private_forward_endpoints_reject_anonymous_remote_access(env, monkeypatch, method, path):
    from fastapi.testclient import TestClient
    import api_server as api
    monkeypatch.delenv('API_AUTH_KEY', raising=False)
    monkeypatch.setenv('VIBE_TRADING_TRUST_TUNNEL', '1')
    monkeypatch.setattr(api, '_API_KEY', '')
    client = TestClient(api.app, client=('192.0.2.1', 40000))
    assert getattr(client, method)(path).status_code == 403


def test_local_api_register_status_and_pause_do_not_accept_backdated_registration(env, monkeypatch):
    from fastapi.testclient import TestClient
    import api_server as api
    monkeypatch.delenv('API_AUTH_KEY', raising=False)
    monkeypatch.setattr(api, '_API_KEY', '')
    client = TestClient(api.app, base_url='http://127.0.0.1:19090', client=('127.0.0.1', 40000))
    response = client.post('/portfolio-forward/register', json={'registered_at': '2000-01-01'})
    assert response.status_code == 200
    assert response.json()['registered_at'] == env[0].isoformat()
    assert client.get('/portfolio-forward/status').json()['status'] == 'registered'
    assert client.post('/portfolio-forward/pause').json()['status'] == 'paused'
    assert client.get('/portfolio-forward/batches/unknown').status_code == 404


def test_foreign_origin_cannot_register_a_local_experiment(env, monkeypatch):
    from fastapi.testclient import TestClient
    import api_server as api
    monkeypatch.delenv('API_AUTH_KEY', raising=False)
    monkeypatch.setattr(api, '_API_KEY', '')
    response = TestClient(api.app, client=('127.0.0.1', 40000)).post(
        '/portfolio-forward/register', headers={'Origin': 'https://synthetic.invalid'})
    assert response.status_code == 403
    assert forward.status()['status'] == 'not_registered'
