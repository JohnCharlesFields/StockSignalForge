"""Offline, fixed four-comparison follow-up on an audited opportunity cohort."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
from entry_path_research import market_states
from event_exit_research import compile_calendar
from market_calendar import is_trading_day
from planned_risk_research import VERSION, EXPERIMENTS, COMPARISONS
from pullback_entry_research import _bars
from scripts.validate_entry_paths import sha, run_symbol, summarize
from scripts.validate_right_side import PERIODS

ALL = ('baseline',) + EXPERIMENTS
COHORT_SHA = '14fcf2bf9077e1ec4be3bd0a5bf4bab6ca8f4cb78806ec2d3fa159ef2ab17e55'
AUDIT_SHA = '872955a3f3cd53366657355365df3b1daddb57a575684f35a31193fae544a87a'


def intervals(values, block):
    values = np.asarray(values, dtype=float)
    if not len(values) or not np.isfinite(values).all():
        raise ValueError('finite_daily_pair_values_required')
    rng = np.random.default_rng(42)
    starts = rng.integers(0, len(values), (10000, int(np.ceil(len(values)/block))))
    idx = ((starts[:, :, None]+np.arange(block)) % len(values)).reshape(10000, -1)[:, :len(values)]
    draws = values[idx].mean(axis=1)
    return dict(mean=float(values.mean()), ci95=np.quantile(draws, [.025, .975]).tolist(),
                family_ci9875=np.quantile(draws, [.00625, .99375]).tolist())


def inference(rows, periods):
    result = {}
    for period, _, _ in PERIODS:
        result[period] = {}
        for side, name in COMPARISONS:
            daily = defaultdict(list)
            for r in rows:
                if r['period'] == period and r['side'] == side:
                    t, b = r['variants'][name], r['variants']['baseline']
                    daily[r['date']].append((t['net_return'] if t else 0.)-(b['net_return'] if b else 0.))
            key = side+':'+name
            if not daily:
                result[period][key] = None
                continue
            days = [str(d.date()) for d in pd.bdate_range(min(daily), max(daily)) if is_trading_day(d.date())]
            values = np.array([np.mean(daily[d]) if daily[d] else 0. for d in days])
            out = {str(block): intervals(values, block) for block in (8, 15)}
            assert all(np.isclose(v['mean'], periods[period][side][name]['equal_day_difference']) for v in out.values())
            result[period][key] = out
    return dict(samples=10000, comparisons=len(COMPARISONS), family_confidence=.9875,
                method='paired equal-day cash-zero basket; circular trading-day blocks; Bonferroni',
                untouched_holdout=False, periods=result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('previous', 'reference', 'calendar', 'announcements', 'design', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--symbols', default='')
    p.add_argument('--workers', type=int, default=2)
    a = p.parse_args()
    old = Path(a.previous)
    audit_raw = (old/'results-calendar-audited.json').read_bytes()
    cohort_raw = (old/'calendar-audited-opportunities.json').read_bytes()
    if sha(audit_raw) != AUDIT_SHA or sha(cohort_raw) != COHORT_SHA:
        raise ValueError('frozen_previous_evidence_changed')
    audit, cohort = json.loads(audit_raw), json.loads(cohort_raw)
    reference = json.loads(Path(a.reference).read_bytes())
    legacy = old/'legacy_pullback_entry_research.py'
    assert sha(legacy.read_bytes()) == reference['code_manifest']['pullback_entry_research.py']
    cp, ap = Path(a.calendar), Path(a.announcements)
    assert sha(cp.read_bytes()) == audit['input_manifest']['calendar_sha256']
    assert sha(ap.read_bytes()) == audit['input_manifest']['announcements_sha256']
    schedule = compile_calendar(json.loads(cp.read_bytes()), json.loads(ap.read_bytes()))
    source_paths = [Path(__file__), AGENT/'entry_path_research.py', AGENT/'planned_risk_research.py',
                    AGENT/'right_side_entry_research.py', AGENT/'scripts/validate_entry_paths.py',
                    AGENT/'scripts/validate_event_exit.py', AGENT/'scripts/validate_right_side.py',
                    AGENT/'pullback_entry_research.py', AGENT/'rebound_short_research.py',
                    AGENT/'cost_model.py', AGENT/'market_calendar.py', AGENT/'pullback_validation.py',
                    AGENT/'technical_signal_metrics.py', AGENT/'event_exit_research.py']
    code = {str(f.relative_to(AGENT)): sha(f.read_bytes()) for f in source_paths}
    design_hash = sha(Path(a.design).read_bytes())
    files = audit['input_manifest']['prices']
    for item in files:
        if sha((old/'prices'/(item['symbol']+'.csv')).read_bytes()) != item['sha256']:
            raise ValueError('frozen_price_changed:'+item['symbol'])
    if a.symbols:
        selected = set(a.symbols.split(','))
        cohort = [r for r in cohort if r['symbol'] in selected]
        files = [f for f in files if f['symbol'] in selected]
    by_symbol = defaultdict(list)
    for r in cohort:
        by_symbol[r['symbol']].append({**r, 'variants': {'current': r['variants']['baseline']}})
    spy_raw = (old/'prices/SPY.csv').read_bytes()
    spy = _bars(pd.read_csv(BytesIO(spy_raw), index_col='Date', parse_dates=['Date']).loc[:'2026-10-15'].tail(756))
    spy = spy.loc[[is_trading_day(d.date()) for d in spy.index]]
    market = market_states(spy)
    jobs = [(str(old/'prices'/(f['symbol']+'.csv')), by_symbol[f['symbol']], f['sha256'],
             str(legacy), schedule, market, spy.index, ALL) for f in files]
    started, results = time.monotonic(), []
    with ProcessPoolExecutor(max_workers=min(4, max(1, a.workers))) as pool:
        for i, r in enumerate(pool.map(run_symbol, jobs), 1):
            results.append(r)
            if i % 100 == 0:
                print(json.dumps(dict(done=i, total=len(jobs), seconds=round(time.monotonic()-started, 1))), flush=True)
    errors = [r for r in results if r['status'] == 'error']
    output = Path(a.output)
    if errors:
        output.with_name(output.stem+'-errors.json').write_text(json.dumps(errors, indent=2), encoding='utf-8')
        raise ValueError('reproduction_failed_see_isolated_errors')
    rows = [x for r in results for x in r.get('rows', [])]
    assert len(rows) == len(cohort) and len({r['id'] for r in rows}) == len(rows)
    expected = {r['id']: r for r in cohort}
    for r in rows:
        assert r['variants']['baseline'] == expected[r['id']]['variants']['baseline']
        for name in EXPERIMENTS:
            if (name == 'long_risk_floor') != (r['side'] == 'LONG'):
                assert r['variants'][name] == r['variants']['baseline']
    periods = {period: {side: summarize(rows, period, side, ALL) for side in ('LONG', 'SHORT')}
               for period, _, _ in PERIODS}
    family = inference(rows, periods)
    assert code == {str(f.relative_to(AGENT)): sha(f.read_bytes()) for f in source_paths}
    assert design_hash == sha(Path(a.design).read_bytes())
    assert all(sha((old/'prices'/(f['symbol']+'.csv')).read_bytes()) == f['sha256'] for f in files)
    report = dict(version=VERSION, fitted=False, eligible_for_activation=False, as_of='2026-10-15',
        smoke_symbols=a.symbols or None, baseline_exact=True, non_applicable_controls_exact=True,
        qualified_opportunities=len(rows), processed=sum(r['status'] == 'processed' for r in results),
        insufficient=sum(r['status'] == 'insufficient_history' for r in results),
        exclusions=dict(sum((Counter(r.get('exclusions', {})) for r in results), Counter())),
        periods=periods, inference=family, code_manifest=code, design_sha256=design_hash,
        input_manifest=dict(previous_audit_sha256=AUDIT_SHA, cohort_sha256=COHORT_SHA,
            calendar_sha256=sha(cp.read_bytes()), announcements_sha256=sha(ap.read_bytes()),
            prices=files, legacy_module_sha256=sha(legacy.read_bytes())),
        limitations=audit['limitations']+['follow-up hypotheses informed by previously viewed path diagnostics',
            'planned close risk differs from actual open risk; thresholds are fixed hypotheses, not fitted optima',
            'four applicable comparisons only; no combinations or parameter search'],
        external_requests=0, paid_requests=0, production_writes=0, seconds=round(time.monotonic()-started, 1))
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    output.with_name(output.stem+'-opportunities.json').write_text(json.dumps(rows, allow_nan=False), encoding='utf-8')
    print(json.dumps(dict(seconds=report['seconds'], opportunities=len(rows),
        latest={side: {n: periods['test'][side][n]['trades'] for n in ALL} for side in ('LONG', 'SHORT')})), flush=True)


if __name__ == '__main__':
    main()
