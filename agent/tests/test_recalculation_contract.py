from copy import deepcopy
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

import priority_board_service as board
from scripts import validate_causal_calibration as study
from corporate_action_research import prepare_history


def frame():
    days = pd.bdate_range('2024-12-02', periods=10)
    return pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000000.),index=days)


def test_calibration_known_split_is_not_a_fake_loss_or_hv_jump():
    raw = frame()
    raw.loc['2024-12-04':,['Open','High','Low','Close']] /= 4
    raw.loc['2024-12-04':,'Volume'] *= 4
    stock, _, issue = prepare_history('ANET',raw,check_prices=False)
    assert not issue
    label, reason = study.event_label(stock,frame(),0,3,pd.Series(0.,index=raw.index),raw_entry_price=100.)
    assert not reason
    assert label['forward_return'] == 0.
    import cost_model
    assert label['net_return'] == pytest.approx(-cost_model.equity_round_trip_cost(100.,100000000.))


def test_label_exit_open_unknown_units_not_a_fake_giant_winner():
    stock = frame()
    stock.loc[stock.index[3],'Open'] = 400.
    label, reason = study.event_label(stock,frame(),0,3,pd.Series(0.,index=stock.index))
    assert label is None and reason == 'exit_open_price_units_unknown'


def test_quality_baseline_excludes_missing_sessions_and_later_exit_candle():
    f = frame()
    before = study.known_quality_baseline(f,3)
    changed = f.copy()
    changed.loc[f.index[3],['High','Low','Close']] = np.nan
    after = study.known_quality_baseline(changed,3)
    assert before.iloc[3] == after.iloc[3] == 0.
    g = f.drop(f.index[1])
    assert pd.isna(study.known_quality_baseline(g,3).iloc[3])


def test_independent_incumbent_snapshot_read_without_wal_or_writes(tmp_path):
    path = tmp_path/'backup.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE signal_calibration (is_active INT, signal_type TEXT, horizon_days INT, curve_json TEXT)')
        conn.execute('INSERT INTO signal_calibration VALUES (1,?,?,?)',('pullback_hv',8,json.dumps({'test':True})))
    raw = path.read_bytes()
    result = study.read_incumbent(tmp_path,8,snapshot=path)
    assert json.loads(result['curve_json']) == {'test':True}
    assert path.read_bytes() == raw and not path.with_name(path.name+'-wal').exists()
    path.with_name(path.name+'-wal').write_bytes(b'not-a-closed-snapshot')
    with pytest.raises(ValueError, match='independent_closed_backup'):
        study.read_incumbent(tmp_path,8,snapshot=path)


def test_empty_calibration_cannot_be_a_valid_zero_win_rate():
    from .test_causal_calibration_validation import protocol
    result = study.validate([],8,protocol(),{})
    assert result['resolved_events'] == 0
    assert result['eligible_for_activation'] is False
    assert 'test' not in result


def active(provenance=None):
    return dict(source='replay',curve={'global':{'edge':{'validated':True},'provenance':provenance or {}}})


def test_missing_provenance_downgrades_claim_not_signal_or_probability(monkeypatch):
    monkeypatch.setattr(board,'signal_calibration_active',lambda *args: active())
    result = board._select_signals()
    assert result['mode'] == 'pullback_hv'
    assert result['horizon'] == 8
    assert result['badge'] == 'experimental'
    assert '未验证' in result['note']


def test_persisted_board_confidence_is_a_view_not_a_history_rewrite(monkeypatch):
    monkeypatch.setattr(board,'signal_calibration_active',lambda *args: active())
    original = dict(signal_basis='pullback_hv_slice',horizon_days=8,
        confidence={'badge':'validated','note':'old'},picks=[dict(symbol='AAA',calibrated_probability=.57,
        priority_score=.42,confidence_badge='validated')])
    saved = deepcopy(original)
    view = board._research_validation_view(original)
    assert original == saved
    assert view['confidence']['badge'] == view['picks'][0]['confidence_badge'] == 'experimental'
    assert view['picks'][0]['calibrated_probability'] == .57
    assert view['picks'][0]['priority_score'] == .42


def test_complete_evidence_is_required_before_retaining_validated_label(monkeypatch):
    evidence = {k:True for k in ('point_in_time_universe','survivorship_controlled',
        'corporate_actions_verified','unseen_holdout','account_backtest_verified')}
    monkeypatch.setattr(board,'signal_calibration_active',lambda *args: active(evidence))
    assert board._select_signals()['badge'] == 'validated'
    evidence['corporate_actions_verified'] = False
    assert board._select_signals()['badge'] == 'experimental'
