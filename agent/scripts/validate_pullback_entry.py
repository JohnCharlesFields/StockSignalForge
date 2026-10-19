"""Offline fixed-protocol comparison. Never fit, activate, fetch, or write a DB."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
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
from pullback_entry_research import EntryConfig, build_entry_timeline
from pullback_validation import block_interval, shift_session

PERIODS = [('development', '1900-01-01', '2024-12-31'),
           ('validation', '2025-01-01', '2025-12-31'), ('test', '2026-01-01', '2026-10-15')]


def partition(trade):
    for name, start, end in PERIODS:
        if start <= trade['signal_date'] <= trade['entry_date'] <= trade['exit_date'] <= end:
            return name
    return None


def run_symbol(args):
    filename, as_of, cfg = args
    path = Path(filename)
    raw = path.read_bytes()
    try:
        f = pd.read_csv(path, index_col='Date', parse_dates=['Date']).loc[:as_of].tail(756)
        if len(f) < 100:
            return {'symbol':path.stem, 'error':'insufficient_history', 'sha256':hashlib.sha256(raw).hexdigest()}
        variants = {}
        for name, allow in [('reversal_control', True), ('trend_guard', False)]:
            result = build_entry_timeline(path.stem, f, replace(cfg, allow_countertrend=allow))
            trades, excluded = [], Counter()
            for t in result['transactions']:
                group = partition(t)
                if group is None:
                    excluded['cross_partition'] += 1
                    continue
                # Missing calendar sessions must not masquerade as an 8-day hold.
                expected = pd.DatetimeIndex([pd.Timestamp(shift_session(pd.Timestamp(t['entry_date']).date(), n))
                                             for n in range(t['held_sessions']+1)])
                if not expected.isin(f.index).all() or expected[-1].date().isoformat() != t['exit_date']:
                    excluded['missing_sessions'] += 1
                    continue
                cost = cost_model.equity_round_trip_cost(t['entry_price'], t['adv'])
                trades.append({**t, 'symbol':path.stem, 'period':group, 'net_return':t['gross_return']-cost, 'cost':cost})
            variants[name] = {'trades':trades, 'excluded':dict(excluded),
                'markers':dict(Counter(m['stage'] for m in result['markers'])),
                'status':result['status'], 'reason':result.get('reason'), 'latest':result['latest']}
        return dict(symbol=path.stem, sha256=hashlib.sha256(raw).hexdigest(), variants=variants)
    except Exception as exc:
        return dict(symbol=path.stem, error=type(exc).__name__, sha256=hashlib.sha256(raw).hexdigest())


def summarize(trades):
    vals = np.array([t['net_return'] for t in trades])
    return dict(n=len(vals), symbols=len({t['symbol'] for t in trades}),
        dates=len({t['signal_date'] for t in trades}), mean_net=float(vals.mean()) if len(vals) else None,
        net_win_rate=float((vals>0).mean()) if len(vals) else None,
        worst=float(vals.min()) if len(vals) else None,
        p05=float(np.quantile(vals,.05)) if len(vals) else None,
        exit_reasons=dict(Counter(t['exit_reason'] for t in trades)))


def compare(all_results, start, end, period):
    by_variant = {}
    for name in ('reversal_control','trend_guard'):
        trades = [t for r in all_results for t in r.get('variants',{}).get(name,{}).get('trades',[]) if t['period']==period]
        daily = defaultdict(list)
        for t in trades: daily[t['signal_date']].append(t['net_return'])
        by_variant[name] = {'summary':summarize(trades), 'daily':daily}
    # Same calendar, equal-day/equal-name baskets; zero on no-trade dates.
    dates = pd.bdate_range(start, end)
    from market_calendar import is_trading_day
    days = [str(d.date()) for d in dates if is_trading_day(d.date())]
    delta = {}
    for day in days:
        a = by_variant['reversal_control']['daily'].get(day, [0.])
        b = by_variant['trend_guard']['daily'].get(day, [0.])
        delta[day] = [float(np.mean(b)-np.mean(a))]
    return {name: value['summary'] for name,value in by_variant.items()} | dict(
        mean_daily_basket_difference=float(np.mean([x[0] for x in delta.values()])) if delta else None,
        difference_ci95=block_interval(delta, block_days=8),
        note='equal-day basket selection diagnostic incl cash; not account equity or market alpha')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir',required=True)
    parser.add_argument('--as-of',default='2026-10-15')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.as_of != '2026-10-15': raise ValueError('This frozen protocol ends on 2026-10-15')
    cfg=EntryConfig()
    files=sorted(Path(args.cache_dir).glob('*.csv'))
    if not files: raise ValueError('No existing cache files')
    started=time.monotonic()
    results=[]
    with ProcessPoolExecutor(max_workers=min(4,max(1,args.workers))) as pool:
        for n, result in enumerate(pool.map(run_symbol, [(str(p),args.as_of,cfg) for p in files]),1):
            results.append(result)
            if n%100==0: print(json.dumps({'done':n,'total':len(files),'seconds':round(time.monotonic()-started,1)}),flush=True)
    minimum=min((t['signal_date'] for r in results for v in r.get('variants',{}).values() for t in v['trades']),default='2024-01-01')
    report=dict(protocol='fixed_pullback_entry_20261016', as_of=args.as_of, eligible_for_activation=False,
        fitted=False, parameter_search=False, paid_requests=0, external_requests=0, production_db_writes=0,
        files=len(files), processed=sum('variants' in r for r in results),
        errors=dict(Counter(r.get('error') for r in results if r.get('error'))),
        cost_assumptions=cost_model.cost_summary(),
        periods={name:compare(results,max(start,minimum),end,name) for name,start,end in PERIODS},
        coverage={name:{'markers':dict(sum((Counter(r.get('variants',{}).get(name,{}).get('markers',{})) for r in results),Counter())),
            'statuses':dict(Counter(r['variants'][name]['status'] for r in results if 'variants' in r)),
            'quality_reasons':dict(Counter(r['variants'][name]['reason'] for r in results
                if 'variants' in r and r['variants'][name].get('reason'))),
            'exclusions':dict(sum((Counter(r.get('variants',{}).get(name,{}).get('excluded',{})) for r in results),Counter()))}
            for name in ('reversal_control','trend_guard')},
        mcd=next((r.get('variants') for r in results if r['symbol']=='MCD'),None),
        source_manifest=[{'symbol':r['symbol'],'sha256':r['sha256']} for r in results],
        code_manifest={str(p.relative_to(AGENT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__),AGENT/'pullback_entry_research.py',AGENT/'cost_model.py']},
        limitations=['current cached universe, not certified PIT membership', 'historical data previously examined, not unseen blind holdout',
            'no dividends/corporate-action certification', 'daily bars, stop-first ambiguity; not intraday fill validation',
            'not account PnL/drawdown/Sharpe', 'no options return/probability estimate'],
        seconds=round(time.monotonic()-started,1))
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_manifest','mcd')},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
