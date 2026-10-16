"""Offline temporal calibration validation using hashed input manifests.

Reads cached OHLCV and dated membership snapshots only. Never downloads data,
activates a model, or mutates the production database. Outcomes are price-study
close-to-open labels, not returns on orders submitted after a closing signal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
import cost_model
import exit_model
from launch_signal_service import _score_frame
from market_data_service import external_data_scope
from priority_board_service import _hv_rise, _pullback_hv_score
from pullback_validation import block_interval, shift_session
from signal_calibration import apply_calibration, build_calibration
from pullback_parameter_service import ranking_settings


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_archives(root, universe):
    archives, manifest = [], []
    for path in sorted((Path(root) / 'universe_snapshots' / universe).glob('*.json')):
        raw = path.read_bytes()
        value = json.loads(raw)
        source = str(value.get('source') or '')
        if not (source.startswith('live:') or source == 'wikipedia:pit_reconstruction'):
            continue
        day = date.fromisoformat(value['effective_date'])
        members = set(value.get('tickers') or [])
        if not members:
            continue
        archives.append({'date': day, 'members': members, 'source': source,
                         'captured_at': value.get('captured_at')})
        manifest.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'effective_date': str(day),
                         'source': source, 'members': len(members), 'captured_at': value.get('captured_at')})
    if not archives:
        raise ValueError('No non-fallback archived membership; current holdings cannot substitute')
    return archives, manifest


def member_at(archives, symbol, day):
    eligible = [row for row in archives if row['date'] <= day]
    return bool(eligible and symbol in eligible[-1]['members'])


def read_bars(path):
    raw = Path(path).read_bytes()
    frame = pd.read_csv(io.BytesIO(raw), index_col='Date', parse_dates=['Date'])
    frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
    if frame.index.duplicated().any():
        raise ValueError('duplicate sessions')
    result = frame[['Open', 'High', 'Low', 'Close', 'Volume']].apply(pd.to_numeric, errors='coerce').sort_index()
    result.attrs['source_sha256'] = hashlib.sha256(raw).hexdigest()
    return result


def valid_bars(frame):
    values = frame.to_numpy(dtype=float)
    prices = frame[['Open', 'High', 'Low', 'Close']]
    if not np.isfinite(values).all() or (prices <= 0).any().any() or (frame.Volume <= 0).any():
        return False
    if (frame.High < prices.max(axis=1)).any() or (frame.Low > prices.min(axis=1)).any():
        return False
    ratio = frame.Close / frame.Close.shift(1)
    return not ((ratio > 2.5) | (ratio < 1 / 2.5)).any()


def event_label(stock, spy, index, horizon, baseline):
    day = stock.index[index].date()
    sessions = [pd.Timestamp(shift_session(day, n)) for n in range(horizon + 1)]
    if not all(s in stock.index and s in spy.index for s in sessions):
        return None, 'missing_path_session'
    # Invalid future bars mean an unavailable outcome, NOT a rejected entry.
    if not valid_bars(stock.loc[sessions[:-1]]) or not valid_bars(spy.loc[sessions[:-1]]):
        return None, 'outcome_data_quality_unknown'
    exit_prices = [float(df.loc[sessions[-1], 'Open']) for df in (stock, spy)]
    if not all(np.isfinite(p) and p > 0 for p in exit_prices):
        return None, 'exit_open_unavailable'
    base = float(baseline.iloc[index])
    if not np.isfinite(base):
        return None, 'baseline_unknown_at_signal'
    entry = float(stock.iloc[index].Close)
    exit_price = float(stock.loc[sessions[-1], 'Open'])
    benchmark = float(spy.loc[sessions[-1], 'Open'] / spy.loc[sessions[0], 'Close'] - 1)
    history = stock.iloc[max(0, index - 19):index + 1]
    adv = float((history.Close * history.Volume).mean())
    cost = cost_model.equity_round_trip_cost(price=entry, avg_dollar_volume=adv)
    gross = exit_price / entry - 1
    paired = pd.concat([stock.Close.pct_change(fill_method=None), spy.Close.pct_change(fill_method=None)], axis=1, sort=True).loc[:sessions[0]].dropna().tail(60)
    beta = None
    if len(paired) >= 40 and paired.iloc[:, 1].var() > 0:
        beta = float(paired.iloc[:, 0].cov(paired.iloc[:, 1]) / paired.iloc[:, 1].var())
    return {'horizon_days': horizon, 'exit_date': str(sessions[-1].date()),
            'label_available_at': str(sessions[-1].date()), 'forward_return': gross,
            'baseline_return': base, 'baseline_as_of': str(day),
            'baseline_method': 'known_matured_expanding_v1', 'cost': cost,
            'net_return': gross - cost, 'net_excess': gross - base - cost,
            'beta_alpha': gross - beta * benchmark - cost if beta is not None else None,
            'win': int(gross - base - cost > 0)}, None


def collect(root, protocol):
    archives, archive_manifest = load_archives(root, protocol['universe'])
    symbols = sorted(set().union(*(r['members'] for r in archives if str(r['date']) <= protocol['test_end'])))
    spy_path = Path(root) / 'market_data' / 'ohlcv_daily' / 'SPY.csv'
    spy = read_bars(spy_path).loc[:protocol['data_as_of']]
    if spy.empty:
        raise ValueError('SPY cache unavailable')
    frames_manifest = [{'symbol': 'SPY', 'sha256': spy.attrs['source_sha256'], 'bars': len(spy)}]
    events = []
    score_config = ranking_settings()
    counters = Counter(requested_symbols=len(symbols))
    horizons = [protocol['primary_horizon'], *protocol['secondary_horizons']]
    for number, symbol in enumerate(symbols, 1):
        if ranking_settings()['version'] != score_config['version']:
            raise ValueError('Scoring configuration changed during the frozen experiment')
        path = Path(root) / 'market_data' / 'ohlcv_daily' / (symbol.replace('.', '_').replace('/', '_') + '.csv')
        if not path.exists():
            counters['symbols_missing_cache'] += 1
            continue
        try:
            frame = read_bars(path).loc[:protocol['data_as_of']]
        except (ValueError, KeyError):
            counters['symbols_bad_cache'] += 1
            continue
        frames_manifest.append({'symbol': symbol, 'sha256': frame.attrs['source_sha256'], 'bars': len(frame),
                                'first': str(frame.index[0].date()) if len(frame) else None,
                                'last': str(frame.index[-1].date()) if len(frame) else None})
        baselines = {h: exit_model.known_baseline_series(frame.Close, frame.Open, h) for h in horizons}
        for index in range(protocol['score_lookback'] - 1, len(frame), protocol['cooldown_sessions']):
            day = frame.index[index].date()
            if not protocol['start'] <= str(day) <= protocol['test_end'] or not member_at(archives, symbol, day):
                continue
            prefix = frame.iloc[index + 1 - protocol['score_lookback']:index + 1]
            if not valid_bars(prefix):
                counters['known_input_quality_rejections'] += 1
                continue
            scored = _score_frame(symbol, prefix, .5)
            hv = _hv_rise(prefix.Close)
            if scored is None or hv is None:
                counters['features_unavailable'] += 1
                continue
            launch = scored['launch_score']
            score = _pullback_hv_score(launch, hv)
            for h in horizons:
                label, failure = event_label(frame, spy, index, h, baselines[h])
                row = {'ticker': symbol, 'date': str(day), 'score': score,
                       'launch_score': launch, 'hv_norm': np.clip(.5 + .5 * hv, 0, 1),
                       'oversold': 1 - launch, 'outcome_status': failure or 'resolved', 'horizon_days': h}
                if label:
                    row.update(label)
                else:
                    counters[f'label_unavailable_{h}d'] += 1
                events.append(row)
        counters['symbols_loaded'] += 1
        if number % 20 == 0:
            print(json.dumps({'stage': 'collect', 'symbols_done': number, 'symbols_total': len(symbols),
                              'events': len(events)}, ensure_ascii=False), flush=True)
    provenance = {'price_source': 'existing_mixed_daily_csv_cache', 'universe_policy': 'latest_nonfallback_archived_snapshot_effective_on_or_before_signal',
                  'point_in_time_universe': False, 'survivorship_controlled': False,
                  'corporate_actions_verified': False, 'unseen_holdout': protocol['unseen_holdout'],
                  'account_backtest_verified': False, 'score_source': 'live_score_frame_and_priority_pullback_hv_score',
                  'label': 'close_signal_day_to_future_session_open_net_own_excess',
                  'baseline_method': 'known_matured_expanding_v1',
                  'limitations': ['quarterly post-hoc reconstruction and sparse live archives, not daily historical member certification',
                                  'missing retired tickers and mixed raw/adjusted price provenance',
                                  'historical final period inspected by earlier research; not proven untouched',
                                  'probability-core ranking only; historical news, GEX and full live-board risk filters are not reconstructed',
                                  'close-entry labels are price-study proxies, not execution after a completed close signal']}
    return events, {'counters': dict(counters), 'provenance': provenance, 'score_config': score_config,
                    'archives': archive_manifest, 'prices': frames_manifest}


def purged_fold(rows, start, end, protocol):
    day = date.fromisoformat(start)
    # Previous sessions, using the existing US calendar rather than stock row counts.
    from market_calendar import is_trading_day
    from datetime import timedelta
    for _ in range(protocol['embargo_sessions']):
        day -= timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
    cutoff = str(day)
    train = [r for r in rows if r['date'] < cutoff and r['label_available_at'] < start]
    validation = [r for r in rows if start <= r['date'] < end and r['label_available_at'] < end]
    return train, validation


def fit(name, rows, horizon):
    labels = np.array([r['win'] for r in rows])
    if len(set(labels)) < 2:
        raise ValueError('training labels contain only one class')
    if name.startswith('isotonic_'):
        prepared = [{'ticker': r['ticker'], 'score': r['score'], 'forward_return': r['net_return'],
                     'excess_return': r['net_excess']} for r in rows]
        curve = build_calibration(prepared, signal_type='pullback_hv', horizon=horizon, cost_bps=0,
                                  target_buckets=int(name.split('_')[1]))
        return {'name': name, 'curve': curve, 'training_rate': float(labels.mean())}
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    model = make_pipeline(StandardScaler(), LogisticRegression(C=float(name.split('_')[1]), max_iter=1000, random_state=42))
    model.fit(np.array([[r['oversold'], r['hv_norm']] for r in rows]), labels)
    scaler, logistic = model.steps[0][1], model.steps[1][1]
    return {'name': name, 'training_rate': float(labels.mean()), 'center': scaler.mean_.tolist(),
            'scale': scaler.scale_.tolist(), 'weights': logistic.coef_[0].tolist(), 'intercept': float(logistic.intercept_[0])}


def predict(model, rows):
    if 'curve' in model:
        return np.array([apply_calibration(model['curve'], r['score'])['p_calibrated'] for r in rows], dtype=float)
    x = np.array([[r['oversold'], r['hv_norm']] for r in rows])
    z = (x - model['center']) / model['scale'] @ np.array(model['weights']) + model['intercept']
    return 1 / (1 + np.exp(-np.clip(z, -35, 35)))


def date_mean(values, rows):
    groups = {}
    for value, row in zip(values, rows):
        groups.setdefault(row['date'], []).append(float(value))
    return float(np.mean([np.mean(v) for v in groups.values()])) if groups else None


def probability_metrics(rows, probabilities):
    if not rows:
        return {'n': 0, 'dates': 0}
    y = np.array([r['win'] for r in rows])
    p = np.array(probabilities)
    if len(p) != len(rows) or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('invalid predicted probabilities')
    reliability = []
    for lo, hi in zip(np.arange(0, 1, .1), np.arange(.1, 1.01, .1)):
        mask = (p >= lo) & (p < hi if hi < .999 else p <= 1)
        if mask.any():
            reliability.append({'lo': float(lo), 'hi': float(hi), 'n': int(mask.sum()),
                                'predicted': float(p[mask].mean()), 'actual': float(y[mask].mean())})
    return {'n': len(rows), 'dates': len({r['date'] for r in rows}), 'tickers': len({r['ticker'] for r in rows}),
            'brier': date_mean((p - y) ** 2, rows), 'predicted': date_mean(p, rows),
            'actual': date_mean(y, rows), 'net_positive_rate': date_mean([r['net_return'] > 0 for r in rows], rows),
            'mean_net': date_mean([r['net_return'] for r in rows], rows),
            'mean_own_excess': date_mean([r['net_excess'] for r in rows], rows), 'reliability': reliability}


def ranking_metrics(rows, probabilities, horizon, protocol):
    groups = {}
    for row, probability in zip(rows, probabilities):
        groups.setdefault(row['date'], []).append((float(probability), row))
    buckets = {key: [] for key in ('Top1-3', '4-10', '11-30', '31+')}
    selected, spread = {}, {}
    for day, group in groups.items():
        ranked = sorted(group, key=lambda x: (-x[0], -x[1]['score'], x[1]['ticker']))
        for rank, (p, r) in enumerate(ranked, 1):
            key = 'Top1-3' if rank <= 3 else '4-10' if rank <= 10 else '11-30' if rank <= 30 else '31+'
            buckets[key].append((p, r))
        selected[day] = [r for _, r in ranked[:protocol['top_n']]]
        if len(ranked) > 30:
            spread[day] = [float(np.mean([r['net_excess'] for _, r in ranked[:3]]) - np.mean([r['net_excess'] for _, r in ranked[30:]]))]
    metrics = {}
    for key, items in buckets.items():
        metrics[key] = probability_metrics([r for _, r in items], [p for p, _ in items])
    top_net = {d: [r['net_return'] for r in rs] for d, rs in selected.items()}
    top_excess = {d: [r['net_excess'] for r in rs] for d, rs in selected.items()}
    ordered = [metrics[k].get('mean_own_excess') for k in buckets]
    monotone = all(v is not None for v in ordered) and all(a >= b for a, b in zip(ordered, ordered[1:]))
    spread_ci = block_interval(spread, block_days=horizon, samples=protocol['bootstrap_samples'])
    return {'buckets': metrics, 'observed_monotone': monotone, 'top_minus_tail_ci95': spread_ci,
            'monotonicity_supported': bool(monotone and spread_ci[0] is not None and spread_ci[0] > 0),
            'top20_net_ci95': block_interval(top_net, block_days=horizon, samples=protocol['bootstrap_samples']),
            'top20_own_excess_ci95': block_interval(top_excess, block_days=horizon, samples=protocol['bootstrap_samples']),
            'top20_mean_net': float(np.mean([np.mean(v) for v in top_net.values()])) if top_net else None,
            'top20_mean_own_excess': float(np.mean([np.mean(v) for v in top_excess.values()])) if top_excess else None,
            'ranking_note': 'probability descending, raw score tie-break, ticker deterministic last tie-break; event baskets, not funded equity curve'}


def select_candidate(rows, horizon, protocol):
    results = []
    starts = protocol['validation_starts']
    for name in protocol['candidates']:
        folds = []
        for start, end in zip(starts, starts[1:] + [protocol['test_start']]):
            train, validation = purged_fold(rows, start, end, protocol)
            if len({r['date'] for r in train}) < protocol['minimum_train_dates'] or len({r['date'] for r in validation}) < protocol['minimum_validation_dates']:
                continue
            print(json.dumps({'stage': 'fit_fold', 'horizon': horizon, 'candidate': name, 'validation_start': start}, ensure_ascii=False), flush=True)
            model = fit(name, train, horizon)
            metrics = probability_metrics(validation, predict(model, validation))
            baseline = probability_metrics(validation, [model['training_rate']] * len(validation))
            folds.append({'start': start, 'end': end, 'train_n': len(train), 'last_training_label': max(r['label_available_at'] for r in train),
                          'metrics': metrics, 'training_base_rate_brier': baseline['brier']})
        results.append({'name': name, 'folds': folds, 'selection_brier': float(np.mean([f['metrics']['brier'] for f in folds])) if len(folds) == 3 else None})
        print(json.dumps({'stage': 'validation', 'horizon': horizon, 'candidate': name, 'folds': len(folds)}, ensure_ascii=False), flush=True)
    eligible = [r for r in results if r['selection_brier'] is not None]
    if not eligible:
        return None, results
    return min(eligible, key=lambda r: (r['selection_brier'], protocol['candidates'].index(r['name'])))['name'], results


def read_incumbent(root, horizon):
    path = Path(root) / 'easymoneysniper.sqlite3'
    if not path.exists():
        return None
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM signal_calibration WHERE is_active=1 AND signal_type=? AND horizon_days=?', ('pullback_hv', horizon)).fetchone()
    return dict(row) if row else None


def validate(events, horizon, protocol, provenance, incumbent=None):
    rows = [r for r in events if r['horizon_days'] == horizon and r['outcome_status'] == 'resolved']
    selected, validation = select_candidate(rows, horizon, protocol)
    if selected is None:
        return {'status': 'insufficient_data', 'validation': validation, 'activated': False}
    train, _ = purged_fold(rows, protocol['test_start'], '9999-12-31', protocol)
    test = [r for r in rows if protocol['test_start'] <= r['date'] <= protocol['test_end']]
    if not test:
        return {'status': 'insufficient_test', 'activated': False}
    model = fit(selected, train, horizon)
    probabilities = predict(model, test)
    metrics = probability_metrics(test, probabilities)
    baseline = probability_metrics(test, [model['training_rate']] * len(test))
    brier_improvements = {}
    for row, p in zip(test, probabilities):
        difference = (model['training_rate'] - row['win']) ** 2 - (float(p) - row['win']) ** 2
        brier_improvements.setdefault(row['date'], []).append(difference)
    brier_ci = block_interval({d: [float(np.mean(v))] for d, v in brier_improvements.items()},
                             block_days=horizon, samples=protocol['bootstrap_samples'])
    metrics['brier_skill'] = 1 - metrics['brier'] / baseline['brier'] if baseline['brier'] else None
    metrics['brier_improvement_ci95'] = brier_ci
    rank = ranking_metrics(test, probabilities, horizon, protocol)
    comparison = None
    if incumbent:
        available = str(incumbent['generated_at'])[:10]
        subset = [r for r in test if r['date'] > available]
        curve = json.loads(incumbent['curve_json'])
        if subset:
            comparison = {'start_after_curve_generation': available, 'n': len(subset),
                          'legacy': probability_metrics(subset, [apply_calibration(curve, r['score'])['p_calibrated'] for r in subset]),
                          'candidate': probability_metrics(subset, predict(model, subset)),
                          'note': 'same corrected labels and live-formula features; old curve evaluated only after creation, not retrospective claims before deployment'}
    failures = []
    if metrics['brier'] >= baseline['brier']:
        failures.append('does_not_beat_training_base_rate_brier')
    if brier_ci[0] is None or brier_ci[0] <= 0:
        failures.append('brier_improvement_ci_not_positive')
    if not rank['monotonicity_supported']:
        failures.append('ranking_monotonicity_not_supported')
    for key in ('top20_net_ci95', 'top20_own_excess_ci95'):
        if rank[key][0] is None or rank[key][0] <= 0:
            failures.append(key + '_not_positive')
    for key in ('point_in_time_universe', 'survivorship_controlled', 'corporate_actions_verified', 'unseen_holdout', 'account_backtest_verified'):
        if provenance.get(key) is not True:
            failures.append('unverified_' + key)
    return {'status': 'completed', 'selected_candidate': selected, 'validation': validation,
            'training_n': len(train), 'last_training_label': max(r['label_available_at'] for r in train),
            'test': metrics, 'training_base_rate': baseline, 'ranking': rank, 'incumbent_comparison': comparison,
            'parameters': model, 'eligible_for_activation': not failures, 'rejection_reasons': failures,
            'activated': False, 'statistical_note': 'time-isolated historical revalidation, not proof of an untouched final holdout or absence of all overfitting'}


def run(root, protocol_path, output):
    protocol_hash = digest(protocol_path)
    code_hash = digest(__file__)
    protocol = json.loads(Path(protocol_path).read_text(encoding='utf-8'))
    boundaries = [protocol['start'], *protocol['validation_starts'], protocol['test_start'], protocol['test_end']]
    if len(protocol['validation_starts']) != 3 or boundaries != sorted(set(boundaries)):
        raise ValueError('Training, three validation folds and final test must be chronological and disjoint')
    if protocol.get('auto_activate') is not False:
        raise ValueError('This offline validator cannot activate a production curve')
    if any(h < 1 for h in [protocol['primary_horizon'], *protocol['secondary_horizons']]):
        raise ValueError('Invalid holding horizon')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'report.json').exists():
        raise ValueError('Test already consumed in this output directory; do not overwrite or tune on it')
    started = time.monotonic()
    horizons = [protocol['primary_horizon'], *protocol['secondary_horizons']]
    incumbents = {h: read_incumbent(root, h) for h in horizons}
    with external_data_scope(False):
        events, inventory = collect(root, protocol)
    event_path = output / 'events.jsonl'
    with event_path.open('w', encoding='utf-8') as stream:
        for row in events:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    results = {str(h): validate(events, h, protocol, inventory['provenance'], incumbents[h]) for h in horizons}
    if digest(protocol_path) != protocol_hash:
        raise ValueError('Protocol changed during experiment; discard results')
    if digest(__file__) != code_hash:
        raise ValueError('Validator changed during experiment; discard results')
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'protocol': protocol,
              'protocol_sha256': protocol_hash, 'event_sha256': digest(event_path),
              'code_sha256': code_hash,
              'cost_assumptions': cost_model.cost_summary(), 'inventory': inventory, 'results': results,
              'activated': False, 'external_requests': 0, 'elapsed_seconds': time.monotonic() - started}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'stage': 'completed', 'events': len(events), 'activated': False, 'elapsed_seconds': report['elapsed_seconds'],
                      'results': {h: {'selected': r.get('selected_candidate'), 'rejection_reasons': r.get('rejection_reasons')} for h, r in results.items()}}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.data_root, args.protocol, args.output)
