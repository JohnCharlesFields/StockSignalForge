"""Orthogonality: do the stage-1 survivors (DD-deep, RS20>0) add edge ON TOP of
the already-shipped deep-oversold (DOS) filter?

Stage-1 (backtest_factor_screen) found two filters beat the pullback_hv top bucket
with CI>0: deep drawdown (<0.75 of 252d high) and 20d relative strength vs SPY > 0.
DD-deep is conceptually "beaten down" = likely the SAME thing DOS already flags, so
the real test is the INCREMENTAL difference (cluster-bootstrap CI on the contrast),
not each group's own CI. RS20 is a different axis (relative, not absolute oversold)
so it could be orthogonal. DOS-hit = z<-1.5 OR rsi2<10 (shipped definition).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from statistics import mean

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
for _p in (AGENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402
from research_signal_framework_backtest import _collect_symbol_events  # noqa: E402


def _universe(name, max_symbols):
    from screening_framework_v2_optimized import resolve_universe
    try:
        syms, _s, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:max_symbols]


def _osc(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _by(rows):
    d = {}
    for e in rows:
        d.setdefault(e["ticker"], []).append(float(e["net_excess"]))
    return d


def _ci(rows, n_boot=3000, seed=42):
    by = _by(rows)
    cl = [k for k in by if by[k]]
    if len(cl) < 2:
        return (None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        v = [x for c in s for x in by[c]]
        if v:
            ests.append(sum(v) / len(v))
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _diff(a, b, n_boot=3000, seed=7):
    ba, bb = _by(a), _by(b)
    cl = sorted(set(ba) | set(bb))
    if len(cl) < 2:
        return (None, None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        xa = [x for c in s for x in ba.get(c, [])]
        xb = [x for c in s for x in bb.get(c, [])]
        if xa and xb:
            ests.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    if not ests:
        return (None, None, None)
    ests.sort()
    return (round(mean(ests), 6), round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _row(label, rows):
    if not rows:
        print(f"  {label:24} n=0", flush=True)
        return
    nets = [e["net_excess"] for e in rows]
    lo, hi = _ci(rows)
    sig = "SIG(>0)" if (lo is not None and lo > 0) else "not-sig"
    ci = f"[{lo}, {hi}]" if lo is not None else "[n/a]"
    print(f"  {label:24} n={len(rows):<5} tk={len({e['ticker'] for e in rows}):<3} net={mean(nets)*100:+.3f}%  CI95={ci}  hit={sum(1 for v in nets if v>0)/len(nets):.0%}  {sig}", flush=True)


def _inc(label, a, b):
    d, lo, hi = _diff(a, b)
    if d is None:
        print(f"  {label}: n/a", flush=True)
        return
    verdict = "INCREMENTAL (CI>0)" if lo > 0 else "no increment (CI incl 0)"
    print(f"  {label}: diff={d*100:+.3f}%  CI95=[{lo}, {hi}]  -> {verdict}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--top-quantile", type=float, default=0.70)
    args = ap.parse_args()

    rt = cost_model.equity_round_trip_cost()
    syms = _universe(args.universe, args.max_symbols)
    try:
        sf, _ = get_daily_history("SPY", period=args.period)
        spy = pd.to_numeric(sf["Close"], errors="coerce").reset_index(drop=True)
        spy_idx = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(sf.index)}
    except Exception:
        spy, spy_idx = None, {}
    print(f"universe={args.universe} symbols={len(syms)} h={args.horizon} cost(rt)={rt:.4f} spy={'ok' if spy is not None else 'NA'}", flush=True)

    events = []
    for sym in syms:
        try:
            frame, _s = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or len(frame) < 230 + args.horizon:
            continue
        evs = _collect_symbol_events(sym, frame, args.horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        rsi2, z = _osc(close)
        hi252 = close.rolling(252).max()
        ret20 = close / close.shift(20) - 1.0
        pos = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(frame.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 210:
                continue
            e["rsi2"] = float(rsi2.iloc[i]) if pd.notna(rsi2.iloc[i]) else None
            e["z"] = float(z.iloc[i]) if pd.notna(z.iloc[i]) else None
            e["dd"] = (float(close.iloc[i]) / hi252.iloc[i]) if pd.notna(hi252.iloc[i]) and hi252.iloc[i] else None
            rs = None
            if spy is not None:
                j = spy_idx.get(e["date"])
                if j is not None and j >= 20 and pd.notna(ret20.iloc[i]):
                    rs = float(ret20.iloc[i]) - float(spy.iloc[j] / spy.iloc[j - 20] - 1.0)
            e["rs20"] = rs
            e["net_excess"] = e["excess_return"] - rt
            events.append(e)

    scores = sorted(e["pullback_hv_score"] for e in events)
    cut = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cut]
    print(f"events={len(events)} top={len(top)}", flush=True)

    dos = lambda e: (e.get("z") is not None and e["z"] < -1.5) or (e.get("rsi2") is not None and e["rsi2"] < 10)
    ddeep = lambda e: e.get("dd") is not None and e["dd"] < 0.75
    rspos = lambda e: e.get("rs20") is not None and e["rs20"] > 0

    DOS = [e for e in top if dos(e)]
    NDOS = [e for e in top if not dos(e)]

    print("\n=== GROUPS ===", flush=True)
    _row("TOP base", top)
    _row("DOS", DOS)
    print("\n--- DD-deep (<0.75 of 252d high) ---", flush=True)
    _row("DOS & DDdeep", [e for e in DOS if ddeep(e)])
    _row("DOS & not-DDdeep", [e for e in DOS if not ddeep(e)])
    _row("nonDOS & DDdeep", [e for e in NDOS if ddeep(e)])
    _row("nonDOS & not-DDdeep", [e for e in NDOS if not ddeep(e)])
    print("\n--- RS20 > 0 (beat SPY 20d) ---", flush=True)
    _row("DOS & RS>0", [e for e in DOS if rspos(e)])
    _row("DOS & RS<=0", [e for e in DOS if not rspos(e)])
    _row("nonDOS & RS>0", [e for e in NDOS if rspos(e)])
    _row("nonDOS & RS<=0", [e for e in NDOS if not rspos(e)])

    print("\n=== INCREMENTAL TESTS (contrast CI) ===", flush=True)
    _inc("DD-deep inside DOS  [DOS&DDdeep]-[DOS&not]", [e for e in DOS if ddeep(e)], [e for e in DOS if not ddeep(e)])
    _inc("DD-deep outside DOS [nDOS&DDdeep]-[nDOS&not]", [e for e in NDOS if ddeep(e)], [e for e in NDOS if not ddeep(e)])
    _inc("RS>0 inside DOS     [DOS&RS>0]-[DOS&RS<=0]", [e for e in DOS if rspos(e)], [e for e in DOS if not rspos(e)])
    _inc("RS>0 outside DOS    [nDOS&RS>0]-[nDOS&RS<=0]", [e for e in NDOS if rspos(e)], [e for e in NDOS if not rspos(e)])

    print("\nCaveats: in-sample, single period, archive_only (survivorship); FDR (multiple filters).", flush=True)


if __name__ == "__main__":
    main()
