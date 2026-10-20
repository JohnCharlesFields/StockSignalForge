from datetime import date
import json

import numpy as np
import pandas as pd
import pytest

from scripts import validate_causal_calibration as study


def protocol():
    return {'start': '2024-01-02', 'validation_starts': ['2024-04-01', '2024-06-03', '2024-08-01'],
            'test_start': '2024-10-01', 'test_end': '2024-12-31', 'embargo_sessions': 10,
            'minimum_train_dates': 30, 'minimum_validation_dates': 15,
            'candidates': ['isotonic_5', 'logistic_1'], 'top_n': 20, 'bootstrap_samples': 20}


def events():
    rows = []
    for i, day in enumerate(pd.bdate_range('2024-01-02', '2024-12-20')):
        for ticker, shift in [('A', .2), ('B', -.2)]:
            score = np.clip(.5 + np.sin(i) * .2 + shift, 0, 1)
            win = int(score > .5)
            rows.append({'date': str(day.date()), 'label_available_at': str((day + pd.offsets.BDay(5)).date()),
                         'exit_date': str((day + pd.offsets.BDay(5)).date()), 'ticker': ticker,
                         'score': float(score), 'oversold': float(score), 'hv_norm': .5,
                         'win': win, 'net_return': .02 if win else -.02, 'net_excess': .01 if win else -.01,
                         'outcome_status': 'resolved', 'horizon_days': 5})
    return rows


def test_split_never_crosses_unmatured_label_window():
    train, val = study.purged_fold(events(), '2024-06-03', '2024-08-01', protocol())
    assert train and val
    assert max(r['label_available_at'] for r in train) < '2024-06-03'
    assert max(r['date'] for r in train) < min(r['date'] for r in val)
    assert all(r['label_available_at'] < '2024-08-01' for r in val)


def test_final_test_outcomes_cannot_select_model():
    rows = events()
    selected, scores = study.select_candidate(rows, 5, protocol())
    changed = [{**r, 'win': 1 - r['win'], 'net_return': -r['net_return'], 'net_excess': -r['net_excess']}
               if r['date'] >= '2024-10-01' else r for r in rows]
    second, other = study.select_candidate(changed, 5, protocol())
    assert selected is not None and selected == second
    assert scores == other


def test_historical_time_split_cannot_claim_unknown_provenance_passed():
    result = study.validate(events(), 5, protocol(), {'unseen_holdout': False})
    assert result['status'] == 'completed'
    assert result['activated'] is False and result['eligible_for_activation'] is False
    assert 'unverified_point_in_time_universe' in result['rejection_reasons']
    assert 'unverified_unseen_holdout' in result['rejection_reasons']


def test_membership_does_not_borrow_future_archive():
    archives = [{'date': date(2024, 4, 1), 'members': {'A'}, 'captured_at':'2024-04-01T12:00:00Z'},
                {'date': date(2024, 7, 1), 'members': {'B'}, 'captured_at':'2024-07-01T12:00:00Z'}]
    assert not study.member_at(archives, 'A', date(2024, 3, 29))
    assert study.member_at(archives, 'A', date(2024, 6, 3))
    assert not study.member_at(archives, 'B', date(2024, 6, 3))


def test_fallback_membership_is_not_certified_archive(tmp_path):
    root = tmp_path / 'universe_snapshots' / 'spx'
    root.mkdir(parents=True)
    (root / '2024-01-02.json').write_text(json.dumps({'effective_date': '2024-01-02', 'tickers': ['A'], 'source': 'fallback:bundled'}))
    with pytest.raises(ValueError, match='non-fallback'):
        study.load_archives(tmp_path, 'spx')


def test_probability_metrics_preserve_real_zero_and_reject_nan():
    rows = [{**events()[0], 'win': 0}]
    result = study.probability_metrics(rows, [0])
    assert result['actual'] == 0 and result['predicted'] == 0 and result['brier'] == 0
    with pytest.raises(ValueError):
        study.probability_metrics(rows, [float('nan')])


def test_exit_day_later_close_cannot_change_already_closed_outcome():
    idx = pd.bdate_range('2024-04-01', periods=10)
    prices = np.linspace(100, 105, 10)
    frame = pd.DataFrame({'Open': prices, 'Close': prices, 'High': prices + 1, 'Low': prices - 1, 'Volume': 1000}, index=idx)
    baseline = pd.Series(.01, index=idx)
    before, failure = study.event_label(frame, frame.copy(), 0, 3, baseline)
    assert not failure
    later = frame.copy()
    later.loc[idx[3], 'Close'] = 99999
    after, failure = study.event_label(later, frame.copy(), 0, 3, baseline)
    assert not failure and before == after


def test_bad_future_path_retains_unavailable_outcome_not_a_false_loss():
    idx = pd.bdate_range('2024-04-01', periods=10)
    frame = pd.DataFrame({'Open': 100., 'Close': 100., 'High': 101., 'Low': 99., 'Volume': 1000}, index=idx)
    frame.loc[idx[2], 'Open'] = np.nan
    outcome, reason = study.event_label(frame, frame.copy(), 0, 3, pd.Series(.01, index=idx))
    assert outcome is None and reason == 'outcome_data_quality_unknown'


def test_training_feature_formula_matches_current_live_formula():
    rng = np.random.default_rng(94)
    close = 100 * np.exp(np.cumsum(rng.normal(.0002, .01, 400)))
    frame = pd.DataFrame({'Open': close, 'Close': close, 'High': close * 1.01,
                          'Low': close * .99, 'Volume': 1_000_000}, index=pd.bdate_range('2023-01-02', periods=400))
    for end in (250, 399):
        full = frame.iloc[:end]
        prefix = full.tail(126)
        live, replay = study._score_frame('TEST', full, .5), study._score_frame('TEST', prefix, .5)
        assert live['launch_score'] == replay['launch_score']
        assert live['daily_tunnel']['score'] == replay['daily_tunnel']['score']
        assert study._hv_rise(full.Close) == study._hv_rise(prefix.Close)


def test_zero_volume_is_unknown_input_not_liquidity_evidence():
    frame = pd.DataFrame({'Open': [100.], 'Close': [100.], 'High': [101.], 'Low': [99.], 'Volume': [0]})
    assert not study.valid_bars(frame)


def test_final_test_labels_cannot_change_frozen_parameters():
    rows = events()
    first = study.validate(rows, 5, protocol(), {'unseen_holdout': False})
    changed = [{**r, 'win': 1 - r['win'], 'net_return': -r['net_return'], 'net_excess': -r['net_excess']}
               if r['date'] >= '2024-10-01' else r for r in rows]
    second = study.validate(changed, 5, protocol(), {'unseen_holdout': False})
    assert first['parameters'] == second['parameters']
    assert first['test']['actual'] != second['test']['actual'] or first['test']['brier'] != second['test']['brier']
