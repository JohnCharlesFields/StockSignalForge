import copy
import json
from pathlib import Path

import pytest

import app_database as db
import prediction_ledger_service as ledger


def board():
    return {
        'parameter_version': 'rank-A',
        'ranking_reference': {'formula_version': 'formula-A', 'configuration': {'version': 'config-A', 'oversold_weight': .5}, 'active_parameters': None},
        'generated_at': '2026-10-14T03:00:00+00:00',
        'snapshot_id': 'home-A',
    }


def pick():
    return {'calibration_references': [{'signal_type': 'pullback_hv', 'horizon_days': 8, 'source': 'calibrated',
                                      'calibration_id': 'curve-A', 'generated_at': '2026-06-21', 'method_version': 'calib_v1', 'applied': True}]}


def recorded(version='rank-A', horizon=8, resolved=True):
    b = {**board(), 'parameter_version': version}
    p = pick()
    p['calibration_references'][0]['horizon_days'] = horizon
    return {'as_of_date': '2026-10-14', 'symbol': 'A', 'horizon_days': horizon, 'rank': 1,
            'calibrated_prob': .8, 'evaluation_version': 'recorded_open_v3', 'resolved': resolved,
            'win': 1, 'net_excess': .02, 'net_return': .03,
            'forecast_json': json.dumps({'validation_contract': ledger._validation_snapshot(b, p, horizon)})}


def test_snapshot_uses_board_time_identity_not_current_registry(monkeypatch):
    monkeypatch.setattr(ledger, 'signal_calibration_list', lambda **kw: pytest.fail('must not query current curves'))
    original = board()
    p = pick()
    result = ledger._validation_snapshot(original, p, 8)
    original['ranking_reference']['configuration']['oversold_weight'] = .9
    p['calibration_references'][0]['calibration_id'] = 'future'
    assert result['ranking_reference']['configuration']['oversold_weight'] == .5
    assert result['calibration_references'][0]['calibration_id'] == 'curve-A'
    assert result['probability_target'] != result['outcome_target']


def test_missing_snapshot_is_unknown_not_filled_from_live_configuration():
    result = ledger._validation_snapshot({}, {}, 8)
    assert result['ranking_version'] is None
    assert result['calibration_references'] == []
    assert result['metadata_complete'] is False


def test_cohorts_separate_versions_horizons_and_keep_pending():
    rows = [recorded(), recorded('rank-B'), recorded(horizon=5), recorded(resolved=False)]
    cohorts = ledger._version_cohorts(rows)
    assert len(cohorts) == 3
    eight_a = next(c for c in cohorts if c['ranking_version'] == 'rank-A' and c['horizon_days'] == 8)
    assert (eight_a['total'], eight_a['resolved'], eight_a['pending']) == (2, 1, 1)
    assert eight_a['mean_net_return'] == .03
    assert eight_a['predicted_win_rate'] is None and eight_a['brier'] is None


@pytest.mark.parametrize('field,value', [('ranking_version', 'rank-B'), ('outcome_target', 'other-target')])
def test_contract_changes_produce_distinct_cohorts(field, value):
    first = recorded()
    second = copy.deepcopy(first)
    forecast = json.loads(second['forecast_json'])
    forecast['validation_contract'][field] = value
    second['forecast_json'] = json.dumps(forecast)
    assert len(ledger._version_cohorts([first, second])) == 2


def test_curve_identity_change_is_not_pooled():
    first = recorded()
    second = copy.deepcopy(first)
    forecast = json.loads(second['forecast_json'])
    forecast['validation_contract']['calibration_references'][0]['calibration_id'] = 'curve-B'
    second['forecast_json'] = json.dumps(forecast)
    assert len(ledger._version_cohorts([first, second])) == 2


def test_collecting_on_different_dates_does_not_change_frozen_version():
    first = recorded()
    second = copy.deepcopy(first)
    second['as_of_date'] = '2026-10-15'
    forecast = json.loads(second['forecast_json'])
    forecast['validation_contract']['board_generated_at'] = '2026-10-15T03:00:00+00:00'
    second['forecast_json'] = json.dumps(forecast)
    assert len(ledger._version_cohorts([first, second])) == 1


def test_wrong_curve_horizon_does_not_claim_complete_metadata():
    assert ledger._validation_snapshot(board(), pick(), 5)['metadata_complete'] is False


def test_live_and_backfill_are_never_pooled_in_any_mode():
    row = recorded()
    assert len(ledger._version_cohorts([{**row, 'mode': 'live'}, {**row, 'mode': 'backfill'}])) == 2


def test_calibration_reference_tracks_actual_probability_and_fallback(monkeypatch):
    import signal_calibration as cal
    monkeypatch.setattr(db, 'signal_calibration_active', lambda *args: {'calibration_id': 'original', 'source': 'replay', 'curve': {}})
    monkeypatch.setattr(cal, 'apply_calibration', lambda *args, **kwargs: {'support_ok': True, 'p_calibrated': .7})
    reference = cal.calibrate('pullback_hv', .8, 8)['calibration_reference']
    assert reference['calibration_id'] == 'original' and reference['applied'] is True
    assert reference['source'] == 'calibrated'
    monkeypatch.setattr(cal, 'apply_calibration', lambda *args, **kwargs: {'support_ok': False, 'p_calibrated': None})
    reference = cal.calibrate('pullback_hv', .8, 8)['calibration_reference']
    assert reference['source'] == 'fallback_sigmoid' and reference['applied'] is False


@pytest.mark.parametrize('raw', [None, '', '{broken', '[]', '{"validation_contract":[]}'])
def test_old_or_invalid_metadata_is_retained_as_unknown(raw):
    row = {**recorded(), 'forecast_json': raw}
    cohort = ledger._version_cohorts([row])[0]
    assert cohort['metadata_complete'] is False
    assert cohort['ranking_version'] is None
    assert cohort['total'] == 1 and cohort['resolved'] == 1


def test_target_mismatch_suppresses_all_probability_scoring(monkeypatch):
    rows = [recorded()]
    monkeypatch.setattr(ledger, 'predictions_count', lambda **kw: {})
    monkeypatch.setattr(ledger, 'predictions_resolved', lambda **kw: rows)
    monkeypatch.setattr(ledger, 'predictions_open', lambda **kw: [])
    monkeypatch.setattr(ledger, '_calibration_audit', lambda: {})
    monkeypatch.setattr(ledger, 'cache_set', lambda *args, **kw: None)
    result = ledger.prediction_scorecard(force=True, evaluation_version='recorded_open_v3')
    assert result['predicted_win_rate'] is None
    assert result['rank_buckets'][0]['predicted_win_rate'] is None
    assert all(c['predicted_win_rate'] is None for c in result['enhancement_buckets'])
    assert result['version_cohorts'][0]['resolved'] == 1


def test_first_contract_survives_relogging_and_is_readable_in_both_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', Path(tmp_path) / 'test.sqlite')
    monkeypatch.setattr(db, '_INITIALIZED', False)
    contract = ledger._validation_snapshot(board(), pick(), 8)
    row = {'as_of_date': '2026-10-14', 'symbol': 'A', 'horizon_days': 8, 'calibrated_prob': .6,
           'evaluation_version': 'recorded_open_v3', 'forecast': {'validation_contract': contract}}
    assert db.predictions_log_many([row]) == 1
    assert db.predictions_log_many([{**row, 'forecast': {'validation_contract': {'ranking_version': 'new'}}}]) == 0
    pending = db.predictions_open(board_only=False)[0]
    assert json.loads(pending['forecast_json'])['validation_contract'] == contract
    with db._connect() as conn:
        pid = conn.execute('SELECT prediction_id FROM predictions').fetchone()[0]
    db.prediction_resolve(pid, exit_price=110, forward_return=.1, baseline_return=.01, excess_return=.09,
                          net_excess=.089, win=1, net_return=.099)
    saved = db.predictions_resolved(board_only=False)[0]
    assert json.loads(saved['forecast_json'])['validation_contract'] == contract
