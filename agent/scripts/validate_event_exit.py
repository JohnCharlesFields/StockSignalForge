"""Offline matched-entry event-exit comparison, with explicit incomplete coverage."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
import cost_model
from event_exit_research import VERSION, apply_event_exit, compile_calendar
from pullback_entry_research import build_entry_timeline
from rebound_short_research import build_short_timeline
from pullback_validation import block_interval, shift_session
from scripts.validate_pullback_entry import PERIODS, partition, summarize

VIEWS = ('fomc_only', 'earnings_only', 'combined')


def costed(trade, side):
    gross = (trade['exit_price']/trade['entry_price']-1)*(1 if side == 'LONG' else -1)
    cost = cost_model.equity_round_trip_cost(trade['entry_price'], trade['adv'])
    days = (pd.Timestamp(trade['exit_date'])-pd.Timestamp(trade['entry_date'])).days
    return {**trade, 'net_return':gross-cost, 'cost':cost, 'side':side,
        'borrow_5pct_return':gross-cost-(.05*days/365 if side=='SHORT' else 0),
        'borrow_20pct_return':gross-cost-(.20*days/365 if side=='SHORT' else 0)}


def run_symbol(args):
    filename, schedule = args
    path = Path(filename)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        frame = pd.read_csv(BytesIO(raw), index_col='Date', parse_dates=['Date']).loc[:'2026-10-15'].tail(756)
        if len(frame) < 100:
            return dict(symbol=path.stem, sha256=sha, error='insufficient_history')
        outputs, exclusions, quality = [], Counter(), {}
        for side, build in [('LONG', build_entry_timeline), ('SHORT', build_short_timeline)]:
            timeline = build(path.stem, frame)
            quality[side] = dict(status=timeline['status'], reason=timeline.get('reason'))
            exclusions[side+'_unresolved_entries'] = sum(m['stage']=='ENTRY' for m in timeline['markers'])-len(timeline['transactions'])
            for trade in timeline['transactions']:
                trade = {**trade, 'symbol':path.stem}
                trade['entry_on_known_event_date'] = any(e['event_date']==trade['entry_date'] and
                    (e['kind']!='EARNINGS' or path.stem in e['subjects']) for e in schedule.exits)
                group = partition(trade)
                if group is None:
                    exclusions[side+'_cross_partition'] += 1
                    continue
                expected = pd.DatetimeIndex([pd.Timestamp(shift_session(pd.Timestamp(trade['entry_date']).date(), n))
                                             for n in range(trade['held_sessions']+1)])
                if not expected.isin(frame.index).all() or str(expected[-1].date()) != trade['exit_date']:
                    exclusions[side+'_missing_sessions'] += 1
                    continue
                variants = {view:costed(apply_event_exit(trade, frame, schedule, side, view), side) for view in VIEWS}
                outputs.append(dict(period=group, side=side, baseline=costed(trade, side), variants=variants))
        return dict(symbol=path.stem, sha256=sha, pairs=outputs, exclusions=dict(exclusions), quality=quality)
    except Exception as exc:
        return dict(symbol=path.stem, sha256=sha, error=type(exc).__name__)


def stats(pairs, view):
    base = [pair['baseline'] for pair in pairs]
    new = [pair['variants'][view] for pair in pairs]
    differences, wins = defaultdict(list), defaultdict(list)
    for a, b in zip(base, new):
        differences[a['signal_date']].append(b['net_return']-a['net_return'])
        wins[a['signal_date']].append(float(b['net_return']>0)-float(a['net_return']>0))
    affected = [trade for trade in new if trade.get('event_exit')]
    return dict(baseline=summarize(base), event_exit=summarize(new),
        paired_mean_difference=float(np.mean([b['net_return']-a['net_return'] for a,b in zip(base,new)])) if pairs else None,
        paired_difference_ci95=block_interval(differences, block_days=8),
        win_rate_difference=float(np.mean([float(b['net_return']>0)-float(a['net_return']>0) for a,b in zip(base,new)])) if pairs else None,
        win_rate_difference_ci95=block_interval(wins, block_days=8),
        affected=len(affected), affected_symbols=len({trade['symbol'] for trade in affected}),
        unchanged_entries_on_known_event_date=sum(bool(trade.get('entry_on_known_event_date')) for trade in base),
        distinct_event_dates=len({day for trade in affected for day in trade['event_dates']}),
        exit_event_types=dict(Counter(kind for trade in affected for kind in trade['event_types'])),
        mean_borrow_5pct=float(np.mean([trade['borrow_5pct_return'] for trade in new])) if pairs else None,
        mean_borrow_20pct=float(np.mean([trade['borrow_20pct_return'] for trade in new])) if pairs else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', required=True)
    parser.add_argument('--calendar', required=True)
    parser.add_argument('--announcements', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    calendar_path, announcement_path = Path(args.calendar), Path(args.announcements)
    export = json.loads(calendar_path.read_text(encoding='utf-8-sig'))
    announcements = json.loads(announcement_path.read_text(encoding='utf-8-sig'))
    schedule = compile_calendar(export, announcements)
    files = sorted(Path(args.cache_dir).glob('*.csv'))
    if not files:
        raise ValueError('No cache files')
    started, results = time.monotonic(), []
    with ProcessPoolExecutor(max_workers=min(4, max(1, args.workers))) as pool:
        for n, result in enumerate(pool.map(run_symbol, [(str(path),schedule) for path in files]), 1):
            results.append(result)
            if n % 100 == 0:
                print(json.dumps(dict(done=n, total=len(files), seconds=round(time.monotonic()-started,1))), flush=True)
    pairs = [pair for result in results for pair in result.get('pairs', [])]
    matched_exit_days = sorted({pair['variants']['combined']['exit_date'] for pair in pairs
                               if pair['variants']['combined'].get('event_exit')})
    # The shared precise half-day table starts in 2026. Fail closed on possible
    # earlier half-days instead of assigning a post-close knowledge deadline.
    unsupported_half_days = [day for day in matched_exit_days if day < '2026-01-01' and (
        day[5:] in {'07-03', '12-24'} or (day[5:7]=='11' and pd.Timestamp(day).dayofweek==4))]
    if unsupported_half_days:
        raise ValueError('Historical half-day decision deadline requires verification: '+','.join(unsupported_half_days))
    report = dict(protocol=VERSION, primary_view='combined', matched_entries=True, capital_redeployment=False,
        fitted=False, parameter_search=False, eligible_for_activation=False, full_event_avoidance_validated=False,
        as_of='2026-10-15', paid_requests=0, external_requests=0, production_db_writes=0,
        files=len(files), processed=sum('pairs' in result for result in results),
        errors=dict(Counter(result['error'] for result in results if result.get('error'))),
        exclusions=dict(sum((Counter(result.get('exclusions', {})) for result in results), Counter())),
        quality={side:dict(Counter(result['quality'][side]['status'] for result in results if 'quality' in result)) for side in ('LONG','SHORT')},
        cost_assumptions=cost_model.cost_summary(),
        calendar_audit=schedule.audit, eligible_exit_events=dict(Counter(e['kind'] for e in schedule.exits)),
        eligible_exit_events_through_asof=dict(Counter(e['kind'] for e in schedule.exits if e['exit_date']<='2026-10-15')),
        calendar_exits=schedule.exits,
        matched_exit_days=matched_exit_days, unsupported_half_days=unsupported_half_days,
        affected_earnings=[dict(symbol=pair['baseline']['symbol'], side=pair['side'], period=pair['period'],
            baseline=pair['baseline'], event_exit=pair['variants']['earnings_only']) for pair in pairs
            if pair['variants']['earnings_only'].get('event_exit')],
        periods={name:{side:{view:stats([pair for pair in pairs if pair['period']==name and pair['side']==side], view)
                            for view in VIEWS} for side in ('LONG','SHORT')} for name,_,_ in PERIODS},
        mcd=[pair for result in results if result['symbol']=='MCD' for pair in result.get('pairs',[])],
        affected_examples=[dict(symbol=pair['baseline']['symbol'], side=pair['side'], period=pair['period'],
            baseline=pair['baseline'], event_exit=pair['variants']['combined']) for pair in pairs
            if pair['variants']['combined'].get('event_exit')][:20],
        input_manifest=dict(calendar_sha256=hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
            announcements_sha256=hashlib.sha256(announcement_path.read_bytes()).hexdigest(),
            prices=[dict(symbol=result['symbol'],sha256=result['sha256']) for result in results]),
        code_manifest={str(path.relative_to(AGENT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            Path(__file__), AGENT/'event_exit_research.py', AGENT/'pullback_entry_research.py', AGENT/'rebound_short_research.py',
            AGENT/'market_calendar.py', AGENT/'pullback_validation.py', AGENT/'cost_model.py']},
        limitations=['known-event subset only, not complete historical earnings/CPI/PCE/NFP/GDP coverage',
            'missing calendar records are unknown, not event-free', 'official tentative FOMC announcements, not emergency-meeting prediction',
            'source HTML retrospectively read; dated announcement metadata, not an immutable captured web archive',
            'close minus 15 minutes knowledge cutoff; reference-close fills with modeled costs, not guaranteed fills',
            'historical early-close table coverage before 2026 incomplete; matched exit days require audit',
            'same completed entries only, no redeployment; pending baseline trades excluded from both arms',
            'exit-only experiment does not suppress new entries on event dates; not a complete live avoidance policy',
            'few distinct macro event dates; clustered samples not independent evidence',
            'cached universe and corporate actions not PIT certified; historical periods already inspected',
            'short proxies exclude observed borrowing/dividends/recall; not Put returns or account PnL'],
        seconds=round(time.monotonic()-started,1))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2), encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key not in
        ('mcd','affected_examples','affected_earnings','input_manifest','calendar_exits')},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
