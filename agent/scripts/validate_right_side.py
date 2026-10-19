"""Fixed, offline shared-opportunity comparison; never fits or activates a model."""
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
sys.path.insert(0,str(AGENT))
import cost_model
from event_exit_research import compile_calendar
from market_calendar import is_trading_day
from pullback_entry_research import _bars
from pullback_validation import block_interval, shift_session
from right_side_entry_research import (
    VERSION, POLICIES, MATURITY_SESSIONS, make_context, confirmation_opportunities, replay_right_side,
)
from scripts.validate_event_exit import costed
from scripts.validate_pullback_entry import PERIODS, summarize


def eligible_period(frame, seed):
    day=pd.Timestamp(seed['date']).date()
    observed=pd.DatetimeIndex([pd.Timestamp(stamp.date()) for stamp in frame.index])
    setup=pd.Timestamp(seed['setup_date']).date()
    confirmation_days=[shift_session(setup,n) for n in range(4)]
    if day not in confirmation_days[1:] or not pd.DatetimeIndex(
        [pd.Timestamp(d) for d in confirmation_days if d<=day]).isin(observed).all():
        return None,'setup_confirmation_gap_or_expired'
    sessions=pd.DatetimeIndex([pd.Timestamp(shift_session(day,n)) for n in range(MATURITY_SESSIONS+1)])
    if not sessions.isin(observed).all():
        return None, 'immature_or_missing_sessions'
    end=str(sessions[-1].date())
    for name,start,stop in PERIODS:
        if start <= seed['date'] <= end <= stop:
            return name,end
    return None,'cross_partition'


def run_symbol(args):
    filename,schedule=args
    path=Path(filename)
    raw=path.read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    try:
        f=_bars(pd.read_csv(BytesIO(raw),index_col='Date',parse_dates=['Date']).loc[:'2026-10-15'].tail(756))
        if len(f)<100:
            return dict(symbol=path.stem,sha256=sha,error='insufficient_history')
        context=make_context(f)
        rows,counts,quality=[],Counter(),{}
        for side in ('LONG','SHORT'):
            seeds=confirmation_opportunities(path.stem,f,side)
            runs={policy:replay_right_side(path.stem,f,side,policy,schedule,seeds,context) for policy in POLICIES}
            quality[side]={p:dict(status=r['status'],reason=r.get('reason'),skips=r['skips']) for p,r in runs.items()}
            transactions={p:{t['id']:costed(t,side) for t in r['transactions']} for p,r in runs.items()}
            entries={p:{m['opportunity_id'] for m in r['markers'] if m['stage']=='ENTRY'} for p,r in runs.items()}
            for seed in seeds:
                period,end=eligible_period(f,seed)
                if period is None:
                    counts[side+'_'+end]+=1
                    continue
                if any(r['latest'].get('date','')<end for r in runs.values()):
                    counts[side+'_path_unknown']+=1
                    continue
                if any(seed['id'] in entries[p] and seed['id'] not in transactions[p] for p in POLICIES):
                    counts[side+'_unresolved_entry']+=1
                    continue
                rows.append(dict(id=seed['id'],symbol=path.stem,side=side,date=seed['date'],period=period,
                    variants={p:transactions[p].get(seed['id']) for p in POLICIES}))
        return dict(symbol=path.stem,sha256=sha,opportunities=rows,exclusions=dict(counts),quality=quality)
    except Exception as exc:
        return dict(symbol=path.stem,sha256=sha,error=type(exc).__name__,error_detail=str(exc)[:200])


def trade_stats(trades):
    result=summarize(trades)
    v=np.array([t['net_return'] for t in trades])
    pos,neg=v[v>0],v[v<0]
    daily=defaultdict(list)
    for t in trades:
        daily[t['signal_date']].append(t['net_return'])
    result.update(mean_ci95=block_interval(daily,block_days=8),
        profit_factor=float(pos.sum()/-neg.sum()) if len(neg) else None,
        average_win=float(pos.mean()) if len(pos) else None,
        average_loss=float(neg.mean()) if len(neg) else None,
        mean_borrow_5pct=float(np.mean([t['borrow_5pct_return'] for t in trades])) if trades else None,
        mean_borrow_20pct=float(np.mean([t['borrow_20pct_return'] for t in trades])) if trades else None)
    return result


def aggregate(rows,period,side):
    cohort=[r for r in rows if r['period']==period and r['side']==side]
    result={}
    period_bounds=next((a,b) for n,a,b in PERIODS if n==period)
    days=sorted({r['date'] for r in cohort})
    if days:
        days=[str(d.date()) for d in pd.bdate_range(max(min(days),period_bounds[0]),max(days)) if is_trading_day(d.date())]
    for policy in POLICIES:
        trades=[r['variants'][policy] for r in cohort if r['variants'][policy]]
        opportunity_values=[r['variants'][policy]['net_return'] if r['variants'][policy] else 0. for r in cohort]
        by_day=defaultdict(list)
        difference=defaultdict(list)
        for row,v in zip(cohort,opportunity_values):
            by_day[row['date']].append(v)
            base=row['variants']['current']
            difference[row['date']].append(v-(base['net_return'] if base else 0.))
        daily_delta={d:[float(np.mean(difference[d])) if difference[d] else 0.] for d in days}
        result[policy]=dict(trades=trade_stats(trades),opportunities=len(cohort),
            skipped_or_cash=len(cohort)-len(trades),
            mean_per_opportunity=float(np.mean(opportunity_values)) if cohort else None,
            opportunity_ci95=block_interval(by_day,block_days=8),
            mean_daily_basket_difference=float(np.mean([v[0] for v in daily_delta.values()])) if daily_delta else None,
            daily_basket_difference_ci95=block_interval(daily_delta,block_days=8),
            daily_basket_difference_ci95_block15=block_interval(daily_delta,block_days=15),
            event_delayed_entries=sum(t['event_deferrals']>0 for t in trades),
            event_close_exits=sum(t['exit_reason']=='event_previous_close' for t in trades))
    base=[r['variants']['current'] for r in cohort if r['variants']['current']]
    result['baseline_diagnosis']=dict(
        by_exit={reason:trade_stats([t for t in base if t['exit_reason']==reason]) for reason in sorted({t['exit_reason'] for t in base})},
        ex_ante_strict_aligned=trade_stats([t for t in base if t['strict_at_decision']]),
        ex_ante_strict_not_aligned=trade_stats([t for t in base if not t['strict_at_decision']]),
        worst_examples=sorted(base,key=lambda t:t['net_return'])[:5])
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('cache-dir','calendar','announcements','output'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--workers',type=int,default=2)
    a=p.parse_args()
    cp,ap=Path(a.calendar),Path(a.announcements)
    schedule=compile_calendar(json.loads(cp.read_text(encoding='utf-8-sig')),json.loads(ap.read_text(encoding='utf-8-sig')))
    files=sorted(Path(a.cache_dir).glob('*.csv'))
    if not files:
        raise ValueError('No cached prices')
    results=[]
    started=time.monotonic()
    with ProcessPoolExecutor(max_workers=min(4,max(1,a.workers))) as pool:
        for n,r in enumerate(pool.map(run_symbol,[(str(f),schedule) for f in files]),1):
            results.append(r)
            if n%100==0:
                print(json.dumps(dict(done=n,total=len(files),seconds=round(time.monotonic()-started,1))),flush=True)
    rows=[r for result in results for r in result.get('opportunities',[])]
    matched_dates=sorted({t['exit_date'] for r in rows for t in r['variants'].values() if t and t['exit_reason']=='event_previous_close'})
    unsupported=[d for d in matched_dates if d<'2026-01-01' and (d[5:] in {'07-03','12-24'} or (d[5:7]=='11' and pd.Timestamp(d).dayofweek==4))]
    if unsupported:
        raise ValueError('Verify historical early close deadline: '+','.join(unsupported))
    report=dict(version=VERSION,as_of='2026-10-15',fitted=False,parameter_search=False,eligible_for_activation=False,
        paid_requests=0,external_requests=0,production_writes=0,entry='next_open_after_complete_signal',
        event_day_entry='defer_until_event_day_complete_then_next_open_if_reconfirmed',
        same_close='historical_quote_reference_only',maturity_sessions=MATURITY_SESSIONS,
        files=len(files),processed=sum('opportunities' in r for r in results),
        errors=dict(Counter(r['error'] for r in results if r.get('error'))),
        error_symbols=[{k:r.get(k) for k in ('symbol','error','error_detail')} for r in results if r.get('error')],
        exclusions=dict(sum((Counter(r.get('exclusions',{})) for r in results),Counter())),
        calendar_audit=schedule.audit,calendar_coverage=dict(Counter(e['kind'] for e in schedule.exits if e['exit_date']<='2026-10-15')),
        cost_assumptions=cost_model.cost_summary(),
        periods={n:{s:aggregate(rows,n,s) for s in ('LONG','SHORT')} for n,_,_ in PERIODS},
        matched_exit_days=matched_dates,
        input_manifest=dict(calendar_sha256=hashlib.sha256(cp.read_bytes()).hexdigest(),
            announcements_sha256=hashlib.sha256(ap.read_bytes()).hexdigest(),
            prices=[dict(symbol=r['symbol'],sha256=r['sha256']) for r in results]),
        code_manifest={str(path.relative_to(AGENT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            Path(__file__),AGENT/'right_side_entry_research.py',AGENT/'event_exit_research.py',
            AGENT/'pullback_entry_research.py',AGENT/'rebound_short_research.py',AGENT/'cost_model.py',
            AGENT/'pullback_validation.py',AGENT/'market_calendar.py',AGENT/'scripts/validate_event_exit.py']},
        quality={side:{policy:dict(Counter(r['quality'][side][policy]['status'] for r in results if 'quality' in r))
                       for policy in POLICIES} for side in ('LONG','SHORT')},
        skips={side:{policy:dict(sum((Counter(r['quality'][side][policy]['skips']) for r in results if 'quality' in r),Counter()))
                     for policy in POLICIES} for side in ('LONG','SHORT')},
        examples=[r for r in rows if r['symbol'] in {'MCD','NVDA','AAPL'} and r['period']=='test'],
        limitations=['shared original confirmation opportunities, including original shadow-position suppression',
            'setup-to-confirmation missing-session paths excluded equally; baseline live long path itself is not modified',
            'no executed trade is not a loss or win: cash zero only on common mature opportunities',
            'different trades across policies: daily opportunity basket difference is not paired-trade profit',
            'no account equity curve or same-time SPY alpha',
            'event coverage incomplete; absent calendar record is unknown, not event-free',
            'event-day close can precede after-hours publication; next open gap still possible',
            'calendar reconstructed with preknown announcements, no full immutable historical archive',
            'cached universe/corporate actions/PIT not certified; historical periods have been inspected',
            'short proxy excludes actual borrow, dividends and recall; not Put PnL',
            'fifteen-session common maturity buffer excludes recent signals without looking at realized profit'],
        seconds=round(time.monotonic()-started,1))
    Path(a.output).write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(files=report['files'],processed=report['processed'],errors=report['errors'],
        seconds=report['seconds'],test={s:{p:report['periods']['test'][s][p]['trades'] for p in POLICIES}
                                      for s in ('LONG','SHORT')})),flush=True)


if __name__=='__main__':
    main()
