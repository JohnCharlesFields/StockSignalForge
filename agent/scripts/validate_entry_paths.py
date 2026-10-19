"""Fixed offline single-factor experiments over saved mature opportunities."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
from entry_path_research import EXPERIMENTS, MAG7, VERSION, complete_window, complete_forward_window, market_states, path_diagnostic
from event_exit_research import compile_calendar
from market_calendar import is_trading_day
from pullback_entry_research import _bars, build_entry_timeline
from pullback_validation import block_interval
from rebound_short_research import build_short_timeline
from right_side_entry_research import confirmation_opportunities, make_context, replay_right_side
from scripts.validate_event_exit import costed
from scripts.validate_right_side import eligible_period, trade_stats, PERIODS


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def legacy_module(path):
    spec = importlib.util.spec_from_file_location('frozen_legacy_pullback', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_symbol(args):
    path, reference_rows, expected_sha, legacy, schedule, market, market_index, *options = args
    experiments = options[0] if options else EXPERIMENTS
    path = Path(path)
    raw = path.read_bytes()
    base = dict(symbol=path.stem, sha256=sha(raw), raw_matches_previous=sha(raw) == expected_sha)
    try:
        frame = _bars(pd.read_csv(BytesIO(raw), index_col='Date', parse_dates=['Date']).loc[:'2026-10-15'].tail(756))
        if len(frame) < 100:
            if reference_rows:
                raise ValueError('previous_reference_now_has_insufficient_history')
            return {**base, 'status': 'insufficient_history'}
        context = make_context(frame)
        rows, diagnostics, exclusions = [], [], Counter()
        quality = build_entry_timeline(path.stem, frame)
        for side in ('LONG', 'SHORT'):
            reference = [r for r in reference_rows if r['side'] == side]
            timeline = (legacy_module(legacy).build_entry_timeline if side == 'LONG' else build_short_timeline)(path.stem, frame)
            seeds = confirmation_opportunities(path.stem, frame, side, timeline)
            rebound_seeds = seeds
            if 'short_rebound' in experiments and side == 'SHORT':
                from planned_risk_research import enrich_setup_features
                rebound_seeds = enrich_setup_features(frame, seeds)
            by_id = {s['id']: s for s in seeds}
            if not {r['id'] for r in reference} <= by_id.keys():
                raise ValueError('previous_opportunity_missing_from_legacy_stream')
            runs = {name: replay_right_side(path.stem, frame, side, schedule=schedule,
                                            opportunities=rebound_seeds if name == 'short_rebound' else seeds,
                                            context=context, experiment=name, market_context=market)
                    for name in experiments}
            trades = {name: {t['id']: costed(t, side) for t in out['transactions']} for name, out in runs.items()}
            entered = {name: {m['opportunity_id'] for m in out['markers'] if m['stage'] == 'ENTRY'} for name, out in runs.items()}
            for r in reference:
                actual = trades['baseline'].get(r['id'])
                if actual != r['variants']['current']:
                    raise ValueError('original_current_trade_failed_exact_reproduction:'+r['id'])
                seed = by_id[r['id']]
                period, mature = eligible_period(frame, seed)
                if period != r['period']:
                    raise ValueError('original_maturity_or_setup_chain_changed')
                if side == 'SHORT' and path.stem in MAG7:
                    exclusions['mag7_short'] += 1
                    continue
                if not complete_window(frame.index, r['date'], 55):
                    exclusions[side+'_stock_55_session_window_unknown'] += 1
                    continue
                if not complete_window(market_index, r['date'], 55) or market.get(r['date'], {}).get('state') == 'unknown':
                    exclusions[side+'_spy_55_session_window_unknown'] += 1
                    continue
                if not complete_forward_window(frame.index, r['date']):
                    exclusions[side+'_forward_calendar_window_invalid'] += 1
                    continue
                if any(out['latest'].get('date', '') < mature for out in runs.values()):
                    exclusions[side+'_shared_path_unknown'] += 1
                    continue
                if any(r['id'] in entered[name] and r['id'] not in trades[name] for name in experiments):
                    raise ValueError('mature_experiment_trade_unresolved')
                row = dict(id=r['id'], symbol=path.stem, side=side, period=period, date=r['date'],
                           variants={name: trades[name].get(r['id']) for name in experiments})
                rows.append(row)
                if actual:
                    d = path_diagnostic(frame, {**actual, 'period': period}, seed)
                    d['market_state'] = market.get(actual['decision_date'], {}).get('state', 'unknown')
                    d['adv'] = seed['adv']
                    diagnostics.append(d)
        return {**base, 'status': 'processed', 'rows': rows, 'diagnostics': diagnostics,
                'exclusions': dict(exclusions), 'legacy_reproduced': len(reference_rows),
                'long_quality_status': quality['status'], 'long_quality_reason': quality.get('reason')}
    except Exception as exc:
        return {**base, 'status': 'error', 'error_type': type(exc).__name__, 'reason': str(exc)[:180]}


def summarize(rows, period, side, experiments=EXPERIMENTS):
    cohort = [r for r in rows if r['period'] == period and r['side'] == side]
    dates = sorted({r['date'] for r in cohort})
    days = [str(d.date()) for d in pd.bdate_range(min(dates), max(dates)) if is_trading_day(d.date())] if dates else []
    result = {}
    for name in experiments:
        trades = [r['variants'][name] for r in cohort if r['variants'][name]]
        daily = defaultdict(list)
        cash_values = []
        for r in cohort:
            trade, baseline = r['variants'][name], r['variants']['baseline']
            value = trade['net_return'] if trade else 0.
            cash_values.append(value)
            daily[r['date']].append(value-(baseline['net_return'] if baseline else 0.))
        basket = {d: [float(np.mean(daily[d])) if daily[d] else 0.] for d in days}
        result[name] = dict(trades=trade_stats(trades), opportunities=len(cohort), skipped_or_cash=len(cohort)-len(trades),
            mean_per_opportunity=float(np.mean(cash_values)) if cohort else None,
            equal_day_difference=float(np.mean([v[0] for v in basket.values()])) if days else None,
            difference_ci95_block8=block_interval(basket, block_days=8),
            difference_ci95_block15=block_interval(basket, block_days=15))
    return result


def describe(items):
    values = np.array([r['net_return'] for r in items])
    lost = [r for r in items if r['net_return'] < 0]
    return dict(n=len(items), mean_net=float(values.mean()) if len(values) else None,
                positive_fraction=float((values > 0).mean()) if len(values) else None,
                losing_trades=len(lost), prior_close_profit_then_loss=sum(r['prior_close_profit_then_loss'] for r in lost))


def diagnose(items, period, side):
    cohort = [r for r in items if r['period'] == period and r['side'] == side]
    out = dict(summary=describe(cohort), horizons={})
    for h in ('1', '3', '5', '8'):
        paths = [r['horizons'][h] for r in cohort if r['horizons'][h] is not None]
        out['horizons'][h] = dict(n=len(paths), mean_directional_open_return=float(np.mean([p['directional_open_return'] for p in paths])) if paths else None,
            positive_fraction=float(np.mean([p['directional_open_return'] > 0 for p in paths])) if paths else None,
            mean_favorable_range=float(np.mean([p['favorable_range'] for p in paths])) if paths else None,
            mean_adverse_range=float(np.mean([p['adverse_range'] for p in paths])) if paths else None)
    groups = {
        'confirmation_distance_atr': [('0_to_0.5', 0., .5), ('0.5_to_1', .5, 1.), ('1_to_2', 1., 2.), ('2_plus', 2., float('inf'))],
        'entry_gap_atr': [('against_direction', -float('inf'), 0.), ('0_to_0.5', 0., .5), ('0.5_to_1', .5, 1.), ('1_plus', 1., float('inf'))],
        'risk_distance_atr': [('below_1', 0., 1.), ('1_to_2', 1., 2.), ('2_plus', 2., float('inf'))],
        'adv': [('below_10m', 0., 1e7), ('10m_to_50m', 1e7, 5e7), ('50m_to_200m', 5e7, 2e8), ('200m_plus', 2e8, float('inf'))],
    }
    for field, bins in groups.items():
        out[field] = {label: describe([r for r in cohort if low <= r[field] < high]) for label, low, high in bins}
    out['market_state'] = {state: describe([r for r in cohort if r['market_state'] == state]) for state in ('up', 'down', 'neutral', 'unknown')}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cache-dir', 'opportunities', 'reference', 'legacy-module', 'calendar', 'announcements', 'output'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--symbols', default='', help='Optional smoke-test subset; never a profit-selected sample')
    args = parser.parse_args()
    reference = json.loads(Path(args.reference).read_text(encoding='utf-8-sig'))
    if sha(Path(args.legacy_module).read_bytes()) != reference['code_manifest']['pullback_entry_research.py']:
        raise ValueError('legacy_module_not_the_frozen_original')
    saved = json.loads(Path(args.opportunities).read_text(encoding='utf-8-sig'))
    by_symbol = defaultdict(list)
    for row in saved:
        by_symbol[row['symbol']].append(row)
    cache = Path(args.cache_dir)
    spy_raw = (cache/'SPY.csv').read_bytes()
    spy = _bars(pd.read_csv(BytesIO(spy_raw), index_col='Date', parse_dates=['Date']).loc[:'2026-10-15'].tail(756))
    quarantined_market_dates = [str(d.date()) for d in spy.index if not is_trading_day(d.date())]
    spy = spy.loc[[is_trading_day(d.date()) for d in spy.index]]
    market = market_states(spy)
    cp, ap = Path(args.calendar), Path(args.announcements)
    assert sha(cp.read_bytes()) == reference['input_manifest']['calendar_sha256']
    assert sha(ap.read_bytes()) == reference['input_manifest']['announcements_sha256']
    schedule = compile_calendar(json.loads(cp.read_text(encoding='utf-8-sig')), json.loads(ap.read_text(encoding='utf-8-sig')))
    files = reference['input_manifest']['prices']
    if args.symbols:
        selected = set(args.symbols.split(','))
        files = [item for item in files if item['symbol'] in selected]
        saved = [r for r in saved if r['symbol'] in selected]
    jobs = [(str(cache/(item['symbol']+'.csv')), by_symbol[item['symbol']], item['sha256'], args.legacy_module,
             schedule, market, spy.index) for item in files]
    started = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=min(4, max(1, args.workers))) as pool:
        for i, result in enumerate(pool.map(run_symbol, jobs), 1):
            results.append(result)
            if i % 100 == 0:
                print(json.dumps(dict(done=i, total=len(jobs), seconds=round(time.monotonic()-started, 1))), flush=True)
    errors = [r for r in results if r['status'] == 'error']
    if errors:
        Path(args.output).with_name('errors.json').write_text(json.dumps(errors, indent=2), encoding='utf-8')
        raise ValueError('Baseline or input validation failed; see isolated errors.json')
    assert sum(r.get('legacy_reproduced', 0) for r in results) == len(saved)
    rows = [x for r in results for x in r.get('rows', [])]
    diagnostics = [x for r in results for x in r.get('diagnostics', [])]
    report = dict(version=VERSION, as_of='2026-10-15', fitted=False, eligible_for_activation=False,
        source_opportunities=len(saved), baseline_reproduced=True,
        raw_inputs_equal_previous=all(r['raw_matches_previous'] for r in results),
        changed_raw_files=[r['symbol'] for r in results if not r['raw_matches_previous']], smoke_symbols=args.symbols or None,
        quarantined_market_nontrading_dates=quarantined_market_dates,
        common_qualified_opportunities=len(rows), experiment_names=EXPERIMENTS,
        processed=sum(r['status'] == 'processed' for r in results), insufficient=sum(r['status'] == 'insufficient_history' for r in results),
        exclusions=dict(sum((Counter(r.get('exclusions', {})) for r in results), Counter())),
        long_quality_stop_reasons=dict(Counter(r.get('long_quality_reason') for r in results if r.get('long_quality_reason'))),
        periods={period: {side: summarize(rows, period, side) for side in ('LONG', 'SHORT')} for period, _, _ in PERIODS},
        path_diagnostics={period: {side: diagnose(diagnostics, period, side) for side in ('LONG', 'SHORT')} for period, _, _ in PERIODS},
        code_manifest={str(p.relative_to(AGENT)): sha(p.read_bytes()) for p in (
            Path(__file__), AGENT/'entry_path_research.py', AGENT/'right_side_entry_research.py', AGENT/'pullback_entry_research.py',
            AGENT/'rebound_short_research.py', AGENT/'pullback_validation.py', AGENT/'cost_model.py', AGENT/'market_calendar.py')},
        input_manifest=dict(reference_sha256=sha(Path(args.reference).read_bytes()),
            opportunities_sha256=sha(Path(args.opportunities).read_bytes()), legacy_module_sha256=sha(Path(args.legacy_module).read_bytes()),
            calendar_sha256=sha(cp.read_bytes()), announcements_sha256=sha(ap.read_bytes()), spy_sha256=sha(spy_raw),
            prices=[dict(symbol=r['symbol'], sha256=r['sha256']) for r in results]),
        limitations=reference['limitations']+[
            'six prespecified single-factor variants, historical periods already inspected; no untouched holdout',
            'shared legacy opportunity stream retains its shadow-position suppression, not a new full account replay',
            'complete 55-session stock and SPY windows define a common cohort before variant comparison',
            'fixed future-window path ranges can continue beyond actual exit; never input features',
            'close-profit giveback excludes actual exit-day intraday high/low to avoid unknown ordering'],
        paid_requests=0, external_requests=0, production_writes=0, seconds=round(time.monotonic()-started, 1))
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    prefix = '' if output.name == 'results.json' else output.stem+'-'
    output.with_name(prefix+'qualified-opportunities.json').write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    output.with_name(prefix+'path-diagnostics.json').write_text(json.dumps(diagnostics, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    print(json.dumps(dict(processed=report['processed'], opportunities=len(rows), seconds=report['seconds'],
                         test={s: {n: report['periods']['test'][s][n]['trades'] for n in EXPERIMENTS} for s in ('LONG', 'SHORT')})), flush=True)


if __name__ == '__main__':
    main()
