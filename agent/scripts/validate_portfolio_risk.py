"""Frozen archived-opportunity portfolio study. No fetching, fitting, or activation."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, replace
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
from event_exit_research import compile_calendar
from market_calendar import is_trading_day
from portfolio_risk_research import VERSION, PortfolioConfig, entry_plans, exit_at, replay_portfolio
from pullback_entry_research import _bars
from pullback_validation import shift_session

COHORT_SHA = '14fcf2bf9077e1ec4be3bd0a5bf4bab6ca8f4cb78806ec2d3fa159ef2ab17e55'
AUDIT_SHA = '872955a3f3cd53366657355365df3b1daddb57a575684f35a31193fae544a87a'
PERIODS = ('development', 'validation', 'test')
MODES = ('LONG', 'SHORT', 'BOTH')
SIZINGS = ('fixed_notional', 'equal_risk')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_single_paths(data, rows, schedule):
    events = defaultdict(list)
    for e in schedule.exits:
        events[e['exit_date']].append(e)
    checks = 0
    for r in rows:
        t = r['variants']['baseline']
        if not t:
            continue
        f = data[r['symbol']]
        start = f.index.get_loc(pd.Timestamp(t['entry_date']))
        price = float(f.iloc[start].Open)
        assert price == t['entry_price']
        sign = 1 if r['side'] == 'LONG' else -1
        target = price+sign*2*sign*(price-t['stop'])
        meta = dict(symbol=r['symbol'], side=r['side'], stop=t['stop'], target=target, hold_sessions=8)
        hit = None
        for age in range(9):
            bar, day = f.iloc[start+age], str(f.index[start+age].date())
            for phase in (('intraday', 'close') if age == 0 else ('open', 'intraday', 'close')):
                hit = exit_at(bar, meta, age, phase, events[day])
                if hit:
                    break
            if hit:
                break
        assert hit and (day, hit[0], hit[1]) == (t['exit_date'], t['exit_price'], t['exit_reason']), t['id']
        cost = cost_model.equity_round_trip_cost(price, t['adv'])
        assert cost == t['cost']
        assert np.isclose(sign*(hit[0]/price-1)-cost, t['net_return'], rtol=0, atol=1e-12)
        checks += 1
    return checks


def paired_intervals(values, block):
    values = np.asarray(values, dtype=float)
    if not len(values) or not np.isfinite(values).all():
        raise ValueError('invalid_portfolio_return_pairs')
    rng = np.random.default_rng(42)
    starts = rng.integers(0, len(values), (10000, int(np.ceil(len(values)/block))))
    idx = ((starts[:, :, None]+np.arange(block)) % len(values)).reshape(10000, -1)[:, :len(values)]
    draws = values[idx].mean(axis=1)
    tail = .05/3/2
    return dict(mean=float(values.mean()), ci95=np.quantile(draws, [.025, .975]).tolist(),
                family_ci=np.quantile(draws, [tail, 1-tail]).tolist())


def daily_returns(run, initial_cash):
    equity = np.array([s['equity'] for s in run['snapshots']], dtype=float)
    return equity/np.r_[initial_cash, equity[:-1]]-1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('previous', 'calendar', 'announcements', 'protocol', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--symbols', default='')
    a = p.parse_args()
    old, output = Path(a.previous), Path(a.output)
    audit_raw = (old/'results-calendar-audited.json').read_bytes()
    raw = (old/'calendar-audited-opportunities.json').read_bytes()
    assert sha(audit_raw) == AUDIT_SHA and sha(raw) == COHORT_SHA
    audit, rows = json.loads(audit_raw), json.loads(raw)
    files = audit['input_manifest']['prices']
    for f in files:
        assert sha((old/'prices'/(f['symbol']+'.csv')).read_bytes()) == f['sha256']
    cp, ap = Path(a.calendar), Path(a.announcements)
    assert sha(cp.read_bytes()) == audit['input_manifest']['calendar_sha256']
    assert sha(ap.read_bytes()) == audit['input_manifest']['announcements_sha256']
    schedule = compile_calendar(json.loads(cp.read_bytes()), json.loads(ap.read_bytes()))
    code_paths = [Path(__file__), AGENT/'portfolio_risk_research.py', AGENT/'backtest/engines/base.py',
        AGENT/'backtest/engines/global_equity.py', AGENT/'backtest/models.py', AGENT/'backtest/metrics.py',
        AGENT/'trade_risk_rules.py', AGENT/'cost_model.py', AGENT/'market_calendar.py',
        AGENT/'event_exit_research.py', AGENT/'pullback_validation.py', AGENT/'pullback_entry_research.py']
    code = {str(f.relative_to(AGENT)): sha(f.read_bytes()) for f in code_paths}
    protocol_hash = sha(Path(a.protocol).read_bytes())
    if a.symbols:
        selected = set(a.symbols.split(','))
        rows = [r for r in rows if r['symbol'] in selected]
    symbols = {r['symbol'] for r in rows if r['variants']['baseline']} | {'SPY'}
    data = {}
    for s in sorted(symbols):
        f = _bars(pd.read_csv(old/'prices'/(s+'.csv'), index_col='Date', parse_dates=['Date']).loc[:'2026-10-15'].tail(756))
        f.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in f.index])
        data[s] = f
    started = time.monotonic()
    checks = verify_single_paths(data, rows, schedule)
    print(json.dumps(dict(stage='individual_baseline_reproduced', trades=checks)), flush=True)
    config = PortfolioConfig()
    result, sensitivity = {}, {}
    for period in PERIODS:
        cohort = [r for r in rows if r['period'] == period]
        if not cohort:
            raise ValueError('empty_portfolio_period')
        end = dict(development='2024-12-31', validation='2025-12-31', test='2026-10-15')[period]
        start = min(r['date'] for r in cohort)
        days = [str(d.date()) for d in pd.bdate_range(start, end) if is_trading_day(d.date())]
        plans = entry_plans(cohort)
        assert all(p['side'] != 'SHORT' or p['symbol'] not in {'AAPL', 'MSFT', 'AMZN', 'GOOG', 'GOOGL', 'META', 'NVDA', 'TSLA'} for p in plans)
        spy = data['SPY'].Close.reindex(pd.to_datetime([shift_session(pd.Timestamp(days[0]).date(), -1).isoformat()]+days))
        if spy.isna().any() or not np.isfinite(spy).all() or (spy <= 0).any():
            raise ValueError('missing_spy_benchmark_prices')
        bench = spy.pct_change(fill_method=None).iloc[1:]
        result[period] = {}
        for mode in MODES:
            result[period][mode] = {}
            for sizing in SIZINGS:
                run = replay_portfolio(data, plans, days, schedule, mode=mode, sizing=sizing,
                                       config=config, benchmark_returns=bench)
                result[period][mode][sizing] = run
                print(json.dumps(dict(stage='portfolio', period=period, mode=mode, sizing=sizing,
                    trades=len(run['trades']), total_return=run['metrics']['total_return'])), flush=True)
        if period == 'test':
            for label, rate, reverse in (('borrow0', 0., False), ('borrow20', .20, False), ('reverse_queue', .05, True)):
                sensitivity[label] = {}
                for mode in MODES:
                    sensitivity[label][mode] = {}
                    for sizing in SIZINGS:
                        run = replay_portfolio(data, plans, days, schedule, mode=mode, sizing=sizing,
                            config=replace(config, borrow_rate=rate), reverse_priority=reverse, benchmark_returns=bench)
                        sensitivity[label][mode][sizing] = {k: run[k] for k in ('metrics', 'fees', 'borrow', 'rejects')}
    inference = {}
    for mode in MODES:
        pair = result['test'][mode]
        delta = daily_returns(pair['equal_risk'], config.initial_cash)-daily_returns(pair['fixed_notional'], config.initial_cash)
        inference[mode] = {str(block): paired_intervals(delta, block) for block in (8, 15)}
    assert code == {str(f.relative_to(AGENT)): sha(f.read_bytes()) for f in code_paths}
    assert protocol_hash == sha(Path(a.protocol).read_bytes())
    assert all(sha((old/'prices'/(f['symbol']+'.csv')).read_bytes()) == f['sha256'] for f in files)
    report = dict(version=VERSION, config=asdict(config), results=result, sensitivity=sensitivity,
        paired_inference=dict(primary_period='test', inspected_history=True, comparisons=3,
            samples=10000, family_confidence=1-.05/3, method='paired daily returns; circular blocks, not strategy re-simulation', results=inference),
        rows=len(rows), source_trades_reproduced=checks, as_of='2026-10-15', smoke_symbols=a.symbols or None,
        code_manifest=code, protocol_sha256=protocol_hash,
        input_manifest=dict(cohort_sha256=COHORT_SHA, audit_sha256=AUDIT_SHA, prices=files,
            calendar_sha256=sha(cp.read_bytes()), announcements_sha256=sha(ap.read_bytes())),
        costs=cost_model.cost_summary(), event_coverage=schedule.audit,
        limitations=audit['limitations']+['fixed SHA queue is not historical model ranking',
            'archived executed opportunities inherit original shadow-position suppression and complete-case selection',
            'short collateral 100%; actual stock borrow, recalls, dividends and margin rules not modeled',
            'risk caps constrain new entries; market moves can cause later exposure drift',
            'SPY price-only comparison is not beta-stripped alpha or a total-return benchmark'],
        fitted=False, eligible_for_activation=False, external_requests=0, paid_requests=0, production_writes=0,
        seconds=round(time.monotonic()-started, 1))
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    for period in PERIODS:
        series = {mode+':'+size: pd.Series([s['equity'] for s in result[period][mode][size]['snapshots']],
                    index=[s['date'] for s in result[period][mode][size]['snapshots']]) for mode in MODES for size in SIZINGS}
        pd.DataFrame(series).to_csv(output.with_name(output.stem+'-'+period+'-equity.csv'), index_label='Date')
    print(json.dumps(dict(stage='complete', seconds=report['seconds'], source_trades=checks,
        test={m: {s: result['test'][m][s]['metrics'] for s in SIZINGS} for m in MODES})), flush=True)


if __name__ == '__main__':
    main()
