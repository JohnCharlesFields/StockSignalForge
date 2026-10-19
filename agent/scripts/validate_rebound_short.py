"""Fixed cache-only short research replay; never activate or estimate Put PnL."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
import cost_model
from rebound_short_research import ShortEntryConfig, build_short_timeline
from pullback_validation import block_interval, shift_session
from scripts.validate_pullback_entry import PERIODS, partition, summarize


def price_proxy(trade):
    entry, exit_price = trade['entry_price'], trade['exit_price']
    gross = (entry-exit_price)/entry
    cost = cost_model.equity_round_trip_cost(entry, trade['adv'])
    days = (pd.Timestamp(trade['exit_date'])-pd.Timestamp(trade['entry_date'])).days
    return dict(gross_directional_return=gross, cost=cost, net_return=gross-cost,
        borrow_5pct_return=gross-cost-.05*days/365,
        borrow_20pct_return=gross-cost-.20*days/365, borrow_calendar_days=days)


def run_symbol(args):
    filename, as_of = args
    path = Path(filename)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        frame = pd.read_csv(path, index_col='Date', parse_dates=['Date']).loc[:as_of].tail(756)
        if len(frame) < 100:
            return dict(symbol=path.stem, sha256=sha, error='insufficient_history')
        result = build_short_timeline(path.stem, frame)
        trades, excluded = [], Counter()
        for trade in result['transactions']:
            group = partition(trade)
            if group is None:
                excluded['cross_partition'] += 1
                continue
            expected = pd.DatetimeIndex([pd.Timestamp(shift_session(pd.Timestamp(trade['entry_date']).date(), n))
                                         for n in range(trade['held_sessions']+1)])
            if not expected.isin(frame.index).all() or str(expected[-1].date()) != trade['exit_date']:
                excluded['missing_sessions'] += 1
                continue
            trades.append({**trade, **price_proxy(trade), 'period':group})
        return dict(symbol=path.stem, sha256=sha, trades=trades, exclusions=dict(excluded),
            status=result['status'], reason=result.get('reason'), latest=result['latest'],
            markers=dict(Counter(m['stage'] for m in result['markers'])))
    except Exception as exc:
        return dict(symbol=path.stem, sha256=sha, error=type(exc).__name__)


def period_stats(results, period):
    trades = [t for r in results for t in r.get('trades', []) if t['period'] == period]
    by_day = defaultdict(list)
    for trade in trades:
        by_day[trade['signal_date']].append(trade['net_return'])
    return dict(summary=summarize(trades), mean_proxy_ci95=block_interval(by_day, block_days=8),
        borrow_scenarios={name:dict(mean=float(np.mean([t[key] for t in trades])) if trades else None,
                                   positive_fraction=float(np.mean([t[key]>0 for t in trades])) if trades else None)
                          for name, key in [('annual_5pct', 'borrow_5pct_return'), ('annual_20pct', 'borrow_20pct_return')]},
        note='trade-weighted signed stock proxy before unknown borrowing/dividends/recall; not executable short or Put returns')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    files = sorted(Path(args.cache_dir).glob('*.csv'))
    if not files:
        raise ValueError('No existing cache files')
    started = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=min(4, max(1, args.workers))) as pool:
        for n, result in enumerate(pool.map(run_symbol, [(str(p), '2026-10-15') for p in files]), 1):
            results.append(result)
            if n % 100 == 0:
                print(json.dumps(dict(done=n, total=len(files), seconds=round(time.monotonic()-started, 1))), flush=True)
    report = dict(protocol='fixed_rebound_short_20261016', as_of='2026-10-15',
        config=ShortEntryConfig().__dict__, fitted=False, parameter_search=False, eligible_for_activation=False,
        return_kind='underlying_short_proxy_before_borrow', borrow_verified=False,
        external_requests=0, paid_requests=0, production_db_writes=0,
        files=len(files), processed=sum('trades' in r for r in results),
        errors=dict(Counter(r['error'] for r in results if r.get('error'))),
        statuses=dict(Counter(r['status'] for r in results if 'status' in r)),
        quality_reasons=dict(Counter(r['reason'] for r in results if r.get('reason'))),
        exclusions=dict(sum((Counter(r.get('exclusions', {})) for r in results), Counter())),
        markers=dict(sum((Counter(r.get('markers', {})) for r in results), Counter())),
        cost_assumptions=cost_model.cost_summary(), periods={name:period_stats(results, name) for name, _, _ in PERIODS},
        mcd=next((r for r in results if r['symbol'] == 'MCD'), None),
        source_manifest=[dict(symbol=r['symbol'], sha256=r['sha256']) for r in results],
        code_manifest={str(p.relative_to(AGENT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [
            Path(__file__), AGENT/'rebound_short_research.py', AGENT/'pullback_entry_research.py',
            AGENT/'technical_signal_metrics.py', AGENT/'cost_model.py', AGENT/'pullback_validation.py',
            AGENT/'market_calendar.py', AGENT/'scripts/validate_pullback_entry.py']},
        limitations=['cached universe not PIT certified, corporate actions/dividends not certified',
            'historical periods already examined; not an unseen holdout', 'no borrowing, recall, locate or margin data',
            '5%/20% annual borrow scenarios are assumptions, not observed charges',
            'daily bars/stop-first ambiguity; not intraday executable fills',
            'no account curve, market-neutral alpha or Put PnL estimate'], seconds=round(time.monotonic()-started, 1))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({k:v for k, v in report.items() if k not in ('source_manifest', 'mcd')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
